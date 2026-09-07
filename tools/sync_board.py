#!/usr/bin/env python3
"""开发项目任务与外部看板联动同步工具。

以 task ID 为跨系统主键，把一个开发任务幂等地同步到配置的外部看板。
协议与状态映射见 .system/rules/看板联动.md。零依赖，仅用标准库。
看板 Provider CLI 由工作区实例声明 .data/templates/board_config.json 外置（角色 main/dev），
本工具零具体系统名；未声明或 CLI 不可用时自动降级为纯本地。

Provider 四态结果契约（ProviderResult）：找到/不存在/失败/结果未知，见看板联动.md「Provider 四态结果契约」。
"""
import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ponytail: 角色固定为 main/dev 两个，命令语法随所选 CLI；更换异语法看板时需在 Provider 声明层扩展适配
PROVIDERS_FILE = Path(__file__).resolve().parent.parent.parent / ".data" / "templates" / "board_config.json"

# 单一 --status 输入 → (dev 看板状态, 主看板 stage；stage=None 表示不进主看板)
STATUS_MAP = {
    "plan":        ("todo",        "plan"),
    "todo":        ("todo",        "plan"),
    "active":      ("in_progress", "active"),
    "in_progress": ("in_progress", "active"),
    "review":      ("in_review",   "review"),
    "in_review":   ("in_review",   "review"),
    "done":        ("done",        "done"),
    "backlog":     ("backlog",     None),
}
# 别名 → 本地真源规范键：规范化只作用于本地真源，外部端映射（STATUS_MAP 的输出侧）不变。
ALIAS_TO_CANONICAL = {"todo": "plan", "in_progress": "active", "in_review": "review"}
# markdown 里的 emoji 状态便捷别名
EMOJI = {"📋": "plan", "🔧": "active", "✅": "done", "⏸": "backlog"}


def norm_status(s):
    """校验并返回本地真源规范键：如 in_progress → active。外部端仍按 STATUS_MAP 输出各自原值。"""
    s = EMOJI.get((s or "").strip(), (s or "").strip()).lower()
    if s not in STATUS_MAP:
        raise SystemExit(f"未知 status: {s!r}；可选 {list(STATUS_MAP)} 或 emoji {list(EMOJI)}")
    return ALIAS_TO_CANONICAL.get(s, s)


@dataclass
class ProviderResult:
    """Provider 四态结构化结果：找到/不存在/失败/结果未知。"""
    status: str  # FOUND | NOT_FOUND | FAILED | UNKNOWN
    id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False
    cursor: Optional[str] = None


def _parse_fm(text):
    """极简 YAML front matter 解析：取扁平 `key: value`。返回 dict；无档头返回 {}。
    # ponytail: 只解析扁平 kv，嵌套/多行值到时再说
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def read_frontmatter(path):
    return _parse_fm(Path(path).read_text(encoding="utf-8"))


def load_providers():
    """读取工作区级看板 Provider 声明（.data/templates/board_config.json），返回 {role: {"cli": [...]}}。
    声明缺失或损坏时返回 {}，全部联动自动降级为纯本地。"""
    try:
        d = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        provs = d.get("providers", d)
        return provs if isinstance(provs, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _provider_cli(role):
    spec = load_providers().get(role)
    cli = spec.get("cli") if isinstance(spec, dict) else None
    return [str(c) for c in cli] if isinstance(cli, list) and cli else None


def load_board(project_dir):
    p = Path(project_dir) / "docs" / ".board.json"
    if not p.exists():
        raise SystemExit(f"缺少 {p}（见 看板联动.md）")
    d = json.loads(p.read_text(encoding="utf-8"))
    boards = d.get("boards", {}) if isinstance(d.get("boards"), dict) else {}
    main_id = boards.get("main") or d.get("main_project")
    dev_id = boards.get("dev") or d.get("dev_project")
    ns = d.get("ns") or "Default"
    return {
        "ns": ns,
        "main_id": main_id,
        "dev_id": dev_id,
        "boards": boards,
        "raw": d,
    }


def _run_role_raw(role, args, cwd, timeout=60):
    """按角色执行已声明 Provider CLI，返回原始 CompletedProcess 或 None（未声明/异常）。

    退出码/异常到状态的映射（见 看板联动.md「Provider 四态结果契约」）由调用方
    （find_issue/find_todo_by_source_ref/upsert_*）翻译为 ProviderResult：
      未声明 Provider 或 OSError/TimeoutExpired → 此处返回 None，调用方归 FAILED；
      退出码非 0 → FAILED；退出码 0 但非法 JSON → FAILED；退出码 0 且解析成功 → FOUND/NOT_FOUND。
    """
    cli = _provider_cli(role)
    if not cli:
        return None
    try:
        return subprocess.run([*cli, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_issue(mp, task, cwd) -> ProviderResult:
    """按稳定回读键（Dev 端：issue 标题前缀 [<TaskID>]）查找，遍历 cursor 直到耗尽。"""
    r = _run_role_raw("dev", ["issue", "list", "--project", mp, "--output", "json"], cwd, 30)
    if r is None:
        return ProviderResult(status="FAILED", error="dev Provider 未声明或不可执行", retryable=False)
    if r.returncode != 0:
        return ProviderResult(status="FAILED", error=(r.stderr or "").strip(), retryable=False)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        return ProviderResult(status="FAILED", error=f"非法 JSON: {exc}", retryable=False)
    issues = data.get("issues", []) if isinstance(data, dict) else data
    pat = re.compile(r"^\[" + re.escape(task) + r"\]")
    cursor = data.get("cursor") if isinstance(data, dict) else None
    while True:
        for it in issues or []:
            if it and pat.match(it.get("title") or ""):
                return ProviderResult(status="FOUND", id=it.get("id"))
        if not cursor:
            break
        # 分页遍历直到耗尽：当前 Dev Provider 未返回 cursor，循环天然只跑一轮；
        # ponytail: Provider 支持分页后在此补 --cursor 传参重跑，无需改调用方
        break
    return ProviderResult(status="NOT_FOUND")


def upsert_dev(bd, project_dir, task, title, mstatus, spec):
    mp = bd.get("dev_id")
    if not mp:
        return None, ProviderResult(status="FAILED", error="dev_id 未配置", retryable=False)
    cwd = str(project_dir)
    args = ["--title", f"[{task}] {title}"]
    if spec:
        if not (Path(project_dir) / spec).exists():
            raise SystemExit(f"spec 不存在：{Path(project_dir) / spec}")
        args += ["--description-file", spec]
    found = find_issue(mp, task, cwd)
    if found.status == "FAILED":
        return None, found  # 查询失败不得进入创建分支
    if found.status == "FOUND":
        iid = found.id
        r = _run_role_raw("dev", ["issue", "update", iid, *args], cwd, 60)
        if r is None or r.returncode != 0:
            return iid, ProviderResult(status="FAILED", error="更新失败", retryable=True)
    else:  # NOT_FOUND：唯一允许创建的状态
        r = _run_role_raw("dev", ["issue", "create", "--project", mp, *args, "--output", "json"], cwd, 60)
        if r is None or r.returncode != 0 or not r.stdout:
            return None, ProviderResult(status="FAILED", error="创建失败", retryable=True)
        try:
            iid = json.loads(r.stdout).get("id")
        except json.JSONDecodeError:
            return None, ProviderResult(status="FAILED", error="创建响应非法 JSON", retryable=False)
    if iid:
        _run_role_raw("dev", ["issue", "status", iid, mstatus], cwd)
    return iid, ProviderResult(status="FOUND" if iid else "UNKNOWN", id=iid)


def find_todo_by_source_ref(dp, source_ref, cwd) -> ProviderResult:
    """按稳定回读键（Main 端：source_ref）查找。"""
    r = _run_role_raw("main", ["todo", "list", "--project", dp, "--source-ref", source_ref], cwd, 30)
    if r is None:
        return ProviderResult(status="FAILED", error="main Provider 未声明或不可执行", retryable=False)
    if r.returncode != 0:
        return ProviderResult(status="FAILED", error=(r.stderr or "").strip(), retryable=False)
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as exc:
        return ProviderResult(status="FAILED", error=f"非法 JSON: {exc}", retryable=False)
    items = data if isinstance(data, list) else data.get("items", [])
    if items:
        return ProviderResult(status="FOUND", id=items[0].get("id"))
    return ProviderResult(status="NOT_FOUND")


def upsert_todo(bd, task, title, stage, due, people, cwd=None):
    dp = bd.get("main_id")
    if not dp or stage is None:
        return None
    source_ref = f'{dp}:{bd["ns"]}:{task}'
    found = find_todo_by_source_ref(dp, source_ref, cwd)
    if found.status == "FAILED":
        return None  # 查询失败不创建
    if found.status == "FOUND":
        return found.id  # 已存在：回读即视为完成，不重复创建
    args = [
        "todo", "add", "--project", dp, "--content", f"[{task}] {title}",
        "--stage", stage, "--source-ref", source_ref,
    ]
    if due:
        args += ["--due", due]
    if people:
        args += ["--people", people]
    r = _run_role_raw("main", args, cwd, 30)
    if r is None or r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout).get("id")
    except json.JSONDecodeError:
        return None


def sync_one(bd, project_dir, row):
    spec = row.get("spec")
    fm = read_frontmatter(Path(project_dir) / spec) if spec else {}
    task = (row.get("task") or fm.get("id") or "").strip()
    title = (row.get("title") or fm.get("title") or "").strip()
    if not task:
        raise SystemExit(f"缺 task（--task 或 spec 档头 id）：{row}")
    status = norm_status(row.get("status", "plan"))
    mstatus, stage = STATUS_MAP[status]
    iid, dev_result = upsert_dev(bd, project_dir, task, title, mstatus, row.get("spec"))
    tid = upsert_todo(bd, task, title, stage, row.get("due"), row.get("people"), cwd=str(project_dir))
    dev_repr = (iid or "")[:8] if dev_result.status != "FAILED" else f"FAILED({dev_result.error})"
    print(f"{task}\tdev={dev_repr}\tmain={tid or '-'}\tstatus={status}")


def selftest():
    assert STATUS_MAP["done"] == ("done", "done")
    assert STATUS_MAP["active"] == ("in_progress", "active")
    assert STATUS_MAP["backlog"][1] is None
    assert norm_status("🔧") == "active"
    assert norm_status("✅") == "done"
    assert norm_status("in_progress") == "active"  # 本地真源规范化：别名收敛到 active
    assert STATUS_MAP["in_progress"] == ("in_progress", "active")  # 外部端映射不变
    fm = _parse_fm("---\ntype: Spec\nid: M1\ntitle: 参考文献只用 PubMed\n---\n# body\n")
    assert fm["id"] == "M1" and fm["title"] == "参考文献只用 PubMed" and fm["type"] == "Spec"
    assert _parse_fm("# no frontmatter\n") == {}
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description="开发任务与外部看板联动同步")
    ap.add_argument("project_dir", nargs="?", help="开发项目根目录（含 docs/.board.json）")
    ap.add_argument("--task", help="任务 ID，如 M1")
    ap.add_argument("--title", help="任务标题")
    ap.add_argument("--status", default="plan", help="plan/active/review/done/backlog 或 emoji")
    ap.add_argument("--spec", help="spec 文件相对路径，如 docs/20260902_主题/Spec_M1_主题方案.md")
    ap.add_argument("--due", help="截止日 YYYY-MM-DD")
    ap.add_argument("--people", help="相关方")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON 数组批量同步")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.project_dir:
        raise SystemExit("需要 project_dir")
    pdir = Path(a.project_dir).resolve()
    bd = load_board(pdir)
    if a.stdin:
        for row in json.loads(sys.stdin.read()):
            sync_one(bd, pdir, row)
    else:
        if not a.task and not a.spec:
            raise SystemExit("单任务模式需 --task/--title，或 --spec（从档头取 id/title）")
        sync_one(bd, pdir, {"task": a.task, "title": a.title, "status": a.status,
                            "spec": a.spec, "due": a.due, "people": a.people})


if __name__ == "__main__":
    main()
