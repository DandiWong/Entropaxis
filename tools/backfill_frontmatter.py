#!/usr/bin/env python3
"""给存量 md 文档回填 YAML front matter 档头（OKF 对齐）。

只处理能归类为 Spec / Tasks / Backlog / Requirements 的文档，其余 md（changelog、
分析稿等）一律跳过。已有 `---` 档头的文件跳过（幂等）。author/date 从 git 首次提交
派生（真实，不编造）；取不到则记 `process:backfill` + 文件 mtime。

用法：
  python3 backfill_frontmatter.py <docs_dir> --project <ns> [--dry-run]
"""
import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path


# 胶囊内新命名：04_Spec_<ID>.md 或 04_Spec_<ID>_中文主题.md，取第三段（下划线分隔）为 ID。
CAPSULE_SPEC_RE = re.compile(r"^04_Spec_(.+)$")
CAPSULE_DIR_RE = re.compile(r"^\d{8}_.+$")  # 事务胶囊容器目录：YYYYMMDD_主题
# 独立 Spec 新命名：<ID>_中文主题.md（ID 前缀体系见 文件交付.md §2.3）。
INDEPENDENT_SPEC_ID_RE = re.compile(r"^(Tech|Task|B|R|SC|M|Bug)-(\d+)_")


def classify(path: Path):
    """返回 (type, id) 或 None（不处理）。"""
    name = path.name
    stem = path.stem
    m = CAPSULE_SPEC_RE.match(stem)
    if m and CAPSULE_DIR_RE.match(path.parent.name):
        # 父目录必须是 YYYYMMDD_主题 胶囊容器，否则任意位置的 04_Spec_*.md 都会被误判
        # 为 Spec（第 4 轮外置复核指出的过宽正则）。
        id_ = m.group(1).split("_", 1)[0]
        return ("Spec", id_) if id_ else None
    if name.startswith("Spec_"):
        parts = stem.split("_", 2)
        if len(parts) >= 3 and parts[1]:
            return "Spec", parts[1]
        return None
    if path.parent.name == "specs" or "/specs/" in str(path):
        dated = re.match(r"^\d{8}_([^_]+)_[A-Za-z0-9]+$", stem)  # 旧带日期格式兼容
        if dated:
            return "Spec", dated.group(1)
        if "_" in stem:  # TaskID_PascalCaseTopic → TaskID（TaskID 可含连字符，如 Bug-28）
            return "Spec", stem.split("_")[0]
        return "Spec", stem.split("-")[0]  # 旧 kebab 命名兼容
    m = INDEPENDENT_SPEC_ID_RE.match(stem)
    if m:
        return "Spec", f"{m.group(1)}-{m.group(2)}"
    if name.lower() == "tasks.md":
        return "Tasks", "tasks"
    if name == "todo.md":
        return "Backlog", "todo"
    m = re.match(r"requirements[-_]?(.*)\.md$", name)
    if m:
        return "Requirements", (m.group(1) or "requirements")
    return None


def h1_title(text: str):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def git_author_date(path: Path):
    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%an|%ad", "--date=short", "--", path.name],
            cwd=path.parent, capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
        if out:
            author, date = out[-1].split("|", 1)   # 最后一行 = 首次新增
            return f"human:{author.strip()}", date.strip()
    except Exception:
        pass
    mtime = dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    return "process:backfill", mtime


def build_header(path, ptype, pid, project):
    text = path.read_text(encoding="utf-8")
    title = h1_title(text) or path.stem
    # 去掉与 id 重复的前导（"M1 · 参考文献…" -> "参考文献…"），避免 issue 标题 [M1] M1 · …
    title = re.sub(r"^" + re.escape(pid) + r"\s*[·.:：、\-]\s*", "", title).strip() or title
    by, at = git_author_date(path)
    lines = [
        "---",
        f"type: {ptype}",
        f"id: {pid}",
        f"title: {title}",
        f"project: {project}",
        f"generated: {{ by: {by}, at: {at} }}",
        "---",
        "",
    ]
    return "\n".join(lines), text


def main():
    ap = argparse.ArgumentParser(description="回填 md 档头（OKF 对齐 YAML front matter）")
    ap.add_argument("docs_dir")
    ap.add_argument("--project", required=True, help="项目命名空间，= .board.json 的 ns")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = Path(a.docs_dir).resolve()
    changed = skipped = 0
    for path in sorted(root.rglob("*.md")):
        kind = classify(path)
        if not kind:
            continue
        if path.read_text(encoding="utf-8").startswith("---"):
            skipped += 1
            continue
        ptype, pid = kind
        header, body = build_header(path, ptype, pid, a.project)
        rel = path.relative_to(root)
        if a.dry_run:
            print(f"[会加头] {rel}  ->  type={ptype} id={pid}")
        else:
            path.write_text(header + body, encoding="utf-8")
            print(f"[已加头] {rel}  ->  type={ptype} id={pid}")
        changed += 1
    print(f"\n合计：{changed} 个{'待' if a.dry_run else '已'}加头，{skipped} 个已有头跳过。")


if __name__ == "__main__":
    main()
