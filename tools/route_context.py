"""Resolve explicit rule reads for both Hook guidance and static cost estimates."""
from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
_RULE_PATH = re.compile(r"\.system/rules/[^/\\]+\.md")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    headings = []
    fence = None
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        marker = _FENCE.match(text)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            if marker[1][0] != "`" or "`" not in marker[2]:
                fence = marker[1]
            continue
        heading = _HEADING.match(text)
        if heading:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip()
            headings.append((index, len(heading[1]), heading[1] + " " + title))
    return headings


def _intervals(lines: list[str], anchor: str | None) -> tuple[list[tuple[int, int]], str | None]:
    if anchor is None:
        return [(0, len(lines))], None
    headings = _headings(lines)
    matches = [i for i, (_, _, title) in enumerate(headings) if title == anchor.strip()]
    if len(matches) != 1:
        reason = "锚点缺失" if not matches else "锚点重复"
        return [(0, len(lines))], f"{reason}：{anchor}；回退全文，请修正 reads"
    selected = matches[0]
    start, level, _ = headings[selected]
    end = next((index for index, depth, _ in headings[selected + 1:] if depth <= level), len(lines))
    intervals = [(start, end)]
    # File preamble and ancestor introductions may carry applicability boundaries.
    if headings and headings[0][0]:
        intervals.append((0, headings[0][0]))
    ancestors = []
    for i, (_, depth, _) in enumerate(headings[:selected]):
        while ancestors and headings[ancestors[-1]][1] >= depth:
            ancestors.pop()
        ancestors.append(i)
    for i in ancestors:
        if headings[i][1] < level:
            intervals.append((headings[i][0], headings[i + 1][0]))
    return intervals, None


def resolve_route_reads(route: dict, root: Path = WORKSPACE_ROOT) -> list[dict]:
    """Return unique inclusive ranges, retaining scope introductions and failures.

    Additional required sections are ordinary entries in ``reads``. Conditional
    dependencies are loaded by the agent when their triggering action occurs.
    """
    reads = route.get("reads")
    if not isinstance(reads, list) or not reads:
        raise ValueError("路由须声明非空 reads，请按 route_map.schema.json 更新配置。")
    by_file: dict[str, list[dict]] = {}
    for item in reads:
        if not isinstance(item, dict) or set(item) != {"file", "anchor"}:
            raise ValueError("每个 reads 项必须且只能包含 file、anchor。")
        file, anchor = item["file"], item["anchor"]
        if not isinstance(file, str) or not _RULE_PATH.fullmatch(file):
            raise ValueError("reads.file 须指向 .system/rules/ 下的 Markdown 文件。")
        if anchor is not None and (not isinstance(anchor, str) or not re.match(r"^#{1,6} \S", anchor)):
            raise ValueError(f"{file} 的 anchor 须为标题原文或 null（全文）。")
        by_file.setdefault(file, []).append(item)

    root = root.resolve()
    result = []
    for file, items in by_file.items():
        path = root / file
        try:
            path.resolve().relative_to(root / ".system" / "rules")
        except ValueError:
            raise ValueError(f"{file} 越出规则目录，拒绝读取；请修正路径或软链。")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            result.append({"file": file, "anchor": None, "text": "", "start_line": None,
                           "end_line": None, "fallback": f"文件不可读：{type(exc).__name__}；请恢复规则文件",
                           "missing": True})
            continue
        lines = text.splitlines(keepends=True)
        intervals = []
        fallbacks = []
        for item in items:
            spans, fallback = _intervals(lines, item["anchor"])
            intervals.extend(spans)
            if fallback and fallback not in fallbacks:
                fallbacks.append(fallback)
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            result.append({"file": file, "anchor": " | ".join(dict.fromkeys(
                item["anchor"] for item in items if item["anchor"] is not None)) or None,
                "text": "".join(lines[start:end]), "start_line": start + 1,
                "end_line": max(start + 1, end), "fallback": "; ".join(fallbacks) or None,
                "missing": False})
    return result
