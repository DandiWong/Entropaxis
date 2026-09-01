#!/usr/bin/env python3
"""开发项目任务与外部看板联动同步工具。

以 task ID 为跨系统主键，把一个开发任务幂等地同步到配置的外部看板。
协议与状态映射见 .system/rules/任务看板联动.md。零依赖，仅用标准库。
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DASHBOARD = "http://127.0.0.1:8799"

# 单一 --status 输入 → (board-platform status, 看板 stage；stage=None 表示不进看板)
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
# markdown 里的 emoji 状态便捷别名
EMOJI = {"📋": "plan", "🔧": "active", "✅": "done", "⏸": "backlog"}


def norm_status(s):
    s = EMOJI.get((s or "").strip(), (s or "").strip()).lower()
    if s not in STATUS_MAP:
        raise SystemExit(f"未知 status: {s!r}；可选 {list(STATUS_MAP)} 或 emoji {list(EMOJI)}")
    return s


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


def load_board(project_dir):
    p = Path(project_dir) / "docs" / ".board.json"
    if not p.exists():
        raise SystemExit(f"缺少 {p}（见 任务看板联动.md）")
    d = json.loads(p.read_text(encoding="utf-8"))
    boards = d.get("boards", {}) if isinstance(d.get("boards"), dict) else {}
    main_id = boards.get("main") or d.get("dashboard_project") or d.get("main_project")
    dev_id = boards.get("dev") or d.get("board-platform_project") or d.get("dev_project")
    ns = d.get("ns") or "Default"
    return {
        "ns": ns,
        "dashboard_project": main_id,
        "board-platform_project": dev_id,
        "boards": boards,
        "raw": d,
    }

def mult(args, cwd):
    try:
        r = subprocess.run(["board-platform", *args], cwd=cwd, capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return r.stdout
    except FileNotFoundError:
        return None

def find_issue(mp, task, cwd):
    data = json.loads(mult(["issue", "list", "--project", mp, "--output", "json"], cwd) or "{}")
    issues = data.get("issues", []) if isinstance(data, dict) else data
    pat = re.compile(r"^\[" + re.escape(task) + r"\]")
    for it in issues or []:
        if it and pat.match(it.get("title") or ""):
            return it.get("id")
    return None
    # ponytail: issue list 取单页，单项目 issue 超一页再加分页


def upsert_board-platform(bd, project_dir, task, title, mstatus, spec):
    mp = bd.get("board-platform_project")
    if not mp:
        return None
    cwd = str(project_dir)
    args = ["--title", f"[{task}] {title}"]
    if spec:
        if not (Path(project_dir) / spec).exists():
            raise SystemExit(f"spec 不存在：{Path(project_dir) / spec}")
        args += ["--description-file", spec]
    try:
        iid = find_issue(mp, task, cwd)
        if iid:
            mult(["issue", "update", iid, *args], cwd)
        else:
            out = mult(["issue", "create", "--project", mp, *args, "--output", "json"], cwd)
            if not out:
                return None
            iid = json.loads(out).get("id")
        if iid:
            mult(["issue", "status", iid, mstatus], cwd)
        return iid
    except Exception:
        return None

def upsert_todo(bd, task, title, stage, due, people):
    dp = bd.get("dashboard_project")
    if not dp or stage is None:
        return None
    body = {
        "project_id": dp,
        "stage": stage,
        "content": f"[{task}] {title}",
        "source_ref": f'{dp}:{bd["ns"]}:{task}',
    }
    if due:
        body["due_date"] = due
    if people:
        body["people"] = people
    req = urllib.request.Request(
        f"{DASHBOARD}/api/todos",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read().decode("utf-8"))
            return res.get("id") or (res.get("todo") or {}).get("id")
    except Exception:
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
    iid = upsert_board-platform(bd, project_dir, task, title, mstatus, row.get("spec"))
    tid = upsert_todo(bd, task, title, stage, row.get("due"), row.get("people"))
    print(f"{task}\tboard-platform={(iid or '')[:8]}\ttodo={tid or '-'}\tstatus={status}")


def selftest():
    assert STATUS_MAP["done"] == ("done", "done")
    assert STATUS_MAP["active"] == ("in_progress", "active")
    assert STATUS_MAP["backlog"][1] is None
    assert norm_status("🔧") == "active"
    assert norm_status("✅") == "done"
    assert norm_status("in_progress") == "in_progress"
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
    ap.add_argument("--spec", help="spec 文件相对路径，如 docs/04_architecture/specs/M1_PubMedOnly.md")
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
