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
import re
import sys
import tempfile
from pathlib import Path
try:
    from . import update_capsule
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import update_capsule  # type: ignore

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
    return update_task_entry(text, task_id, mark_done=True)


def update_task_entry(text: str, task_id: str, *, mark_done: bool = False, hours: float | None = None) -> tuple[str, bool, str]:
    """更新 Tasks.md 中对应 Task 所在行（状态翻转与 ⏱ 工时累计）。"""
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
    line = lines[idx]
    orig_line = line
    notes = []
    if mark_done:
        if re.match(r"^\s*-\s*\[ \]", line):
            line = re.sub(r"^(\s*-\s*)\[ \]", r"\1[x]", line, count=1)
            notes.append(f"{task_id} 已勾选完成")
        else:
            notes.append(f"{task_id} 已是完成态")
    if hours is not None:
        h_str = f"{int(hours) if hours == int(hours) else hours}h"
        if re.search(r"⏱\d+(\.\d+)?h", line):
            line = re.sub(r"⏱\d+(\.\d+)?h", f"⏱{h_str}", line)
            notes.append(f"工时累计更新为 {h_str}")
        else:
            line = line.rstrip() + f" ⏱{h_str}"
            notes.append(f"工时记为 {h_str}")
    changed = line != orig_line
    lines[idx] = line
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed, " · ".join(notes) or "无需变更"


def find_capsule_for_task(root: Path, task_id: str, explicit: str | None = None) -> Path | None:
    """定位写入用工时胶囊（显式目录优先，否则唯一顶层 id 命中的胶囊）。

    注意：这只回答「工时记到哪个胶囊」。一个工程任务的段可以跨多个胶囊，
    收尾汇总必须用 collect_task_segments() 做全域扫描，不能用本函数的单点结果。
    """
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if (p / "capsule.yaml").is_file():
            return p
        raise IterationError(f"❌ 指定的胶囊目录不存在 capsule.yaml: {explicit}")
    capsules = scan_capsules(root)
    candidates = [c for c in capsules if (c.get("data", {}).get("id") or "") == task_id]
    if len(candidates) == 1:
        return candidates[0]["dir"]
    if len(candidates) > 1:
        raise IterationError(
            f"❌ 多个胶囊命中同一 Task ID {task_id}: {[str(c['dir']) for c in candidates]}\n"
            "👉 修复建议: 用 --capsule 显式指定。"
        )
    return None


def scan_capsules(root: Path) -> list[dict]:
    """枚举项目范围内全部胶囊（控制面 data/docs + 各项目 docs），不分 lifecycle。

    IMP-C2：收尾汇总的事实来源必须是全域胶囊，不是顶层 id 相同的单个胶囊。
    """
    ws_root = root
    for p in [root, *root.parents]:
        if (p / ".entropaxis").is_dir():
            ws_root = p
            break
    search_dirs = [ws_root / ".entropaxis" / "data" / "docs"]
    if (root / "docs").is_dir():
        search_dirs.append(root / "docs")
    # 注册表内项目的自有 docs（含嵌套子项目）及代码库 code[0]/docs，与控制面 docs 并成同一扫描全集。
    try:
        import project_registry
        for proj in project_registry.load_projects(ws_root):
            docs = ws_root / str(proj.get("path", "")).strip("/") / "docs"
            if docs.is_dir() and docs not in search_dirs:
                search_dirs.append(docs)
            for c_path in proj.get("code") or []:
                c_docs = ws_root / str(c_path).strip("/") / "docs"
                if c_docs.is_dir() and c_docs not in search_dirs:
                    search_dirs.append(c_docs)
    except Exception:  # noqa: BLE001 - 注册表缺失/不可读时退回控制面 + 当前项目两级
        pass
    capsules: list[dict] = []
    seen: set[Path] = set()
    for d in search_dirs:
        if not d.is_dir():
            continue
        for manifest in sorted(d.rglob("capsule.yaml")):
            cap_dir = manifest.parent
            if cap_dir in seen:
                continue
            seen.add(cap_dir)
            data: dict = {}
            try:
                import yaml
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 - 单份损坏清单不阻断其余胶囊扫描
                data = {}
            capsules.append({"dir": cap_dir, "data": data})
    return capsules


def collect_task_segments(root: Path, task_id: str) -> list[dict]:
    """全域收集挂在 task_id 下的任务段（IMP-C2）。

    返回元素含 segment、capsule_dir、capsule_id；同一 taskId 可跨多个胶囊。
    """
    found: list[dict] = []
    for cap in scan_capsules(root):
        cid = cap["data"].get("id") or cap["dir"].name
        for seg in cap["data"].get("task_segments") or []:
            if isinstance(seg, dict) and seg.get("taskId") == task_id:
                found.append({
                    "segment": seg,
                    "capsule_dir": cap["dir"],
                    "capsule_id": cid,
                })
    return found

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
    parser.add_argument("--time", type=float, metavar="HOURS", help="登记工时并联动更新 Tasks 累计（步长 0.25h）")
    parser.add_argument("--from-segments", action="store_true", help="按该 taskId 下全部有效任务段重算并写入 Tasks.md（方案 B 契约）")
    parser.add_argument("--capsule", help="显式指定胶囊目录")
    parser.add_argument("--stage", help="工时所属阶段名，缺省自动推导")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根，默认当前目录")
    parser.add_argument("--changelog", help="显式指定 Changelog.md 路径")
    parser.add_argument("--tasks", help="显式指定 Tasks.md 路径")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不产生物理写入")
    args = parser.parse_args()

    if not args.add and not args.mark_done and args.time is None and not args.from_segments:
        parser.error("❌ 缺少动作参数\n👉 修复建议: 至少指定 --add、--mark-done、--time 或 --from-segments 之一。")
    if args.time is not None and args.from_segments:
        parser.error("❌ --time 与 --from-segments 互斥\n👉 修复建议: 有任务段的任务只用 --from-segments，去掉 --time。")
    root = args.root.expanduser().resolve()
    changes: list[dict] = []
    pending: list[tuple[Path, str]] = []
    worklog_refs: list[str] = []

    try:
        if args.add:
            path = _locate(args.changelog, root, "Changelog.md")
            text = path.read_text(encoding="utf-8")
            new_text, changed = add_changelog_entry(text, args.add[0], args.add[1], args.task_id)
            if changed:
                pending.append((path, new_text))
            changes.append({"file": str(path), "changed": changed,
                            "note": "已追加变更条目" if changed else "同条目已存在，未重复追加"})

        capsule_dir = None
        total_hours = None
        if args.time is not None:
            capsule_dir = find_capsule_for_task(root, args.task_id, args.capsule)

        if args.from_segments:
            # IMP-C2：先收齐全域胶囊中挂在 task_id 下的所有段，再做判定与求和。
            matched = collect_task_segments(root, args.task_id)
            if not matched:
                raise IterationError(
                    f"❌ 无法执行 --from-segments: 未找到任务 {args.task_id} 对应的任务段。\n"
                    "👉 修复建议: 确认已用 update_capsule.py --start-task --task-id "
                    f"{args.task_id} 开段；或该任务没有段，改用 --time 登记。"
                )
            # open 段拦截：任一胶囊存在该 taskId 的 open 段即拒绝收尾汇总。
            open_s = [m["segment"] for m in matched if m["segment"].get("state") == "open"]
            if open_s:
                raise IterationError(
                    f"❌ 任务 {args.task_id} 存在未收尾的 open 任务段: {[s['seg_id'] for s in open_s]}\n"
                    "👉 修复建议: 请先收尾任务段（update_capsule.py --end-task），再执行收尾汇总。"
                )
            # pending_approval 段告警且不计入。
            pending_s = [m["segment"] for m in matched if m["segment"].get("state") == "pending_approval"]
            if pending_s:
                print(f"⚠️ 警告: 任务 {args.task_id} 存在待审批任务段: {[s['seg_id'] for s in pending_s]}，暂不计入本次汇总", file=sys.stderr)

            # 跨胶囊求和：logged 段 seg_id → 所属胶囊 worklogs 的当前有效 spent。
            logged_by_capsule: dict[Path, set[str]] = {}
            for m in matched:
                if m["segment"].get("state") == "logged":
                    logged_by_capsule.setdefault(m["capsule_dir"], set()).add(m["segment"]["seg_id"])
            total_hours = 0.0
            for cap_dir, seg_ids in logged_by_capsule.items():
                for w in update_capsule.read_worklogs(cap_dir):
                    if w.get("ref") in seg_ids:
                        try:
                            total_hours += float(w.get("spent", 0))
                        except (TypeError, ValueError):
                            continue
            changes.append({"file": "跨胶囊全域扫描", "changed": True,
                            "note": (f"汇总 {len(matched)} 个任务段（logged {sum(len(v) for v in logged_by_capsule.values())}）"
                                     f"，有效工时 {total_hours}h")})

        if args.time is not None:
            # IMP-C2：跨胶囊已有该 taskId 的段时，--time 同样拦截，防止双计。
            existing_segments = collect_task_segments(root, args.task_id)
            if existing_segments:
                raise IterationError(
                    f"❌ 任务 {args.task_id} 已包含任务段，禁止使用 --time；请使用 --from-segments 进行收尾汇总。"
                )
            if capsule_dir:
                summary = args.add[1] if args.add else f"完成 {args.task_id}"
                res = update_capsule.log_time(capsule_dir, spent=args.time, stage=args.stage,
                                              summary=summary, dry_run=args.dry_run, idempotent=True)
                total_hours = res["total"]
                worklog_refs = res.get("worklog_refs", [])
                changes.append({"file": str(capsule_dir / "capsule.yaml"), "changed": res["status"] == "added",
                                "note": f"胶囊已登记工时 {args.time}h (真源累计 {total_hours}h)" if res["status"] == "added" else f"胶囊工时已存在(幂等放行，真源累计 {total_hours}h)"})
            else:
                print("⚠️ 未找到胶囊，工时仅记入 Tasks", file=sys.stderr)

        if args.mark_done or args.time is not None or args.from_segments:
            path = _locate(args.tasks, root, "Tasks.md")
            text = path.read_text(encoding="utf-8")
            # 若无胶囊，从既有行累计
            calc_hours = total_hours
            if calc_hours is None and args.time is not None:
                # 从当前行提取现有小时并累加
                m = re.search(rf"\b{re.escape(args.task_id)}\b.*?⏱(\d+(?:\.\d+)?)h", text)
                calc_hours = (float(m.group(1)) + args.time) if m else args.time
            new_text, changed, note = update_task_entry(text, args.task_id, mark_done=args.mark_done, hours=calc_hours)
            if changed:
                pending.append((path, new_text))
            changes.append({"file": str(path), "changed": changed, "note": note})
        if not args.dry_run:
            for path, content in pending:
                atomic_write(path, content)

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
