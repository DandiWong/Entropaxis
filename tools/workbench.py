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
    from . import workbench_view
except ImportError:  # 脚本方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths  # type: ignore
    import project_registry  # type: ignore
    import workbench_view  # type: ignore

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
               "/api/resume", "/api/find", "/api/projects", "/api/runs")
WRITE_ROUTES = ("/api/capsules", "/api/segments/open", "/api/segments/end",
                "/api/worklog", "/api/files/open")


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
        rows.append(row)
    return rows


def _pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError, OSError):
        return False


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

    # ---- 分发

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        if not self._authorized():
            self._send_error_detail(401, f"缺少或错误的 {TOKEN_HEADER} 启动令牌")
            return
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path.rstrip("/") or "/"
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
            self._send_json(200, _capsule_index(root, since, until, me, project))
            return
        if route.startswith("/api/capsules/"):
            capsule_id = urllib.parse.unquote(route[len("/api/capsules/"):]).strip("/")
            if not capsule_id:
                self._send_error_detail(400, "缺少胶囊 ID")
                return
            rows = _capsule_index(root, since, until, me, project)
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
    return str(me).strip() or None


def build_server(root: Path, port: int = 0, me: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    """绑回环地址 + 指定（或随机）端口，生成启动令牌。返回 (server, token)。

    绑定地址按 IP 字面量构造（不用回环主机名）：该主机名在某些环境解析到 IPv6
    `::1`，而 IPv4 客户端连不上，令牌鉴权的边界会出现「本机也连不上」的假故障。
    """
    server = ThreadingHTTPServer((LOOPBACK_IPV4, port), WorkbenchHandler)
    server.state = WorkbenchState(root, secrets.token_urlsafe(32), me)  # type: ignore[attr-defined]
    return server, server.state.token  # type: ignore[attr-defined]


def serve(root: Path, port: int, me: str | None, open_browser: bool = True) -> int:
    server, token = build_server(root, port, me)
    host, actual_port = server.server_address[:2]
    # 启动信息只走 stderr：stdout 留给工具语义输出；首行是令牌，前端据此带头发起请求。
    print(f"token {token}", file=sys.stderr, flush=True)
    print(f"listening http://{host}:{actual_port}/", file=sys.stderr, flush=True)
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
    try:
        return serve(root, args.port, args.me or _read_me(root), open_browser=not args.no_open)
    except OSError as error:
        print(f"❌ 端口绑定失败: {error}\n👉 修复建议: 换一个 --port，或确认该端口未被占用。",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
