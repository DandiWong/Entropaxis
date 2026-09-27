#!/usr/bin/env python3
"""按自然语言或别名精准解析项目目录与代码真源，降低大模型定位时的上下文 Token 开销。

注册表读取经 project_registry（PyYAML）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from . import paths, project_registry
except ImportError:
    import paths
    import project_registry


class ResolveError(Exception):
    """项目解析异常。"""


def load_registry_projects(workspace_root: Path | None = None) -> list[dict]:
    """读取工作区注册表，返回结构化项目列表（code_source 取首个代码真源，相对工作区根）。"""
    root = workspace_root or paths.WORKSPACE_ROOT
    projects = []
    for p in project_registry.load_projects(root):
        code = p["code"][0] if p["code"] else ""
        projects.append({
            "id": p["id"],
            "name": p["name"],
            "dir": p["path"],
            "abs_dir": str((root / p["path"]).resolve()),
            "parent": p["parent"],
            "aliases": p["aliases"],
            "boards": p["boards"],
            "notes": p["note"],
            "code_source": code,
            "abs_code_source": str((root / code).resolve()) if code else "",
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
        print(f"❌ 错误原因：未找到与 '{args.query}' 匹配的项目。\n👉 修复建议：检查 .entropaxis/data/templates/registry.yaml 确认项目名称或别名，或运行 `--list` 查看有效列表。", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(matched, ensure_ascii=False, indent=2))
    else:
        print(format_project_text(matched))

    return 0


if __name__ == "__main__":
    sys.exit(main())
