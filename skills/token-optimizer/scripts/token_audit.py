#!/usr/bin/env python3
"""Token 经济性与语法税静态审计脚本。

静态计量目标文件/目录的 Token 估算、信息密度与语法税（Markdown 宽表、排版空格、装饰线）。
纯标准库实现，零外部依赖。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def estimate_tokens(text: str) -> int:
    """粗粒度静态 Token 估算（中文字符 ~1 token，英文单词与符号 ~0.25 token）。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
    return cjk + (len(text) - cjk + 3) // 4


def analyze_syntax_tax(content: str) -> dict:
    """分析文本中的语法税（排版表格、多余空格与装饰符号）。"""
    lines = content.splitlines()
    total_lines = len(lines)
    total_chars = len(content)

    table_lines = 0
    table_chars = 0
    excess_space_chars = 0
    box_chars = 0

    table_sep_pattern = re.compile(r"^\|?[\s\-:|]+\|?$")
    table_row_pattern = re.compile(r"^\|.+line.+\|$|^\|.+\|$")

    for line in lines:
        stripped = line.strip()
        if table_sep_pattern.match(stripped) or (stripped.startswith("|") and stripped.endswith("|")):
            table_lines += 1
            table_chars += len(line)

        # 匹配连续多余空格（除缩进外）
        excess_spaces = re.findall(r"(?<=\S) {2,}(?=\S)", line)
        excess_space_chars += sum(len(m) for m in excess_spaces)

        # 匹配 ASCII 边框字符
        box_chars += len(re.findall(r"[┌┬┐├┼┤└┴┘│─═║╔╦╗╠╬╣╚╩╝]", line))

    tax_chars = table_chars + excess_space_chars + box_chars
    tax_ratio = round((tax_chars / total_chars * 100), 2) if total_chars > 0 else 0.0

    return {
        "total_lines": total_lines,
        "total_chars": total_chars,
        "estimated_tokens": estimate_tokens(content),
        "table_lines": table_lines,
        "syntax_tax_chars": tax_chars,
        "syntax_tax_ratio_pct": tax_ratio,
        "has_heavy_tables": table_lines > 10,
    }


def audit_path(path: Path) -> dict:
    """审计单个文件或目录。"""
    if path.is_file():
        try:
            content = path.read_text(encoding="utf-8")
            metrics = analyze_syntax_tax(content)
            return {"path": str(path), "is_file": True, **metrics}
        except Exception as exc:
            return {"path": str(path), "error": str(exc)}

    results = []
    total_tokens = 0
    total_tax_chars = 0
    total_chars = 0

    for file_path in sorted(path.rglob("*.md")):
        try:
            content = file_path.read_text(encoding="utf-8")
            metrics = analyze_syntax_tax(content)
            total_tokens += metrics["estimated_tokens"]
            total_tax_chars += metrics["syntax_tax_chars"]
            total_chars += metrics["total_chars"]
            results.append({"path": str(file_path), **metrics})
        except Exception:
            continue

    overall_tax_ratio = round((total_tax_chars / total_chars * 100), 2) if total_chars > 0 else 0.0

    return {
        "target": str(path),
        "is_file": False,
        "file_count": len(results),
        "total_estimated_tokens": total_tokens,
        "overall_syntax_tax_ratio_pct": overall_tax_ratio,
        "files": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="静态审计文件与规则的 Token 消耗与语法税")
    parser.add_argument("paths", nargs="+", help="待审计的文件或目录路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON 格式")

    args = parser.parse_args(argv)
    reports = [audit_path(Path(p)) for p in args.paths]

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0

    for report in reports:
        if report.get("error"):
            print(f"❌ {report['path']}: {report['error']}")
            continue
        if report.get("is_file"):
            print(f"📄 {report['path']}:")
            print(f"   • 行数: {report['total_lines']} 行 | 字符数: {report['total_chars']} 字符")
            print(f"   • 预估 Token: ~{report['estimated_tokens']} tokens")
            print(f"   • 语法税占比: {report['syntax_tax_ratio_pct']}% ({report['syntax_tax_chars']} 字符)")
            if report['has_heavy_tables']:
                print("   ⚠️ 警告: 检测到多行 Markdown 宽表格，建议优化为紧凑键值或下沉 Tool 前置提取。")
        else:
            print(f"📁 目录: {report['target']} ({report['file_count']} 个 Markdown 文件):")
            print(f"   • 总预估 Token: ~{report['total_estimated_tokens']} tokens")
            print(f"   • 整体语法税占比: {report['overall_syntax_tax_ratio_pct']}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
