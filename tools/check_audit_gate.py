#!/usr/bin/env python3
"""审计报告 Critical 问题关闭硬门禁 (Audit Critical-Closure Gate).

把《指令解析.md》"审计"节——"Critical 级问题的关闭必须由外置 reviewer 完成
（会话内自评不可关闭 Critical）"——从纯文字约束转成可执行的机械阻断：
扫描审计报告 Front Matter 的 independence 字段与正文问题清单，命中
"会话内降级 (session-internal-downgraded) + 存在已关闭 Critical 问题"
的组合时以非零退出码阻断，不允许会话内自评静默关闭 Critical。

用法:
  python3 .system/tools/check_audit_gate.py <审计报告路径>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FRONT_MATTER_PATTERN = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
INDEPENDENCE_PATTERN = re.compile(r"^independence:\s*(.+)$", re.MULTILINE)
# 单条问题块：从"级别: Critical"起，到下一个标题/分隔线/文末为止。
ISSUE_BLOCK_PATTERN = re.compile(r"级别[:：]\s*Critical.*?(?=\n#{1,6}\s|\n---|\Z)", re.DOTALL)
CLOSED_PATTERN = re.compile(r"状态[:：]\s*(已关闭|closed)", re.IGNORECASE)


def extract_independence(text: str) -> str | None:
    """读取 Front Matter 中的 independence 声明；未声明返回 None。"""
    match = FRONT_MATTER_PATTERN.search(text)
    if not match:
        return None
    m2 = INDEPENDENCE_PATTERN.search(match.group(1))
    return m2.group(1).strip() if m2 else None


def find_closed_critical_blocks(text: str) -> list[str]:
    """返回正文中已标注"已关闭"的 Critical 问题块。"""
    return [b for b in ISSUE_BLOCK_PATTERN.findall(text) if CLOSED_PATTERN.search(b)]


def check_report(text: str) -> list[str]:
    """对审计报告全文做门禁核验，返回违规说明列表（空列表即通过）。

    fail-closed：independence 缺失时不再放行。此前缺字段即返回空列表，
    等于「忘写声明」比「如实写会话内降级」更容易通过门禁——奖励了漏报。
    字段契约见 .system/schemas/audit_report.schema.json。
    """
    independence = extract_independence(text)
    closed = find_closed_critical_blocks(text)
    if not closed:
        return []
    if not independence:
        return [
            f"检测到 {len(closed)} 处 Critical 问题标记为已关闭，但 Front Matter 未声明 "
            "independence。审计独立性无法核验的报告不得关闭 Critical；请按 "
            "`external: <cli>` 或 `session-internal-downgraded (external: <cli> missing|failed)` 补齐。"
        ]
    if independence.startswith("session-internal"):
        return [
            f"independence={independence!r} 属于会话内降级，但检测到 {len(closed)} 处 Critical "
            "问题标记为已关闭；会话内自评不可关闭 Critical，必须由外置 reviewer 复核或转人工仲裁。"
        ]
    return []


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python3 check_audit_gate.py <审计报告路径>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"[审计报告不存在] {path}", file=sys.stderr)
        return 2
    issues = check_report(path.read_text(encoding="utf-8"))
    if issues:
        for issue in issues:
            print(f"[Critical违规关闭] {path}: {issue}", file=sys.stderr)
        return 1
    print(f"✅ {path} 未发现会话内降级下的 Critical 违规关闭。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
