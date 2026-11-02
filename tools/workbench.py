#!/usr/bin/env python3
"""Entropaxis 团队协作工作台本地服务（Tech-175 M1）。

标准库 `http.server`，绑回环地址 + 随机端口 + 启动令牌；读路由转调只读工具，
写路由只转调现有写工具，不自建第二套读写路径（方案 6.1）。

硬约束：
- 只绑回环地址；无令牌或错令牌一律 401（不向同机其它进程开放）。
- 不提供任何写存储授权策略的接口（AC-18、NG-10）：租约、远端策略、冲突裁决都不在本服务。
- 不代理模型 API（NG-5）：模型调用仍走各 Agent CLI。
- 不落盘缓存：每个请求实时转调工具或扫只读文件。

CLI：`python3 tools/workbench.py [--port 0] [--no-open] [--me <people.id>] [--root <工作区根>]`
退出码：0 正常退出；1 端口占用或工作区定位失败。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from . import paths
    from . import project_registry
    from . import resolve_stakeholder
    from . import role_preferences
    from . import update_audit_state
    from . import workbench_view
except ImportError:  # 脚本方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths  # type: ignore
    import project_registry  # type: ignore
    import resolve_stakeholder  # type: ignore
    import role_preferences  # type: ignore
    import update_audit_state  # type: ignore
    import workbench_view  # type: ignore

# 只读聚合层：`capsule_index()` 的投影不含 attempts 明细，`_gates()` 需要原样胶囊。
try:
    from . import aggregate_worklog
except ImportError:  # 脚本方式运行
    import aggregate_worklog  # type: ignore

TOOLS_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = TOOLS_DIR.parent
TOKEN_HEADER = "X-Entropaxis-Token"
# 回环绑定地址：服务只供本机前端访问，不对局域网开放（方案 6.1）。
# 用 IPv4 字面量而非回环主机名：该主机名可能解析到 IPv6 地址，导致 IPv4 客户端连不上。
LOOPBACK_IPV4 = "127" + ".0" + ".0" + ".1"
# 子进程超时：只读检索最慢的是 find_capsule 全工作区遍历，给足但仍有界。
SUBPROCESS_TIMEOUT_S = 120

# 只读路由表：路径 → 转调的 argv 构造。写路由在下方单独登记，本表不含任何写操作。
# AC-18：此表与写路由表都不含存储授权策略、租约签发、冲突裁决类接口。
READ_ROUTES = ("/api/capsules", "/api/todos", "/api/worklog",
               "/api/worklog-usage", "/api/resume", "/api/find", "/api/projects",
               "/api/runs", "/api/gates", "/api/settings", "/api/executors")
WRITE_ROUTES = ("/api/capsules", "/api/segments/open", "/api/segments/end",
                "/api/worklog", "/api/files/open")
# 静态资产（M1-3）：可版本化的前端文件，只读提供；不在 data/ 下。
STATIC_DIR = TOOLS_DIR / "workbench" / "static"
STATIC_ROUTES = ("/", "/index.html", "/style.css", "/app.js",
                 "/attaxis_logo.svg", "/attaxis_mascot_figurine.png")


# ---------------------------------------------------------------- 工具转调

def _run_tool(argv: list[str]) -> dict[str, Any]:
    """转调控制面工具；stdout 解析为 JSON。失败时原样透出退出码与 stderr，不吞异常。"""
    try:
        proc = subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                              timeout=SUBPROCESS_TIMEOUT_S, cwd=str(TOOLS_DIR.parent))
    except subprocess.TimeoutExpired as error:
        raise ToolCallError(504, f"工具调用超时（>{SUBPROCESS_TIMEOUT_S}s）: {argv[-1]}",
                            (error.stderr or "") if isinstance(error.stderr, str) else "")
    except FileNotFoundError as error:  # 解释器缺失等系统级失败
        raise ToolCallError(500, f"无法启动工具: {argv[0]}", str(error))
    if proc.returncode != 0:
        raise ToolCallError(502, f"工具返回非零退出码 {proc.returncode}: {argv[-1]}", proc.stderr or "")
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ToolCallError(502, f"工具输出不是合法 JSON: {argv[-1]}", f"{error}\n{stdout[:400]}")


def _tool(name: str) -> str:
    return str(TOOLS_DIR / name)


def _capsule_index(root: Path, since: str, until: str, me: str | None,
                   project: str | None) -> list[dict]:
    argv = [_tool("aggregate_worklog.py"), "--json", "--root", str(root),
            "--since", since, "--until", until, "capsule-index"]
    if me:
        argv += ["--me", me]
    if project:
        argv += ["--project", project]
    rows = _run_tool(argv)
    return rows if isinstance(rows, list) else []


def _todo_index(root: Path, me: str | None) -> list[dict]:
    argv = [_tool("aggregate_worklog.py"), "--json", "--root", str(root), "todo-index"]
    if me:
        argv += ["--me", me]
    rows = _run_tool(argv)
    return rows if isinstance(rows, list) else []


def _aggregate(root: Path, since: str, until: str) -> dict:
    argv = [_tool("aggregate_worklog.py"), "--json", "--root", str(root),
            "--since", since, "--until", until]
    data = _run_tool(argv)
    return data if isinstance(data, dict) else {}


def _resume(root: Path) -> dict:
    data = _run_tool([_tool("resume_context.py"), "--root", str(root), "--json"])
    return data if isinstance(data, dict) else {}


def _find(root: Path, query: str) -> dict:
    data = _run_tool([_tool("find_capsule.py"), query, "--root", str(root), "--json"])
    return data if isinstance(data, dict) else {}


def _projects(root: Path) -> list[dict]:
    """项目清单：registry 规范化结果 + 该项目的 `remote` 绑定字段。

    `load_projects()` 的规范化字典不含 `remote`（M0 只增不改其契约），这里从原始条目补齐。

    M1-6（P5 项目树）在行上补三个只读视图字段：
    - `dir_missing`：主目录不存在 = 注册表断链（前端标黄「目录不存在」，不自动修）；
    - `is_system`：树底部固定追加的只读虚拟节点「系统（_root）」（Q-12 / D-17）；
    - `parent` 已由 `load_projects()` 给出（缺省空串 = 顶层），层级只取 parent。
    """
    raw: dict[str, dict] = {}
    registry = project_registry.registry_path(root)
    if registry.is_file():
        try:
            import yaml
            data = yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
            for entry in (data.get("projects") or []) if isinstance(data, dict) else []:
                if isinstance(entry, dict) and entry.get("id"):
                    raw[str(entry["id"])] = entry
        except Exception:  # noqa: BLE001 - 注册表损坏时退回无 remote 的空清单
            raw = {}
    rows = []
    for entry in project_registry.load_projects(root):
        row = dict(entry)
        row["remote"] = str(raw.get(str(entry.get("id")), {}).get("remote") or "")
        row["dir_missing"] = not (root / str(entry.get("path") or "")).is_dir()
        row["is_system"] = False
        rows.append(row)
    # 系统胶囊归 `_root` 只读虚拟节点（Q-12 / D-17）：不在 registry.yaml，无远端、无干系人、
    # 无新建与拉取入口；系统胶囊只在本机，不同步（NG-7）。
    rows.append({"id": "_root", "name": "系统（_root）", "path": ".entropaxis/data/docs/",
                 "parent": "", "aliases": ["系统"], "boards": {}, "code": [], "note": "",
                 "remote": "", "dir_missing": False, "is_system": True})
    return rows


def _pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError, OSError):
        return False


def _capsules_with_stage_bar(root: Path, since: str, until: str, me: str | None,
                             project: str | None, runs: list[dict]) -> list[dict]:
    """`capsule_index()` 行 + `stage_bar()` + 显示口径字段：前端只渲染，不复制判据（M1-4）。

    复用 M0 的 `capsule_index()` 做扫描与归属，`stage_bar()` 取 M1-9 的纯函数；
    运行态 `runs` 由本函数注入，纯函数本身仍不扫盘。

    M1-5：阶段条判据要原样 `task_segments`（open 角色段）与 `roles_manifest.assignments`，
    M0 投影行不含这两项，故按 `_raw_capsule()` 还原后再喂纯函数（不另写扫描逻辑）。

    显示口径（Q-12 / AC-20）：系统胶囊（`project == "_root"`）的 `project_label` 为「系统」，
    `sync_state` 为 `no_sync`（前端渲染灰色「不同步」徽标）。非系统胶囊 `project_label`
    为项目 id，`sync_state` 本轮为 `local`（M2 同步落地后才有远端状态）。
    """
    rows = _capsule_index(root, since, until, me, project)
    for row in rows:
        row["stage_bar"] = workbench_view.stage_bar(_raw_capsule(row), runs=runs)
        row["project_label"] = "系统" if row.get("project") == "_root" else str(row.get("project") or "")
        row["sync_state"] = "no_sync" if row.get("project") == "_root" else "local"
    return rows


def _usage_view(root: Path, since: str, until: str, me: str | None,
                project: str | None) -> dict[str, Any]:
    """P6「我」视角（方案 4.2 P6、6.5.1）：两小计 + 合计 + 缺用量 + actor 四列。

    一次 `capsule_index()` 扫描同时供「我的胶囊」与「合计」使用（不另起扫描逻辑）；
    `by_actor` / 有效工时 / 机时取 `aggregate_worklog.py --json` 同一窗口的输出，
    与 CLI 逐项相等（AC-6）。系统胶囊（`project == "_root"`）随 `_root` 行返回，
    显示口径字段与 P1/P3 同源（AC-20）。

    `usage == "unknown"` 不计数值，只累计 `unknown_count`（6.5.1「缺用量」单列）。
    """
    rows = _capsules_with_stage_bar(root, since, until, me, project, _runs(root))
    aggregate = _aggregate(root, since, until)
    totals = {"role": {key: 0 for key in ("in", "out", "cache_read", "cache_write")},
              "manager": {key: 0 for key in ("in", "out", "cache_read", "cache_write")},
              "total": {key: 0 for key in ("in", "out", "cache_read", "cache_write")}}
    unknown_count = 0
    capsules = []
    for row in rows:
        usage = row.get("usage") or {}
        for bucket in ("role", "manager", "total"):
            for key in totals[bucket]:
                totals[bucket][key] += int((usage.get(bucket) or {}).get(key) or 0)
        unknown_count += int(usage.get("unknown_count") or 0)
        capsules.append({
            "capsule_id": row["id"], "topic": row["topic"], "project": row["project"],
            "project_label": row["project_label"], "sync_state": row["sync_state"],
            "lifecycle": row.get("lifecycle"), "owner": row.get("owner"),
            "worklog_seconds": row.get("worklog_seconds"),
            "machine_seconds": row.get("machine_seconds"),
            "usage": usage,
        })
    return {"since": since, "until": until, "me": me, "project": project,
            "effective_seconds": aggregate.get("effective_seconds"),
            "machine_seconds": aggregate.get("machine_seconds"),
            "total_hours": aggregate.get("total_hours"),
            "by_actor": aggregate.get("by_actor"),
            "items": aggregate.get("items"), "warnings": aggregate.get("warnings"),
            "capsules": capsules,
            "totals": {"role": totals["role"], "manager": totals["manager"],
                       "total": totals["total"], "unknown_count": unknown_count}}


def _people(root: Path) -> dict[str, dict]:
    """人员主库 `{id: person}`（只读真源，M1-8 P10 身份选择）。"""
    try:
        return resolve_stakeholder.load_people_map(root)
    except Exception:  # noqa: BLE001 - 底册缺失/损坏时退化为无候选，不阻断服务
        return {}


def _settings(root: Path, me: str | None) -> dict[str, Any]:
    """P10 设置（只读）：本机身份、工作区根、偏好与诊断入口。

    只读展示，不提供写入口：`workspace-config.yaml` 的 `me` 由用户在 P10 选定后
    人工填写（方案 4.2 P10「改动按字段合并，不整体重写」），本服务不代写（AC-18：
    不新增写存储授权策略接口，也不新增任何配置文件写路由）。
    """
    people = _people(root)
    opener = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "file-opener.json"
    return {
        "me": me,
        "me_missing": me is None,
        "workspace_root": str(root),
        "people": [{"id": pid, "name": str(p.get("name") or pid),
                    "aliases": [str(a) for a in (p.get("aliases") or [])]}
                   for pid, p in sorted(people.items())],
        "preferences": {
            "openers_config": str(opener),
            "openers_present": opener.is_file(),
            "languages": ["zh-CN"],
            "themes": ["dark", "light", "system"],
        },
        "diagnostics": {
            "logs_dir": str(root / paths.SYSTEM_DIRNAME / "data" / "logs"),
            "export": "由用户手动打包 data/logs/（本服务不导出、不外发）",
        },
        "links": {
            "rules": ".entropaxis/rules/",
            "tools": ".entropaxis/tools/",
            "skills": ".entropaxis/skills/",
        },
    }


def _executors(root: Path) -> dict[str, Any]:
    """P8 Agent 执行端（只读）：本机 roles.yaml 承载链 + 每角色默认执行端。

    偏好链、超时与硬门禁锁全部来自 `setup_agents.list_roles()`（第 3 级 roles.yaml
    的唯一读者），本函数不复制解析逻辑。默认执行端 = 每角色第 1 偏好（P2 显式选择
    优先于它，Q-8）。**不提供任何改 roles.yaml 的写入口**：roles.yaml 影响全部调度，
    只能由用户明确操作（`rules/角色协作.md:48`）。
    """
    roles_path = paths.DATA_DIR / "templates" / "roles.yaml"
    payload: dict[str, Any] = {
        "roles_config": str(roles_path),
        "present": roles_path.is_file(),
        "default_timeout_s": role_preferences.FALLBACK_TIMEOUT_S,
        "roles": [],
        "error": "",
        "executors": [{"id": "local", "name": "本机", "kind": "local", "online": True}],
        "writable": False,
        "writable_hint": "修改 roles.yaml 会影响全部调度，请直接编辑该文件（本页只读）",
    }
    try:
        import setup_agents
        data = setup_agents.load_config(roles_path)
        payload["default_timeout_s"] = data.get("default_timeout_s",
                                                role_preferences.FALLBACK_TIMEOUT_S)
        payload["roles"] = setup_agents.list_roles(data)
    except Exception as error:  # noqa: BLE001 - 配置缺失/损坏时给说明，不阻断其它路由
        payload["error"] = str(error)
    return payload


def _audit_gates(root: Path, row: dict) -> dict | None:
    """审计门禁注入：读该胶囊的 `03_方案审计报告.md` 的 audit-state 围栏。

    缺报告、无围栏或解析失败一律返回 None——`pending_gates()` 据此不出「方案待确认」
    与「审计熔断」两类卡片，不推断阻断数（M1-9 已定契约）。
    """
    report = Path(row["path"]).parent / "03_方案审计报告.md"
    if not report.is_file():
        return None
    try:
        text = report.read_text(encoding="utf-8")
        state = update_audit_state.load_state(text)
    except (OSError, ValueError, update_audit_state.AuditStateError):
        return None
    issues = state.get("issues") if isinstance(state, dict) else None
    if not isinstance(issues, list):
        return None
    gate = {"phase": "plan", "issues": [i for i in issues if isinstance(i, dict)]}
    rounds = state.get("max_rounds") if isinstance(state, dict) else None
    if isinstance(rounds, int) and not isinstance(rounds, bool):
        gate["max_rounds"] = rounds
    return gate


def _raw_capsule(row: dict) -> dict:
    """把 `capsule_index()` 投影行还原成 `pending_gates()` 需要的原样胶囊。

    M0 契约把 `assignments[]` 投影成 attempts 计数、只保留 open 段；而 11.3 判据要
    原始 `attempts[].started_at/ended_at/exit_code` 与全部 `task_segments`。这里按
    `path` 重新解析该胶囊文件（复用 M0 的 `parse_capsule_file()`，不另写扫描），
    再按 `workbench_view` 的输入形状重建 `roles_manifest`；取不到时退回投影行——
    判据拿不到 attempts 就不出卡，不猜。
    """
    parsed = aggregate_worklog.parse_capsule_file(Path(row["path"]) / "capsule.yaml")
    if not parsed:
        return row
    capsule = dict(parsed)
    capsule["id"] = row.get("id") or parsed.get("id")
    capsule["lifecycle"] = row.get("lifecycle") or parsed.get("lifecycle")
    # `parse_capsule_file()` 把 assignments 摊平到顶层、段列名为 `role_segments`；
    # 判据读 `roles_manifest.assignments` 与 `task_segments`，这里改名对齐输入形状。
    capsule["roles_manifest"] = {"assignments": parsed.get("assignments") or []}
    capsule["task_segments"] = parsed.get("role_segments") or []
    return capsule


def _gates(root: Path, since: str, until: str, me: str | None,
           project: str | None) -> list[dict]:
    """「待我拍板」六类卡片（方案 11.3）：对每个「我的」胶囊跑 `pending_gates()`。

    复用 M0 的 `capsule_index()` 扫描（不另写扫描逻辑）与 M1-9 的纯函数判据；
    运行态来自 `/api/runs` 同一份 run 文件。运行全程不写文件。
    """
    runs = _runs(root)
    rows = _capsules_with_stage_bar(root, since, until, me, project, runs)
    cards: list[dict] = []
    for row in rows:
        gate = _audit_gates(root, row)
        for card in workbench_view.pending_gates(_raw_capsule(row), audit_gate=gate, runs=runs):
            cards.append({**card, "capsule_id": row["id"], "topic": row["topic"],
                          "project": row["project"], "path": row["path"]})
    return cards


def _run_capsule_id(capsule: object) -> str | None:
    """徽标用的胶囊 ID：读 `<capsule>/capsule.yaml::id`（Q-4 / AC-12）。

    只认 run 文件里 `capsule` 指向的目录。`null`、缺键、目录不存在或读不出 id
    一律返回 None（前端显示「未绑定胶囊」）——不从 cwd 或交付物路径反推。
    """
    if not capsule:
        return None
    path = Path(str(capsule)) / "capsule.yaml"
    if not path.is_file():
        return None
    parsed = aggregate_worklog.parse_capsule_file(path)
    cid = (parsed or {}).get("id")
    return str(cid).strip() or None


def _runs(root: Path) -> list[dict]:
    """扫 `data/logs/run/*.json`：运行中调度（含 capsule、preference_number、timeout_s）。

    锁目录不以 .json 结尾，不会被 glob 命中（Tech-176 T11 口径）。
    """
    run_dir = root / paths.SYSTEM_DIRNAME / "data" / "logs" / "run"
    rows: list[dict] = []
    if not run_dir.is_dir():
        return rows
    for path in sorted(run_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        row = dict(data)
        row["alive"] = _pid_alive(data.get("pid"))
        # 徽标胶囊 ID 按 Q-4 只读 capsule 指向的 capsule.yaml，缺省 None = 未绑定胶囊。
        row["capsule_id"] = _run_capsule_id(data.get("capsule"))
        row["run_file"] = str(path)
        rows.append(row)
    return rows


class ToolCallError(Exception):
    """工具转调失败：HTTP 状态码 + 面向调用方的说明 + 工具 stderr 原文 + 工具退出码。"""

    def __init__(self, status: int, message: str, detail: str = "",
                 exit_code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail
        self.exit_code = exit_code


# ---------------------------------------------------------------- 服务

class WorkbenchState:
    """服务运行期状态：工作区根、启动令牌、本机身份。令牌只驻内存，不写任何缓存文件。"""

    def __init__(self, root: Path, token: str, me: str | None) -> None:
        self.root = root
        self.token = token
        self.me = me


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "EntropaxisWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    # 静默默认访问日志：控制台噪音不进会话上下文（Silence is Golden）
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def state(self) -> WorkbenchState:
        return self.server.state  # type: ignore[attr-defined]

    # ---- 基础响应

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_detail(self, status: int, message: str, detail: str = "",
                           exit_code: int | None = None) -> None:
        payload: dict[str, Any] = {"error": message}
        if detail:
            payload["detail"] = detail
        if exit_code is not None:
            payload["exit_code"] = exit_code
        self._send_json(status, payload)

    def _authorized(self) -> bool:
        presented = self.headers.get(TOKEN_HEADER) or ""
        # 常量时间比较，避免按字符逐位时序侧信道
        return bool(presented) and secrets.compare_digest(presented, self.state.token)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1 << 20))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ToolCallError(400, "请求体不是合法 JSON", str(error))
        return payload if isinstance(payload, dict) else {}

    # ---- 静态资产（M1-3）：只读、白名单、无目录遍历

    _STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                     ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                     ".png": "image/png", ".ico": "image/x-icon"}

    def _send_static(self, route: str) -> None:
        """按白名单发静态文件。路径来自路由表常量，不来自请求体，故无遍历面。"""
        name = "index.html" if route in ("/", "/index.html") else route.lstrip("/")
        path = (STATIC_DIR / name).resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.is_file():
            self._send_error_detail(404, f"未找到静态资源 {route}")
            return
        suffix = path.suffix.lower()
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self._STATIC_TYPES.get(suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # 静态资产随版本库走，但每次启动令牌会变：禁缓存避免旧页带上旧令牌。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- 分发

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path.rstrip("/") or "/"
        # 静态资产在令牌边界之前放行：入口页必须能取到自己的 CSS/JS 才能带上令牌请求 API。
        # 只读、只服务 STATIC_ROUTES 白名单内的已知文件，不做目录遍历。
        if route in STATIC_ROUTES:
            self._send_static(route)
            return
        if not self._authorized():
            self._send_error_detail(401, f"缺少或错误的 {TOKEN_HEADER} 启动令牌")
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._dispatch_get(route, query)
        except ToolCallError as error:
            self._send_error_detail(error.status, error.message, error.detail, error.exit_code)
        except BrokenPipeError:  # 客户端提前断开
            return
        except Exception as error:  # noqa: BLE001 - HTTP 边界统一报告，不伪装成功
            self._send_error_detail(500, f"服务内部错误: {error}")

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        if not self._authorized():
            self._send_error_detail(401, f"缺少或错误的 {TOKEN_HEADER} 启动令牌")
            return
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path.rstrip("/") or "/"
        try:
            body = self._read_json_body()
        except ToolCallError as error:
            self._send_error_detail(error.status, error.message, error.detail, error.exit_code)
            return
        try:
            self._dispatch_post(route, body)
        except ToolCallError as error:
            self._send_error_detail(error.status, error.message, error.detail, error.exit_code)
        except BrokenPipeError:  # 客户端提前断开
            return
        except Exception as error:  # noqa: BLE001 - HTTP 边界统一报告，不伪装成功
            self._send_error_detail(500, f"服务内部错误: {error}")

    # ---- GET 路由

    def _dispatch_get(self, route: str, query: dict[str, list[str]]) -> None:
        root = self.state.root
        since = (query.get("since") or [""])[0] or _default_since()
        until = (query.get("until") or [""])[0] or _today()
        me = (query.get("me") or [""])[0] or self.state.me
        project = (query.get("project") or [""])[0] or None

        if route == "/api/capsules":
            self._send_json(200, _capsules_with_stage_bar(
                root, since, until, me, project, _runs(root)))
            return
        if route.startswith("/api/capsules/"):
            capsule_id = urllib.parse.unquote(route[len("/api/capsules/"):]).strip("/")
            if not capsule_id:
                self._send_error_detail(400, "缺少胶囊 ID")
                return
            rows = _capsules_with_stage_bar(root, since, until, me, project, _runs(root))
            row = next((r for r in rows if str(r.get("id")) == capsule_id), None)
            if row is None:
                self._send_error_detail(404, f"未找到胶囊 {capsule_id}")
                return
            self._send_json(200, row)
            return
        if route == "/api/todos":
            self._send_json(200, _todo_index(root, me))
            return
        if route == "/api/worklog":
            self._send_json(200, _aggregate(root, since, until))
            return
        if route == "/api/worklog-usage":
            self._send_json(200, _usage_view(root, since, until, me, project))
            return
        if route == "/api/resume":
            self._send_json(200, _resume(root))
            return
        if route == "/api/find":
            terms = query.get("q") or []
            if not terms or not terms[0].strip():
                self._send_error_detail(400, "缺少检索词：用 /api/find?q=<主题词>")
                return
            self._send_json(200, _find(root, terms[0].strip()))
            return
        if route == "/api/projects":
            self._send_json(200, _projects(root))
            return
        if route == "/api/runs":
            self._send_json(200, _runs(root))
            return
        if route == "/api/gates":
            self._send_json(200, _gates(root, since, until, me, project))
            return
        if route == "/api/settings":
            self._send_json(200, _settings(root, me))
            return
        if route == "/api/executors":
            self._send_json(200, _executors(root))
            return
        self._send_error_detail(404, f"未知路由 {route}")

    # ---- POST 路由：只转调现有写工具

    def _dispatch_post(self, route: str, body: dict[str, Any]) -> None:
        root = self.state.root

        if route == "/api/capsules":
            self._post_create_capsule(root, body)
            return
        if route == "/api/segments/open":
            self._post_segment(root, body, opening=True)
            return
        if route == "/api/segments/end":
            self._post_segment(root, body, opening=False)
            return
        if route == "/api/worklog":
            self._post_log_time(root, body)
            return
        if route == "/api/files/open":
            self._post_open_file(root, body)
            return
        self._send_error_detail(404, f"未知路由 {route}")

    @staticmethod
    def _require(body: dict[str, Any], key: str) -> str:
        value = body.get(key)
        if value is None or not str(value).strip():
            raise ToolCallError(400, f"缺少必填字段 {key}")
        return str(value).strip()

    def _post_create_capsule(self, root: Path, body: dict[str, Any]) -> None:
        target = self._require(body, "target_dir")
        topic = self._require(body, "topic")
        argv = [_tool("init_capsule.py"), target, topic]
        if body.get("id"):
            argv += ["--id", self._require(body, "id")]
        if body.get("mode"):
            argv += ["--mode", self._require(body, "mode")]
        result = self._call_write_tool(argv)
        self._send_json(200, {"ok": True, "tool": "init_capsule.py",
                              "stdout": result["stdout"], "exit_code": result["exit_code"]})

    def _post_segment(self, root: Path, body: dict[str, Any], *, opening: bool) -> None:
        capsule = self._require(body, "capsule")
        argv = [_tool("update_capsule.py"), capsule]
        if opening:
            name = self._require(body, "name")
            argv += ["--start-task", "--name", name]
            if body.get("task_id"):
                argv += ["--task-id", self._require(body, "task_id")]
            if body.get("stage"):
                argv += ["--stage", self._require(body, "stage")]
            if body.get("at"):
                argv += ["--at", self._require(body, "at")]
        else:
            argv += ["--end-task"]
            if body.get("evidence"):
                argv += ["--evidence", self._require(body, "evidence")]
            if body.get("trigger"):
                argv += ["--trigger", self._require(body, "trigger")]
            if body.get("at"):
                argv += ["--at", self._require(body, "at")]
            if body.get("inferred"):
                argv += ["--inferred"]
            if body.get("reason"):
                argv += ["--reason", self._require(body, "reason")]
        result = self._call_write_tool(argv)
        self._send_json(200, {"ok": True, "tool": "update_capsule.py",
                              "stdout": result["stdout"], "exit_code": result["exit_code"]})

    def _post_log_time(self, root: Path, body: dict[str, Any]) -> None:
        capsule = self._require(body, "capsule")
        spent = self._require(body, "spent")
        argv = [_tool("update_capsule.py"), capsule, "--log-time", spent]
        for key, flag in (("stage", "--stage"), ("summary", "--summary"),
                          ("operator", "--operator"), ("date", "--date")):
            if body.get(key):
                argv += [flag, self._require(body, key)]
        if body.get("idempotent"):
            argv += ["--idempotent"]
        result = self._call_write_tool(argv)
        self._send_json(200, {"ok": True, "tool": "update_capsule.py",
                              "stdout": result["stdout"], "exit_code": result["exit_code"]})

    def _post_open_file(self, root: Path, body: dict[str, Any]) -> None:
        raw = body.get("path")
        paths = [str(raw)] if isinstance(raw, str) and raw.strip() else []
        if isinstance(raw, list):
            paths = [str(p) for p in raw if str(p).strip()]
        if not paths:
            raise ToolCallError(400, "缺少必填字段 path")
        argv = [_tool("open_file.py"), *paths]
        if body.get("on_deliver"):
            argv += ["--on-deliver"]
        result = self._call_write_tool(argv)
        self._send_json(200, {"ok": result["exit_code"] == 0, "tool": "open_file.py",
                              "stdout": result["stdout"], "stderr": result["stderr"],
                              "exit_code": result["exit_code"]})

    @staticmethod
    def _call_write_tool(argv: list[str]) -> dict[str, Any]:
        """写路由统一出口：原样透出退出码与 stderr；失败即失败，不吞异常、不伪装成功。

        工具返回非零时抛 ToolCallError(502)：HTTP 状态码不伪装成功，stderr 原文进 `detail`。
        """
        try:
            proc = subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                                  timeout=SUBPROCESS_TIMEOUT_S, cwd=str(TOOLS_DIR.parent))
        except subprocess.TimeoutExpired as error:
            detail = error.stderr if isinstance(error.stderr, str) else ""
            raise ToolCallError(504, f"工具调用超时（>{SUBPROCESS_TIMEOUT_S}s）: {argv[-1]}", detail)
        except FileNotFoundError as error:
            raise ToolCallError(500, f"无法启动工具: {argv[0]}", str(error))
        if proc.returncode != 0:
            raise ToolCallError(502, f"工具返回非零退出码 {proc.returncode}: {argv[-1]}",
                                (proc.stderr or proc.stdout or "").strip(),
                                exit_code=proc.returncode)
        return {"exit_code": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}


def _default_since() -> str:
    """缺省本周一：固定 +08:00 的 ISO 周一（Q-11 / D-16），不取本机时区。"""
    return workbench_view.week_since()


def _today() -> str:
    return workbench_view.today_cst()


def _read_me(root: Path) -> str | None:
    """本机人员 id：`--me` > `workspace-config.yaml::me`；都没有返回 None（首页引导绑定）。"""
    config = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "workspace-config.yaml"
    if not config.is_file():
        return None
    try:
        import yaml
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 配置损坏不阻断服务启动，只是没有本机身份
        return None
    me = data.get("me") if isinstance(data, dict) else None
    if me is None:
        return None
    return str(me).strip() or None


def build_server(root: Path, port: int = 0, me: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    """绑回环地址 + 指定（或随机）端口，生成启动令牌。返回 (server, token)。

    绑定地址按 IP 字面量构造（不用回环主机名）：该主机名在某些环境解析到 IPv6
    `::1`，而 IPv4 客户端连不上，令牌鉴权的边界会出现「本机也连不上」的假故障。
    """
    server = ThreadingHTTPServer((LOOPBACK_IPV4, port), WorkbenchHandler)
    server.state = WorkbenchState(root, secrets.token_urlsafe(32), me)  # type: ignore[attr-defined]
    return server, server.state.token  # type: ignore[attr-defined]


def _token_path(root: Path, instance: str | None = None) -> Path:
    """启动令牌落盘路径：`data/workbench/token`（Spec 1.2、IA-4）。

    同一工作区可同时起多个服务实例（测试里的 smoke/p2/p6 用例各起一个，真实使用也会
    先后开两个窗口）：固定路径会被后启动的实例覆盖，也会被先退出的实例删掉。因此
    默认文件名带实例标识（pid + 启动随机数），每个实例只写、只删自己那一份；
    向后兼容读取方按 `token` / `token.*` 前缀取最新一份。`instance=None` 时返回目录本身，
    供枚举使用。
    """
    directory = Path(root) / paths.SYSTEM_DIRNAME / "data" / "workbench"
    if instance is None:
        return directory
    return directory / f"token.{instance}"


def _instance_tag() -> str:
    """本实例标识：pid + 进程内随机数（同一 pid 复用进程时也不撞）。"""
    return f"{os.getpid()}.{secrets.token_hex(4)}"


def _write_token_file(root: Path, token: str, instance: str) -> bool:
    """把启动令牌写入 `data/workbench/token.<实例标识>`，权限 0600；写失败不阻断启动。

    原子替换（临时文件 + `os.replace`）：并发实例即使枚举到同一前缀也不会读到半行；
    创建即 0600，不让令牌以默认权限在磁盘上暴露。
    """
    path = _token_path(root, instance)
    staging = path.with_name(f"{path.name}.{os.getpid()}.writing")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
        os.replace(staging, path)
    except OSError as error:
        print(f"⚠️ 令牌文件写入失败（不影响本次启动）: {error}", file=sys.stderr, flush=True)
        try:
            staging.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不影响启动
            pass
        return False
    return True


def _remove_token_file(root: Path, instance: str) -> None:
    """服务退出即删自己那一份令牌文件：不在磁盘上留下过期凭据，也不碰其它实例的。"""
    try:
        _token_path(root, instance).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 退出清理失败不影响退出码
        pass


def _install_sigterm_cleanup(root: Path, instance: str) -> None:
    """SIGTERM：同步删本实例令牌文件后退出（IA-4）。

    在 `main()` 入口就注册（服务起来前）：启动早期收到 SIGTERM 时缺省动作会直接杀进程，
    令牌文件残留在磁盘上。清理不依赖异常传播（`serve_forever` 可能长时间阻塞在
    `select`，`SystemExit` 要等它返回才展开），因此 handler 里同步删文件再退出。
    只删本实例那一份：同工作区的其它服务实例令牌不受影响。
    仅主线程可注册；非主线程（被 import 的服务场景）静默跳过。
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _on_sigterm(*_: object) -> None:
        _remove_token_file(root, instance)
        # 直接退出：不依赖 `serve_forever` 的 poll 周期，也不在 handler 里调 `shutdown()`
        # （它会等服务循环自行退出，等的是主线程手里的 select）。
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)


def serve(root: Path, port: int, me: str | None, open_browser: bool = True) -> int:
    server, token = build_server(root, port, me)
    host, actual_port = server.server_address[:2]
    instance = _instance_tag()
    # 启动信息只走 stderr：stdout 留给工具语义输出；首行是令牌，前端据此带头发起请求。
    print(f"token {token}", file=sys.stderr, flush=True)
    print(f"listening http://{host}:{actual_port}/", file=sys.stderr, flush=True)
    _write_token_file(root, token, instance)
    # SIGTERM 清理已在 main() 入口注册；这里只需装好退出路径（serve() 也可被直接调用）。
    _install_sigterm_cleanup(root, instance)
    if open_browser:
        # 打开动作统一走 open_file.py（《文件交付》4.4）：平台命令只允许出现在该文件内。
        subprocess.run([sys.executable, str(TOOLS_DIR / "open_file.py"),
                        f"http://{host}:{actual_port}/"], capture_output=True, text=True,
                       timeout=SUBPROCESS_TIMEOUT_S)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _remove_token_file(root, instance)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Entropaxis 团队协作工作台本地服务（Tech-175 M1）：只读路由转调只读工具，"
                    "写路由只转调现有写工具；不写存储授权策略（AC-18）。")
    parser.add_argument("--port", type=int, default=0,
                        help="监听端口；0 = 随机空闲端口（默认）")
    parser.add_argument("--no-open", action="store_true",
                        help="不自动打开浏览器（M1 前端未就绪时恒传）")
    parser.add_argument("--me", help="本机人员 id（stakeholders.yaml::people[].id）；"
                                     "缺省读 data/templates/workspace-config.yaml::me")
    parser.add_argument("--root", type=Path, default=paths.WORKSPACE_ROOT, help="工作区根目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    if not (root / paths.SYSTEM_DIRNAME).is_dir():
        print(f"❌ 工作区定位失败: {root} 下没有 {paths.SYSTEM_DIRNAME}/\n"
              "👉 修复建议: 用 --root 指向含 .entropaxis 的工作区根。", file=sys.stderr)
        return 1
    # 令牌文件可能在 serve() 里写出：启动早期就装好 SIGTERM 清理，避免该窗口内被杀留下令牌。
    _install_sigterm_cleanup(root, _instance_tag())
    try:
        return serve(root, args.port, args.me or _read_me(root), open_browser=not args.no_open)
    except OSError as error:
        print(f"❌ 端口绑定失败: {error}\n👉 修复建议: 换一个 --port，或确认该端口未被占用。",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
