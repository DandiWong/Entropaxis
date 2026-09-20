#!/usr/bin/env python3
"""迭代收尾的履历记录：Changelog 追加 + Tasks 勾选，一次调用两处定点写入。

真源定义: rules/软件工程.md「研发迭代与方案审修闭环」§3.4-3.6 与「Changelog 规范」
          rules/治理指令.md「新迭代 / 新 Feature」第 3 步

存在的理由（《工具设计》信号 3 模型易幻觉盲区 + output token）：
    收尾要在 Changelog 的 `## [Unreleased]` 下找到**正确的**分类小节插入条目（6 大分类）、
    写 ISO 日期、补 `([Task-ID])` 后缀，再去 Tasks.md 把对应行的复选框翻成 `[x]`。这些都是
    字符级精确但零判断的动作，模型做要先读两个文件找插入点（input），再手写条目（output），
    且分类小节找错位、复选框语法写错是实测常见失误。

设计不变量:
  1. **幂等重入**：同一 Task ID + 同一摘要重复执行不产生第二条；已勾选的任务不重复翻转。
  2. **就地插入不重排**：只在目标小节尾部追加，不重写 Changelog 其余内容、不调整既有条目顺序。
  3. **随文件既有语种**：分类小节名跟随该 Changelog 已在用的语种（中/英），没有既有小节时
     默认中文——与《软件工程》分类表的书写顺序一致。不强行统一他人文件的风格。
  4. **原子写入**：同目录 tempfile 组装 + os.replace；两个文件各自原子，任一校验失败则整体不写。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

# 《软件工程》「Changelog 规范」6 大分类。中英互认，写入时跟随文件既有语种。
CATEGORIES = {
    "新增": "Added", "变更": "Changed", "弃用": "Deprecated",
    "移除": "Removed", "修复": "Fixed", "安全": "Security",
}
_EN_TO_ZH = {en: zh for zh, en in CATEGORIES.items()}


class IterationError(Exception):
    """可恢复的业务异常，携带行动导向修复指引。"""


def _locate(explicit: str | None, root: Path, name: str) -> Path:
    """显式路径优先；否则 docs/<name>，再否则在 docs/ 下递归找第一个同名文件。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise IterationError(
                f"❌ 指定的 {name} 不存在: {path}\n"
                f"👉 修复建议: 核对路径，或省略该参数让工具在 docs/ 下自动定位。"
            )
        return path
    direct = root / "docs" / name
    if direct.is_file():
        return direct
    candidates = sorted((root / "docs").rglob(name)) if (root / "docs").is_dir() else []
    if not candidates:
        raise IterationError(
            f"❌ 未能在 {root}/docs/ 下定位 {name}。\n"
            f"👉 修复建议: 用 --changelog / --tasks 显式指定路径，或确认当前目录是项目根。"
        )
    return candidates[0]


def _resolve_category(raw: str, text: str) -> str:
    """把用户给的分类归一，再按文件既有语种决定写中文还是英文。"""
    zh = raw if raw in CATEGORIES else _EN_TO_ZH.get(raw)
    if not zh:
        raise IterationError(
            f"❌ 未知变更分类 {raw!r}。\n"
            f"👉 修复建议: 取 {'/'.join(CATEGORIES)} 或对应英文 {'/'.join(CATEGORIES.values())}。"
        )
    headings = set(re.findall(r"^###\s+(\S+)\s*$", text, re.MULTILINE))
    if zh in headings:
        return zh
    if CATEGORIES[zh] in headings:
        return CATEGORIES[zh]
    # 两种都没出现过：看文件整体用哪种分类词更多，全无则默认中文
    return zh if not (headings & set(CATEGORIES.values())) else CATEGORIES[zh]


def add_changelog_entry(text: str, category: str, summary: str, task_id: str) -> tuple[str, bool]:
    """在 [Unreleased] 的目标分类小节尾部追加一条。返回 (新正文, 是否发生改动)。"""
    entry = f"- {summary} ([{task_id}])"
    if entry in text:
        return text, False  # 幂等：同条目已在

    heading = _resolve_category(category, text)
    lines = text.splitlines()

    unreleased = next((i for i, l in enumerate(lines)
                       if re.match(r"^##\s*\[Unreleased\]", l, re.IGNORECASE)), None)
    if unreleased is None:
        # 没有 [Unreleased] 就在第一个 `## ` 版本段之前建一个；全无版本段则追加到文末。
        first_section = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
        block = [f"## [Unreleased]", "", f"### {heading}", "", entry, ""]
        lines[first_section:first_section] = block
        return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True

    # [Unreleased] 段的边界：到下一个 `## ` 为止
    section_end = next((i for i in range(unreleased + 1, len(lines))
                        if lines[i].startswith("## ")), len(lines))

    target = next((i for i in range(unreleased + 1, section_end)
                   if re.match(rf"^###\s+{re.escape(heading)}\s*$", lines[i])), None)
    if target is None:
        # 该分类小节尚不存在：建在 [Unreleased] 段末尾
        insert = section_end
        while insert > unreleased + 1 and not lines[insert - 1].strip():
            insert -= 1
        lines[insert:insert] = ["", f"### {heading}", "", entry]
    else:
        # 小节存在：插到该小节最后一个条目之后
        nxt = next((i for i in range(target + 1, section_end) if lines[i].startswith("###")), section_end)
        insert = nxt
        while insert > target + 1 and not lines[insert - 1].strip():
            insert -= 1
        lines[insert:insert] = [entry]

    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True


def mark_task_done(text: str, task_id: str) -> tuple[str, bool, str]:
    """把 Tasks.md 中该 ID 所在行的复选框翻成 [x]。返回 (新正文, 是否改动, 说明)。"""
    lines = text.splitlines()
    hits = [i for i, l in enumerate(lines) if re.search(rf"\*\*{re.escape(task_id)}\*\*|\b{re.escape(task_id)}\b", l)]
    if not hits:
        raise IterationError(
            f"❌ Tasks.md 中未找到任务 {task_id}。\n"
            "👉 修复建议: 核对 Task ID 拼写，或确认该任务已登记在本地任务真源中。"
        )
    checkbox = [i for i in hits if re.match(r"^\s*-\s*\[[ xX]\]", lines[i])]
    if not checkbox:
        raise IterationError(
            f"❌ 任务 {task_id} 所在行不是复选框条目，无法机械勾选。\n"
            "👉 修复建议: 该条目可能用了其他状态表达（如 `status:` 字段），请人工更新。"
        )
    idx = checkbox[0]
    if re.match(r"^\s*-\s*\[[xX]\]", lines[idx]):
        return text, False, f"{task_id} 已是完成态，未重复勾选"
    lines[idx] = re.sub(r"^(\s*-\s*)\[ \]", r"\1[x]", lines[idx], count=1)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True, f"{task_id} 已勾选完成"


def atomic_write(path: Path, content: str) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        tmp_path = Path(tmp) / path.name
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="迭代收尾履历记录：Changelog 追加 + Tasks 勾选（《软件工程》研发闭环 4-6 步）",
    )
    parser.add_argument("task_id", help="任务 ID，如 Tech-92")
    parser.add_argument("--add", nargs=2, metavar=("CATEGORY", "SUMMARY"),
                        help="追加 Changelog 条目，如 --add 修复 \"修正跨租户越权\"")
    parser.add_argument("--mark-done", action="store_true", help="在 Tasks.md 中勾选该任务为完成")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根，默认当前目录")
    parser.add_argument("--changelog", help="显式指定 Changelog.md 路径")
    parser.add_argument("--tasks", help="显式指定 Tasks.md 路径")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不产生物理写入")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    args = parser.parse_args()

    if not args.add and not args.mark_done:
        parser.error("至少指定 --add 或 --mark-done 之一")

    root = args.root.expanduser().resolve()
    changes: list[dict] = []
    pending: list[tuple[Path, str]] = []

    try:
        if args.add:
            path = _locate(args.changelog, root, "Changelog.md")
            text = path.read_text(encoding="utf-8")
            new_text, changed = add_changelog_entry(text, args.add[0], args.add[1], args.task_id)
            if changed:
                pending.append((path, new_text))
            changes.append({"file": str(path), "changed": changed,
                            "note": "已追加变更条目" if changed else "同条目已存在，未重复追加"})

        if args.mark_done:
            path = _locate(args.tasks, root, "Tasks.md")
            text = path.read_text(encoding="utf-8")
            new_text, changed, note = mark_task_done(text, args.task_id)
            if changed:
                pending.append((path, new_text))
            changes.append({"file": str(path), "changed": changed, "note": note})

        if not args.dry_run:
            for path, content in pending:
                atomic_write(path, content)

        result = {"ok": True, "task_id": args.task_id, "dry_run": args.dry_run, "changes": changes}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            prefix = "（演练）" if args.dry_run else ""
            done = sum(1 for c in changes if c["changed"])
            print(f"✅ {prefix}{args.task_id}: {done}/{len(changes)} 处写入 · "
                  + " · ".join(c["note"] for c in changes))
        return 0
    except IterationError as err:
        print(str(err), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
