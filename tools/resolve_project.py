#!/usr/bin/env python3
"""按自然语言或别名精准解析项目目录与代码真源，降低大模型定位时的上下文 Token 开销。

纯标准库实现，零外部依赖。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths


class ResolveError(Exception):
    """项目解析异常。"""


def load_registry_projects(workspace_root: Path | None = None) -> list[dict]:
    """读取工作区注册表，返回结构化项目列表。"""
    root = workspace_root or paths.WORKSPACE_ROOT
    registry_file = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.md"
    if not registry_file.is_file():
        # 回退检查模板
        registry_file = root / paths.SYSTEM_DIRNAME / "templates" / "instance" / "registry.template.md"
        if not registry_file.is_file():
            return []

    content = registry_file.read_text(encoding="utf-8")
    table_content, _, _ = content.partition("## 排除规则")

    projects: list[dict] = []
    for line in table_content.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| 项目 ID") or line.startswith("|-"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) < 3:
            continue

        proj_id = cols[0]
        if not proj_id or "待填写" in proj_id or proj_id.startswith("（"):
            continue

        name = cols[1] if len(cols) > 1 else ""
        raw_dir = cols[2] if len(cols) > 2 else ""
        dir_matches = re.findall(r"`([^`]+)`", raw_dir)
        dir_str = (dir_matches[0] if dir_matches else raw_dir).strip().rstrip("/")

        parent = cols[3] if len(cols) > 3 else ""
        aliases_str = cols[4] if len(cols) > 4 else ""
        aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]
        board_mapping = cols[5] if len(cols) > 5 else ""
        notes = cols[6] if len(cols) > 6 else ""

        # 从备注中提取代码真源（如果有）
        code_source = ""
        code_match = re.search(r"代码(?:与实施)?真源\s*`?([^`\s,，；;]+)`?", notes)
        if code_match:
            code_source = code_match.group(1).strip().rstrip("/")

        abs_dir = (root / dir_str).resolve() if dir_str else root
        abs_code_source = (abs_dir / code_source).resolve() if (dir_str and code_source) else None

        projects.append({
            "id": proj_id,
            "name": name,
            "dir": dir_str,
            "abs_dir": str(abs_dir),
            "parent": parent,
            "aliases": aliases,
            "board_mapping": board_mapping,
            "notes": notes,
            "code_source": code_source,
            "abs_code_source": str(abs_code_source) if abs_code_source else "",
        })

    return projects


def resolve_project(query: str, workspace_root: Path | None = None) -> dict | None:
    """根据查询词（项目 ID、名称、别名或路径）精准解析项目。"""
    query = query.strip()
    if not query:
        return None

    projects = load_registry_projects(workspace_root)
    if not projects:
        return None

    lowered = query.lower()

    # 1. 精确匹配 ID
    for p in projects:
        if p["id"].lower() == lowered:
            return p

    # 2. 精确匹配别名或名称
    for p in projects:
        if p["name"].lower() == lowered:
            return p
        if any(a.lower() == lowered for a in p["aliases"]):
            return p

    # 3. 别名包含匹配 / 查询词包含别名（按别名长度倒序，优先长词精准命中）
    scored_matches: list[tuple[int, dict]] = []
    for p in projects:
        matched_len = 0
        for a in p["aliases"]:
            a_low = a.lower()
            if a_low in lowered or lowered in a_low:
                matched_len = max(matched_len, len(a))
        if p["name"].lower() in lowered or lowered in p["name"].lower():
            matched_len = max(matched_len, len(p["name"]))
        if matched_len > 0:
            scored_matches.append((matched_len, p))

    if scored_matches:
        scored_matches.sort(key=lambda x: x[0], reverse=True)
        return scored_matches[0][1]

    # 4. 目录路径匹配
    for p in projects:
        if p["dir"] and (lowered == p["dir"].lower() or lowered in p["dir"].lower()):
            return p

    return None


def format_project_text(p: dict) -> str:
    """输出 Agent 极简高密度纯文本格式（~15 tokens）。"""
    code_part = f" (code: {p['code_source']})" if p.get("code_source") else ""
    parent_part = f" [parent: {p['parent']}]" if p.get("parent") else ""
    return f"[{p['id']}] {p['dir']}/{code_part}{parent_part}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按名称或别名解析项目目录与代码真源")
    parser.add_argument("query", nargs="?", default="", help="项目 ID、别名或自然语言查询词")
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON 格式")
    parser.add_argument("--list", action="store_true", help="列出注册表中的全部项目")
    parser.add_argument("--workspace-root", default=None, help="工作区根目录路径（可选）")

    args = parser.parse_args(argv)
    ws_root = Path(args.workspace_root).resolve() if args.workspace_root else None

    if args.list:
        projects = load_registry_projects(ws_root)
        if args.json:
            print(json.dumps(projects, ensure_ascii=False, indent=2))
        else:
            for p in projects:
                print(format_project_text(p))
        return 0

    if not args.query:
        print("❌ 错误原因：未提供查询词。\n👉 修复建议：指定项目名称或别名，例如 `python3 resolve_project.py PaperPro`，或加 `--list` 查看全部项目。", file=sys.stderr)
        return 1

    matched = resolve_project(args.query, ws_root)
    if not matched:
        print(f"❌ 错误原因：未找到与 '{args.query}' 匹配的项目。\n👉 修复建议：检查 .entropaxis/data/templates/registry.md 确认项目名称或别名，或运行 `--list` 查看有效列表。", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(matched, ensure_ascii=False, indent=2))
    else:
        print(format_project_text(matched))

    return 0


if __name__ == "__main__":
    sys.exit(main())
