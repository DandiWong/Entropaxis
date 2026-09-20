#!/usr/bin/env python3
"""随机取一条工作空间 Tip 原文，供会话首条回复末尾展示。

替代「Read 整份 tips.md 再挑一行」的读法：后者每会话都为一行输出付掉整份真源的上下文
成本，正是 00_元规则 第 3 条禁止的「在上下文内大范围扫表」。
执行方式:
  python3 .entropaxis/tools/pick_tip.py
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

TIPS_PATH = paths.DATA_DIR / "rules" / "tips.md"
# 全角冒号为契约写法，半角一并接受：否则手工写成 `- TIP:` 的条目会被静默丢弃
TIP_PATTERN = re.compile(r"^-\s*TIP[：:]")


def pick_tip(tips_path: Path = TIPS_PATH) -> str | None:
    """返回一条随机 TIP 原文；真源缺失、为空或不可读时返回 None（契约要求静默跳过）。"""
    try:
        text = tips_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    tips = [line.strip() for line in text.splitlines() if TIP_PATTERN.match(line.strip())]
    return random.choice(tips) if tips else None


def main() -> int:
    parser = argparse.ArgumentParser(description="随机取一条工作空间 Tip 原文（无可用条目时静默退出）")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    args = parser.parse_args()

    tip = pick_tip()
    if args.json:
        print(json.dumps({"tip": tip}, ensure_ascii=False))
    elif tip:
        print(tip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
