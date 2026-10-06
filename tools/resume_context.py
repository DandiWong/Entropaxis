#!/usr/bin/env python3
"""
续接线索收集：无上下文收到"继续"时，一次性列出工作区内可接续的断点线索。

只读、只列事实、不推断下一步——"做到哪、下一步做什么"需要语义理解，由调用方 Agent 判断
（《指令解析》「续接指令」）。任一来源缺失即静默跳过，不报错。

来源：
  session   上一轮会话末尾的用户/助手发言（Claude Code、Codex 本机会话记录）
  dispatch  dispatch-trace.jsonl 中有开始无结束（进行中/已中断）与近期失败的调度
  capsule   近期改动的事务胶囊及其审计报告中未关闭的问题
  tasks     docs/Tasks.md 的 Active 项
  git       有未提交改动的仓库
  rawinput  非空的 RawInput/
判定：verdict=none（无线索）| unique（全部线索落在同一项目）| multiple（跨项目）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_DIR, WORKSPACE_ROOT  # noqa: E402

TRACE_LOG = DATA_DIR / "logs" / "dispatch-trace.jsonl"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".entropaxis", "Archive", "build", "dist",
             ".gradle", ".venv", "venv", ".idea"}
MAX_DEPTH = 6
TEXT_MAX = 160
# 会话记录里由工具或系统注入、不代表用户意图的发言
NOISE_PREFIXES = ("<", "# AGENTS.md", "Caveat:", "[Request interrupted")


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def _codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX] + "…"


def _texts(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text"))
    return ""


def _under(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _tail_turns(rows: list[tuple[str, str]]) -> dict:
    """最后一条有效用户发言与最后一条助手发言。"""
    out: dict = {}
    for role, text in reversed(rows):
        text = text.strip()
        if not text or text.startswith(NOISE_PREFIXES) or role in out:
            continue
        out[role] = _clip(text)
        if len(out) == 2:
            break
    return out


def claude_sessions(root: Path, since: float, exclude: str | None) -> list[dict]:
    base = _claude_dir()
    # Claude Code 把会话目录名编码为 cwd 的非字母数字字符替换成 "-"；前缀匹配即覆盖工作区下所有子目录
    prefix = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    found = []
    if not base.is_dir():
        return found
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith(prefix):
            continue
        for f in d.glob("*.jsonl"):
            if f.stem == exclude or f.stat().st_mtime < since:
                continue
            rows, cwd = [], None
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    cwd = rec.get("cwd") or cwd
                    if rec.get("type") in ("user", "assistant") and not rec.get("isMeta"):
                        rows.append((rec["type"], _texts(rec.get("message", {}).get("content"))))
            except OSError:
                continue
            if cwd and not _under(cwd, root):
                continue
            found.append({"source": "claude", "id": f.stem, "cwd": cwd, "mtime": f.stat().st_mtime,
                          **_tail_turns(rows)})
    return found


def codex_sessions(root: Path, since: float, exclude: str | None) -> list[dict]:
    base = _codex_dir()
    found = []
    if not base.is_dir():
        return found
    for f in base.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
            with f.open(encoding="utf-8") as fh:
                meta = json.loads(fh.readline()).get("payload", {})
                if not _under(meta.get("cwd", "/nonexistent"), root) or meta.get("id") == exclude:
                    continue
                rows = []
                for line in fh:
                    p = json.loads(line).get("payload", {})
                    if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                        rows.append((p["role"], _texts(p.get("content"))))
        except (OSError, ValueError):
            continue
        found.append({"source": "codex", "id": meta.get("id"), "cwd": meta.get("cwd"),
                      "mtime": f.stat().st_mtime, **_tail_turns(rows)})
    return found


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def dispatches(trace: Path, since_iso: str) -> list[dict]:
    """开始记录无对应结束记录：pid 存活=running，否则=interrupted；另报近期失败。"""
    if not trace.is_file():
        return []
    starts, ends, failed = {}, set(), []
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        did = r.get("dispatch_id")
        if r.get("event") == "start":
            starts[did] = r
            continue
        if did:
            ends.add(did)
        if r.get("failure_code") and r.get("ts", "") >= since_iso:
            failed.append({"state": "failed", "role": r.get("role"), "ts": r.get("ts"),
                           "failure_code": r["failure_code"], "deliverable": r.get("deliverable"),
                           "cwd": r.get("cwd")})
    open_ = [{"state": "running" if _pid_alive(r.get("pid")) else "interrupted", "role": r.get("role"),
              "ts": r.get("ts"), "deliverable": r.get("deliverable"), "cwd": r.get("cwd")}
             for did, r in starts.items() if did not in ends]
    return open_ + failed


def _walk(root: Path):
    """按深度上限遍历，剪掉依赖与构建目录。"""
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        p = Path(dirpath)
        has_git = ".git" in dirnames or ".git" in filenames
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and len(p.parts) - base_depth < MAX_DEPTH]
        yield p, dirnames, filenames, has_git


def _open_issues(report: Path) -> int | None:
    try:
        from update_audit_state import AuditStateError, load_state
        state = load_state(report.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError, AuditStateError):
        return None
    return sum(1 for i in state.get("issues", []) if i.get("status") == "open")


def _git_dirty(repo: Path, since: float) -> dict | None:
    try:
        branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    # 长期遗留的未提交改动不是"刚才在做"的线索：只算窗口期内动过的文件
    recent = [ln for ln in status[:200] if _mtime(repo / ln[3:].strip('"').split(" -> ")[-1]) >= since]
    return {"path": str(repo), "branch": branch, "dirty": len(recent)} if recent else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def scan_tree(root: Path, since: float) -> dict:
    caps, tasks, repos, raw = [], [], [], []
    for p, dirnames, files, has_git in _walk(root):
        if has_git:
            g = _git_dirty(p, since)
            if g:
                repos.append(g)
        if "capsule.yaml" in files:
            mtimes = [(p / f).stat().st_mtime for f in files]
            if max(mtimes) >= since:
                stages = sorted(f for f in files if re.match(r"^\d{2}_", f))
                cap = {"path": str(p), "stages": stages}
                if "05_审计报告.md" in files:
                    cap["open_issues"] = _open_issues(p / "05_审计报告.md")
                caps.append(cap)
        if p.name == "docs" and "Tasks.md" in files:
            active = _active_tasks(p / "Tasks.md")
            if active:
                tasks.append({"path": str(p / "Tasks.md"), "active": active})
        if p.name == "RawInput":
            n = len([f for f in files if not f.startswith(".")]) + len(dirnames)
            if n:
                raw.append({"path": str(p), "items": n})
    return {"capsule": caps, "tasks": tasks, "git": repos, "rawinput": raw}


def _active_tasks(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"^## Active\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        return []
    return [_clip(ln.strip()[2:]) for ln in m.group(1).splitlines() if ln.strip().startswith("- ")]


def _project_of(path: str | None, workspace: Path = WORKSPACE_ROOT) -> str | None:
    """项目 = 工作区第一层目录；与扫描根无关，扫描单个项目时不会把其子目录误判成多个项目。"""
    if not path:
        return None
    try:
        rel = Path(path).resolve().relative_to(workspace.resolve())
    except (ValueError, OSError):
        return None
    return rel.parts[0] if rel.parts else "."


def default_root(cwd: Path, workspace: Path = WORKSPACE_ROOT) -> Path:
    """cwd 在某项目内 → 该项目（工作区第一层目录）；否则 → 工作区根。"""
    try:
        rel = cwd.resolve().relative_to(workspace.resolve())
    except ValueError:
        return workspace
    return workspace / rel.parts[0] if rel.parts else workspace


def resume_context(root: Path, days: int = 7, limit: int = 3, exclude: str | None = None,
                   trace: Path = TRACE_LOG, workspace: Path = WORKSPACE_ROOT) -> dict:
    root = root.resolve()
    since = time.time() - days * 86400
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since))
    sessions = sorted(claude_sessions(root, since, exclude) + codex_sessions(root, since, exclude),
                      key=lambda s: s["mtime"], reverse=True)[:limit]
    for s in sessions:
        s["mtime"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["mtime"]))
    signals = {"session": sessions, "dispatch": dispatches(trace, since_iso)[:limit], **scan_tree(root, since)}
    for k in ("capsule", "tasks", "git", "rawinput"):
        signals[k] = signals[k][:limit]
    projects = set()
    for k, items in signals.items():
        for it in items:
            projects.add(_project_of(it.get("cwd") or it.get("path") or it.get("deliverable"), workspace))
    projects.discard(None)
    verdict = "none" if not any(signals.values()) else ("unique" if len(projects) <= 1 else "multiple")
    return {"verdict": verdict, "projects": sorted(projects), "signals": signals}


def render(res: dict) -> str:
    lines = [f"verdict={res['verdict']} projects={','.join(res['projects']) or '-'}"]
    for kind, items in res["signals"].items():
        for it in items:
            if kind == "session":
                lines.append(f"session {it['source']} {it['mtime']} cwd={it.get('cwd')} "
                             f"| user: {it.get('user', '-')} | assistant: {it.get('assistant', '-')}")
            elif kind == "dispatch":
                lines.append(f"dispatch {it['state']} {it.get('role')} {it.get('ts')} "
                             f"{it.get('failure_code') or ''} → {it.get('deliverable')}")
            elif kind == "capsule":
                extra = f" open_issues={it['open_issues']}" if "open_issues" in it else ""
                lines.append(f"capsule {it['path']} stages={','.join(it['stages'])}{extra}")
            elif kind == "tasks":
                lines.append(f"tasks {it['path']} active={' ; '.join(it['active'])}")
            elif kind == "git":
                lines.append(f"git {it['path']} branch={it['branch']} dirty={it['dirty']}")
            else:
                lines.append(f"rawinput {it['path']} items={it['items']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="续接线索收集：列出工作区内可接续的断点线索（只读）")
    parser.add_argument("--root", default=str(default_root(Path.cwd())),
                        help="扫描根目录，默认当前所在项目；在工作区根调用时为整个工作区")
    parser.add_argument("--days", type=int, default=7, help="只看最近 N 天的线索，默认 7")
    parser.add_argument("--limit", type=int, default=3, help="每类线索最多条数，默认 3")
    parser.add_argument("--exclude-session",
                        default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                        help="排除的会话 ID（默认取当前 Claude Code 会话，避免把自己当成上一轮）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"❌ 扫描根目录不存在: {root}\n👉 修复建议: 用 --root 指向工作区根目录。", file=sys.stderr)
        return 1
    if args.days < 1 or args.limit < 1:
        print("❌ --days 与 --limit 必须为正整数\n👉 修复建议: 例如 --days 7 --limit 3。", file=sys.stderr)
        return 1
    res = resume_context(root, args.days, args.limit, args.exclude_session)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
