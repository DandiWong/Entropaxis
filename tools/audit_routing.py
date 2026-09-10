#!/usr/bin/env python3
"""Audit deterministic routing regression coverage and static read estimates.

The regression corpus protects the literal matcher.  The frozen holdout corpus is
reported separately as a diagnostic of semantic expectations; it never changes
``--strict``.  Static figures are character-based estimates of the exact hook
context and resolver-selected rule ranges, not provider token accounting or
billing data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__:
    from .hook_route_match import build_additional_context, match_prompt
    from .route_context import resolve_route_reads
else:
    from hook_route_match import build_additional_context, match_prompt
    from route_context import resolve_route_reads

HERE = Path(__file__).resolve().parent
SYSTEM_ROOT = HERE.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent
ROUTE_MAP_PATH = SYSTEM_ROOT / "config" / "route_map.json"
CASES_PATH = SYSTEM_ROOT / "tests" / "fixtures" / "instruction_cases.json"
HOLDOUT_PATH = SYSTEM_ROOT / "tests" / "fixtures" / "instruction_holdout.json"

# Always-present files are shown independently; they are not part of a route hit.
RESIDENT_FILES = ("AGENTS.md", "CLAUDE.md")
COST_SCOPE_EXCLUSIONS = {
    "provider_prompt": "未计入宿主、Provider 与其他 Skill 注入的上下文",
    "dynamic_reads": "未观测模型实际读取、后续条件依赖和工具返回的格式开销",
    "instance_reads": "未计入规则引用的 .data 实例、项目材料及 Skill 正文；读取清单只覆盖声明的规则段",
    "cache_retries": "未观测缓存、重复读取、重试与多轮 Hook 调用",
    "output_billing": "无模型输出、真实 tokenizer usage、计费单价与账单",
}


class AuditError(Exception):
    """Actionable input error."""


def estimate_tokens(text: str) -> int:
    """Return a coarse static text estimate, never a provider token or price."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    return cjk + (len(text) - cjk + 3) // 4


def _load_json(path: Path, hint: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"无法读取 {path}：{exc}\n{hint}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"{path} 须为 JSON 对象。{hint}")
    return value


def load_routes(path: Path = ROUTE_MAP_PATH) -> list[dict]:
    return _load_json(path, "从版本库恢复 route_map.json。").get("routes", [])


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    return _load_json(path, "按 instruction_cases.json 的 _meta 结构创建回归语料。").get("cases", [])


def load_holdout_cases(path: Path = HOLDOUT_PATH) -> list[dict]:
    return _load_json(path, "按 instruction_holdout.json 的 _meta 结构创建冻结留出语料。").get("cases", [])


def audit_coverage(routes: list[dict], cases: list[dict], corpus: str) -> dict:
    """Compare literal hook hits with one corpus's declared semantic expectation."""
    if not isinstance(cases, list) or not cases:
        raise AuditError(f"{corpus} 语料为空或不是列表；请恢复用例，不能将空集报告为通过。")
    known = {route.get("mechanism") for route in routes}
    allowed = {"positive", "negative", "paraphrase"} if corpus == "regression" else {"semantic"}
    for index, case in enumerate(cases):
        if (not isinstance(case, dict) or case.get("kind") not in allowed
                or not isinstance(case.get("instruction"), str) or not case["instruction"].strip()
                or not isinstance(case.get("expect"), list)
                or any(not isinstance(name, str) or name not in known for name in case["expect"])):
            raise AuditError(f"{corpus} 用例 {index + 1} 契约无效；核对 kind、instruction 与 expect。")
    results = []
    for case in cases:
        hit_names = [route.get("mechanism", "") for route in match_prompt(case["instruction"], routes)]
        expected = case.get("expect", [])
        missed = [mechanism for mechanism in expected if mechanism not in hit_names]
        extra = [mechanism for mechanism in hit_names if mechanism not in expected]
        results.append({
            "kind": case.get("kind", "positive"),
            "instruction": case["instruction"],
            "expect": expected,
            "hit": hit_names,
            "missed": missed,
            "extra": extra,
            "note": case.get("note", ""),
        })

    summary = {}
    for kind in sorted({result["kind"] for result in results} | {"positive", "negative", "paraphrase", "semantic"}):
        group = [result for result in results if result["kind"] == kind]
        summary[kind] = {
            "total": len(group),
            "clean": sum(not result["missed"] and not result["extra"] for result in group),
            "missed": sum(bool(result["missed"]) for result in group),
            "extra": sum(bool(result["extra"]) for result in group),
        }

    tested = {
        mechanism
        for result in results
        if result["kind"] == "positive"
        for mechanism in result["expect"]
    }
    return {
        "corpus": corpus,
        "scope": ("已登记字面回归，不代表总体语义覆盖率" if corpus == "regression"
                  else "本轮构造且未用于调词的有限留出样本；非独立用户抽样或盲测"),
        "results": results,
        "summary": summary,
        "untested_mechanisms": [
            route.get("mechanism", "") for route in routes
            if route.get("mechanism") not in tested
        ] if corpus == "regression" else [],
    }


def audit_corpora(routes: list[dict], regression_cases: list[dict], holdout_cases: list[dict]) -> dict:
    """Keep gateable regression evidence and nonblocking semantic diagnostics apart."""
    return {
        "regression": audit_coverage(routes, regression_cases, "regression"),
        "holdout": audit_coverage(routes, holdout_cases, "holdout"),
    }


def regression_failures(regression: dict) -> list[dict]:
    """Only literal regression positive/negative cases gate ``--strict``."""
    return [
        result for result in regression["results"]
        if result["kind"] in {"positive", "negative"} and (result["missed"] or result["extra"])
    ]


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None



def _range_record(read: dict) -> dict:
    record = {
        "file": read["file"],
        "anchor": read.get("anchor"),
        "start_line": read.get("start_line"),
        "end_line": read.get("end_line"),
        "fallback": read.get("fallback"),
        "missing": bool(read.get("missing")),
    }
    record["estimated_tokens"] = None if record["missing"] else estimate_tokens(read.get("text", ""))
    return record


def audit_cost(routes: list[dict], root: Path = WORKSPACE_ROOT) -> dict:
    """Estimate exact hook text and resolver-selected static read ranges per route.

    A missing input makes affected static totals unknown rather than treating an
    unreadable file as zero text.  The resolver owns anchor and dependency logic.
    """
    resident_steps = []
    resident_unknown = []
    for name in RESIDENT_FILES:
        text = _read_file(root / name)
        if text is None:
            resident_unknown.append(name)
            resident_steps.append({"file": name, "estimated_tokens": None, "missing": True})
        else:
            resident_steps.append({"file": name, "estimated_tokens": estimate_tokens(text), "missing": False})
    resident_total = None if resident_unknown else sum(step["estimated_tokens"] for step in resident_steps)

    scenarios = []
    for route in routes:
        reads = resolve_route_reads(route, root)
        ranges = [_range_record(read) for read in reads]
        unknown_files = sorted({read["file"] for read in ranges if read["missing"]})
        known_ranges = [read for read in ranges if not read["missing"]]
        read_total = None if unknown_files else sum(read["estimated_tokens"] for read in known_ranges)

        full_files = []
        for file in sorted({read["file"] for read in ranges}):
            text = _read_file(root / file)
            missing = text is None
            full_files.append({
                "file": file,
                "estimated_tokens": None if missing else estimate_tokens(text),
                "missing": missing,
            })
        full_unknown = sorted(item["file"] for item in full_files if item["missing"])
        full_total = None if full_unknown else sum(item["estimated_tokens"] for item in full_files)

        hook_text = build_additional_context([route], root=root)
        hook_estimate = estimate_tokens(hook_text)
        scenarios.append({
            "mechanism": route.get("mechanism", "未命名机制"),
            "actual_hook_context": {
                "text": hook_text,
                "estimated_tokens": hook_estimate,
            },
            "full_file_baseline": {
                "files": full_files,
                "estimated_tokens": full_total,
                "unknown_files": full_unknown,
            },
            "necessary_read_ranges": {
                "ranges": ranges,
                "estimated_tokens": read_total,
                "unknown_files": unknown_files,
            },
            "static_context_estimate": {
                "estimated_tokens": None if read_total is None else hook_estimate + read_total,
                "unknown_files": unknown_files,
            },
        })
    scenarios.sort(key=lambda item: (item["static_context_estimate"]["estimated_tokens"] is None, item["static_context_estimate"]["estimated_tokens"] or 0), reverse=True)
    return {
        "estimate_basis": "按字符粗估：汉字及 U+3000–303F 标点按 1，其余每 4 字符按 1；非真实 token 或费用。",
        "actual_usage": None,
        "actual_cost": None,
        "measurement_status": "未验证：没有真实调用 usage 或账单",
        "scope_exclusions": COST_SCOPE_EXCLUSIONS,
        "resident_baseline": {
            "files": resident_steps,
            "estimated_tokens": resident_total,
            "unknown_files": resident_unknown,
        },
        "scenarios": scenarios,
    }


def _render_results(out: list[str], title: str, coverage: dict, gateable: bool) -> None:
    out.extend([title, ""])
    out.append("  " + coverage["scope"])
    for kind, label in (("positive", "应命中"), ("negative", "防误触发"), ("paraphrase", "回归语义盲区"), ("semantic", "语义真值")):
        summary = coverage["summary"][kind]
        if summary["total"]:
            out.append(f"  {label} {summary['clean']}/{summary['total']}（漏检案例 {summary['missed']}；额外触发案例 {summary['extra']}）")
    failures = [result for result in coverage["results"] if result["missed"] or result["extra"]]
    for result in failures:
        gated = gateable and result["kind"] in {"positive", "negative"}
        marker = "阻断" if gated else "诊断（不阻断）"
        details = []
        if result["missed"]:
            details.append("漏检 " + "、".join(result["missed"]))
        if result["extra"]:
            details.append("额外触发 " + "、".join(result["extra"]))
        out.append(f"  [{marker}]「{result['instruction']}」→ {'；'.join(details)}")
    if gateable and coverage["untested_mechanisms"]:
        out.append("  未被 positive 用例覆盖的机制：" + "、".join(coverage["untested_mechanisms"]))
    out.append("")


def render(coverage: dict, cost: dict) -> str:
    out = ["=" * 64, "指令路由回归与静态读取审计", "=" * 64, ""]
    _render_results(out, "## 一、回归语料（--strict 的唯一门禁）", coverage["regression"], True)
    _render_results(out, "## 二、冻结留出语料（语义诊断，不参与 --strict）", coverage["holdout"], False)
    out.extend(["## 三、静态上下文读取估算", ""])
    resident = cost["resident_baseline"]
    out.append(f"  常驻基线估算：{resident['estimated_tokens'] if resident['estimated_tokens'] is not None else '未知'}")
    for scenario in cost["scenarios"]:
        hook = scenario["actual_hook_context"]["estimated_tokens"]
        full = scenario["full_file_baseline"]["estimated_tokens"]
        needed = scenario["necessary_read_ranges"]["estimated_tokens"]
        total = scenario["static_context_estimate"]["estimated_tokens"]
        out.append(
            f"  {scenario['mechanism']}：实际 Hook 文本估算 {hook}；"
            f"唯一整文件基线 {full if full is not None else '未知'}；"
            f"必要读取范围 {needed if needed is not None else '未知'}；"
            f"静态合计 {total if total is not None else '未知'}"
        )
    heaviest = next((s for s in cost["scenarios"] if s["static_context_estimate"]["estimated_tokens"] is not None), None)
    if heaviest:
        out.append("  最重场景逐文件范围：" + heaviest["mechanism"])
        for item in heaviest["necessary_read_ranges"]["ranges"]:
            out.append(f"    {item['file']}:{item['start_line']}-{item['end_line']} 估算 {item['estimated_tokens']}")
    for scenario in cost["scenarios"]:
        for item in scenario["necessary_read_ranges"]["ranges"]:
            if item["fallback"]:
                out.append(f"  [{scenario['mechanism']}] {item['file']}：{item['fallback']}")
    out.extend(["", "  估算仅基于静态文本长度；不代表实际模型输入、输出或费用。", "  未计入：" + "；".join(COST_SCOPE_EXCLUSIONS.values()), ""])
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="审计路由回归、留出语料与静态读取范围")
    parser.add_argument("--json", action="store_true", help="输出机器可解析的 JSON")
    parser.add_argument("--strict", action="store_true", help="仅回归 positive/negative 漏检或额外触发时退出 1")
    args = parser.parse_args()

    try:
        routes = load_routes()
        coverage = audit_corpora(routes, load_cases(), load_holdout_cases())
    except AuditError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not routes:
        print("路由表为空，无可审计对象。检查 .system/config/route_map.json 的 routes 数组。", file=sys.stderr)
        return 2

    try:
        cost = audit_cost(routes)
    except ValueError as exc:
        print(f"读取计划无效：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"coverage": coverage, "cost": cost}, ensure_ascii=False, indent=2))
    else:
        print(render(coverage, cost))
    if any(s["static_context_estimate"]["unknown_files"] for s in cost["scenarios"]):
        return 2
    return 1 if args.strict and regression_failures(coverage["regression"]) else 0


if __name__ == "__main__":
    sys.exit(main())
