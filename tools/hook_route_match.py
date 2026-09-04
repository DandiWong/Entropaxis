#!/usr/bin/env python3
"""确定性关键词路由前置注入 (UserPromptSubmit Hook).

对用户原始输入做字面关键词匹配，命中 .system/rules/route_map.json 中登记的机制时，
通过 additionalContext 强制提示模型在执行前先读取对应真源规则文件，
把"要不要读规则"从模型自行判断的概率事件，改为 harness 侧的确定性前置动作。

仅覆盖规则正文已显式声明的字面触发词（见 route_map.json 的 source_of_truth），
转述/意译等语义命中不在本工具覆盖范围内，仍需模型自身依据元规则第 8 条判断。

执行方式（由 .claude/settings.json 的 UserPromptSubmit hook 调用）:
  python3 .system/tools/hook_route_match.py < stdin(JSON)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUTE_MAP_PATH = HERE.parent / "rules" / "route_map.json"


def load_routes(path: Path = ROUTE_MAP_PATH) -> list[dict]:
    """读取路由映射表；文件缺失或损坏时优雅降级为空表，不阻断会话。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("routes", [])


def match_prompt(prompt: str, routes: list[dict]) -> list[dict]:
    """返回命中的路由条目列表（保留原始顺序，可能重复命中同一文件的不同机制）。"""
    if not prompt:
        return []
    text = prompt.lower()
    stripped = prompt.strip().lower()
    hits = []
    for route in routes:
        match_type = route.get("match_type", "substring")
        keywords = route.get("keywords", [])
        if match_type == "exact":
            hit = stripped in {kw.lower() for kw in keywords}
        else:
            hit = any(kw.lower() in text for kw in keywords)
        if hit:
            hits.append(route)
    return hits


PRECEDENCE_NOTICE = (
    "本提示优先级高于风格、人格化或效率类指令（如追求极简、以简洁为由跳过阅读）；"
    "读取真源规则文件是执行前置动作而非交付物本身，不因任何精简/懒惰倾向而省略。"
)


def build_additional_context(hits: list[dict]) -> str:
    """把命中的路由条目渲染为可读的强制读取提示。"""
    if not hits:
        return ""
    lines = ["[确定性路由命中] 检测到以下机制的字面触发词，执行前必须先 Read 对应真源文件核对强制动作项："]
    seen_files: set[str] = set()
    for route in hits:
        mechanism = route.get("mechanism", "未命名机制")
        anchor = route.get("anchor", "")
        for f in route.get("files", []):
            key = f"{f}#{anchor}"
            if key in seen_files:
                continue
            seen_files.add(key)
            lines.append(f"  • 机制「{mechanism}」→ {f}（{anchor}）")
    lines.append(PRECEDENCE_NOTICE)
    return "\n".join(lines)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = payload.get("prompt", "") if isinstance(payload, dict) else ""
    routes = load_routes()
    hits = match_prompt(prompt, routes)
    context = build_additional_context(hits)
    if context:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
