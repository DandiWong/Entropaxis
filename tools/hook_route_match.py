#!/usr/bin/env python3
"""确定性关键词路由前置注入 (UserPromptSubmit Hook).

对用户原始输入做字面关键词匹配，命中 .entropaxis/config/route_map.json 中登记的机制时，
通过 additionalContext 强制提示模型在执行前先读取对应真源规则文件，
把"要不要读规则"从模型自行判断的概率事件，改为 harness 侧的确定性前置动作。

仅覆盖规则正文已显式声明的字面触发词（见 route_map.json 的 source_of_truth），
转述/意译等语义命中不在本工具覆盖范围内，仍需模型自身依据元规则第 8 条判断。

执行方式（由 .claude/settings.json 的 UserPromptSubmit hook 调用）:
  python3 .entropaxis/tools/hook_route_match.py < stdin(JSON)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__:
    from .route_context import WORKSPACE_ROOT, resolve_route_reads
else:
    from route_context import WORKSPACE_ROOT, resolve_route_reads

HERE = Path(__file__).resolve().parent
ROUTE_MAP_PATH = HERE.parent / "config" / "route_map.json"


def load_routes(path: Path = ROUTE_MAP_PATH) -> list[dict]:
    """读取路由映射表；文件缺失或损坏时优雅降级为空表，不阻断会话。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("routes", [])


def normalize(text: str) -> str:
    """归一化待匹配文本：小写 + 去所有空白。

    去空白是必要的——触发词以无空格形式登记（如 `纳入entropaxis`），而用户实际会写
    「纳入 Entropaxis」。不做归一化则中英混排的触发词几乎必然漏检。
    """
    return "".join(text.lower().split())


def match_prompt(prompt: str, routes: list[dict]) -> list[dict]:
    """返回命中的路由条目列表（保留原始顺序，可能重复命中同一文件的不同机制）。"""
    if not prompt:
        return []
    text = normalize(prompt)
    hits = []
    for route in routes:
        keywords = [normalize(kw) for kw in route.get("keywords", [])]
        if route.get("match_type", "substring") == "exact":
            hit = text in set(keywords)
        else:
            hit = any(kw in text for kw in keywords)
        # 抑制词：命中即判定为他机制语义，消解子串包含造成的误触发
        if hit and any(normalize(x) in text for x in route.get("exclude", [])):
            hit = False
        if hit:
            hits.append(route)
    return hits


PRECEDENCE_NOTICE = (
    "本提示属于工作区规则；覆盖边界遵循元规则第 2 条，不改变宿主指令权限。"
    "读取真源是执行前置动作，不因风格或精简偏好省略。"
)

ANCHOR_NOTICE = (
    "读取范围：按上列文件行号读取，包含适用范围与已声明必需依赖，重叠区间已合并。"
    "条件依赖在相关动作发生时按需追加；文件变化后重新定位，不能沿用旧行号。"
    "本提示不代替实际读取。"
)


def build_additional_context(hits: list[dict], root: Path = WORKSPACE_ROOT) -> str:
    """Render the exact read plan shared with the static cost auditor."""
    if not hits:
        return ""
    mechanisms = "、".join(dict.fromkeys(r.get("mechanism", "未命名机制") for r in hits))
    lines = [f"[确定性路由命中] 机制「{mechanisms}」：执行前必须先 Read 对应真源核对强制动作项："]
    try:
        plan = resolve_route_reads({"reads": [item for route in hits for item in route["reads"]]}, root)
    except (KeyError, ValueError) as exc:
        lines.append(f"读取计划无效：{exc}；请修正路由配置，不能视为已读取。")
    else:
        for item in plan:
            if item["missing"]:
                lines.append(f"  • {item['file']}：{item['fallback']}；读取未完成。")
            else:
                lines.append(f"  • {item['file']}:{item['start_line']}-{item['end_line']}")
                if item["fallback"]:
                    lines.append(f"    {item['fallback']}")
    lines.extend((ANCHOR_NOTICE, PRECEDENCE_NOTICE))
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
