#!/usr/bin/env python3
"""指令路由覆盖率与上下文加载成本审计 (Routing Coverage & Context Cost Auditor).

回答两个问题：
  1. 路由是否有效——测试集中的指令是否命中了该场景应当执行的规则？
  2. 成本花在哪——每个场景从常驻层到规则读取，逐步消耗多少上下文？

真源：
  路由表   .system/config/route_map.json
  测试集   .system/tests/fixtures/instruction_cases.json

执行方式:
  python3 .system/tools/audit_routing.py             # 人读报告
  python3 .system/tools/audit_routing.py --json      # 机器可解析输出
  python3 .system/tools/audit_routing.py --strict    # 存在漏检/误触发时以退出码 1 阻断
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM_ROOT = HERE.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent
ROUTE_MAP_PATH = SYSTEM_ROOT / "config" / "route_map.json"
CASES_PATH = SYSTEM_ROOT / "tests" / "fixtures" / "instruction_cases.json"

# 常驻层：每轮会话无条件进入上下文，与是否命中路由无关
RESIDENT_FILES = ("AGENTS.md", "CLAUDE.md")


class AuditError(Exception):
    """行动导向错误：消息须同时给出原因与修复指引。"""


def estimate_tokens(text: str) -> int:
    """估算 token 数。

    CJK 字符按 1 token 计，其余按 4 字符 1 token 计——这是量级估算而非精确计量，
    用于横向比较各场景的相对成本，不可作为计费依据。
    """
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    return cjk + (len(text) - cjk + 3) // 4


def _load_json(path: Path, hint: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuditError(f"❌ 找不到 {path}\n👉 {hint}")
    except json.JSONDecodeError as exc:
        raise AuditError(f"❌ {path} 不是合法 JSON：{exc}\n👉 修正语法后重跑；勿手工拼接 JSON。")


def load_routes(path: Path = ROUTE_MAP_PATH) -> list[dict]:
    data = _load_json(path, "路由表缺失会使字面触发词兜底完全失效，请从版本库恢复 route_map.json。")
    return data.get("routes", [])


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    data = _load_json(path, "测试集缺失，请按 fixtures/instruction_cases.json 的 _meta.kinds 结构创建。")
    return data.get("cases", [])


def normalize(text: str) -> str:
    """归一化待匹配文本：小写 + 去所有空白。与 hook_route_match.normalize 同口径。"""
    return "".join(text.lower().split())


def extract_section(text: str, anchor: str) -> str:
    """截取 anchor 指向的小节正文（含标题行，至下一个同级或更高级标题止）。

    anchor 形如 `## 审计` 或 `### 4.2 触发场景`。找不到时返回空串，由调用方按整篇计。
    """
    anchor = anchor.strip()
    if not anchor.startswith("#"):
        return ""
    level = len(anchor) - len(anchor.lstrip("#"))
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == anchor), None)
    if start is None:
        return ""
    for j in range(start + 1, len(lines)):
        s = lines[j].lstrip()
        if s.startswith("#"):
            if len(s) - len(s.lstrip("#")) <= level:
                return "\n".join(lines[start:j])
    return "\n".join(lines[start:])


def match_prompt(prompt: str, routes: list[dict]) -> list[dict]:
    """与 hook_route_match.match_prompt 同源的字面匹配。

    此处独立实现而非 import：hook 在 harness 侧运行，审计工具须能在 hook 未安装
    或被禁用时独立复现同一判定，两者的一致性由 test_audit_routing 交叉断言保证。
    """
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
        if hit and any(normalize(x) in text for x in route.get("exclude", [])):
            hit = False
        if hit:
            hits.append(route)
    return hits


def audit_coverage(routes: list[dict], cases: list[dict]) -> dict:
    """逐条跑测试集，按 kind 分组统计命中情况。"""
    results = []
    for case in cases:
        hit_names = [r.get("mechanism", "") for r in match_prompt(case["instruction"], routes)]
        expected = case.get("expect", [])
        missed = [m for m in expected if m not in hit_names]
        extra = [m for m in hit_names if m not in expected]
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
    for kind in ("positive", "negative", "paraphrase"):
        group = [r for r in results if r["kind"] == kind]
        clean = [r for r in group if not r["missed"] and not r["extra"]]
        summary[kind] = {"total": len(group), "clean": len(clean)}

    # 未被任何用例覆盖的机制：路由表登记了却从未被测过，等于没有回归保护
    tested = {m for r in results if r["kind"] == "positive" for m in r["expect"]}
    untested = [r.get("mechanism", "") for r in routes if r.get("mechanism") not in tested]

    return {"results": results, "summary": summary, "untested_mechanisms": untested}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def audit_cost(routes: list[dict], root: Path = WORKSPACE_ROOT) -> dict:
    """逐场景计量上下文加载成本，拆分为常驻层、Hook 注入与规则读取三步。"""
    resident_steps = []
    for name in RESIDENT_FILES:
        text = _read(root / name)
        if text:
            resident_steps.append({"file": name, "tokens": estimate_tokens(text)})
    resident_total = sum(s["tokens"] for s in resident_steps)

    scenarios = []
    for route in routes:
        mechanism = route.get("mechanism", "未命名机制")
        anchor = route.get("anchor", "")
        hook_text = f"机制「{mechanism}」→ {' '.join(route.get('files', []))}（{anchor}）"
        anchors = [a.strip() for a in anchor.split("|")] if anchor else []
        rule_steps = []
        secondary = set()
        for idx, rel in enumerate(route.get("files", [])):
            text = _read(root / rel)
            if not text:
                rule_steps.append({"file": rel, "tokens": 0, "anchor_tokens": 0, "missing": True})
                continue
            full = estimate_tokens(text)
            section = extract_section(text, anchors[idx]) if idx < len(anchors) else ""
            rule_steps.append({
                "file": rel,
                "tokens": full,
                # 无锚点或锚点失配时按整篇计，避免把「测不到」记成「省下了」
                "anchor_tokens": estimate_tokens(section) if section else full,
            })
            # 二级引用：正文里指向的其他规则文件，实际执行时往往被追加读取
            for other in routes:
                for other_rel in other.get("files", []):
                    name = Path(other_rel).name
                    if name != Path(rel).name and name in text:
                        secondary.add(other_rel)

        rule_total = sum(s["tokens"] for s in rule_steps)
        anchor_total = sum(s["anchor_tokens"] for s in rule_steps)
        hook_tokens = estimate_tokens(hook_text)
        scenarios.append({
            "mechanism": mechanism,
            "steps": {
                "1_常驻层": resident_total,
                "2_Hook注入": hook_tokens,
                "3_规则读取": rule_total,
            },
            "rule_files": rule_steps,
            "secondary_refs": sorted(secondary),
            "total": resident_total + hook_tokens + rule_total,
            "total_anchor_only": resident_total + hook_tokens + anchor_total,
        })

    scenarios.sort(key=lambda s: s["total"], reverse=True)
    return {"resident": {"steps": resident_steps, "total": resident_total}, "scenarios": scenarios}


def render(coverage: dict, cost: dict) -> str:
    out = ["=" * 64, "指令路由覆盖率与上下文加载成本审计", "=" * 64, "", "## 一、路由覆盖率", ""]

    s = coverage["summary"]
    out.append(f"  正向命中   {s['positive']['clean']}/{s['positive']['total']}   （应命中且已命中）")
    out.append(f"  防误触发   {s['negative']['clean']}/{s['negative']['total']}   （不应命中且未命中）")
    out.append(
        f"  语义盲区   {s['paraphrase']['clean']}/{s['paraphrase']['total']}   "
        "（字面表覆盖不到，不计入失败，数字越低说明依赖模型语义判断越多）"
    )
    out.append("")

    failures = [r for r in coverage["results"] if r["kind"] != "paraphrase" and (r["missed"] or r["extra"])]
    if failures:
        out.append("  ❌ 需修复：")
        for r in failures:
            problem = []
            if r["missed"]:
                problem.append(f"漏检 {','.join(r['missed'])}")
            if r["extra"]:
                problem.append(f"误触发 {','.join(r['extra'])}")
            out.append(f"     「{r['instruction']}」→ {'；'.join(problem)}")
        out.append("")

    blind = [r for r in coverage["results"] if r["kind"] == "paraphrase" and r["missed"]]
    if blind:
        out.append("  ⚠️ 语义盲区（须由模型按元规则第 8 条判断，字面表兜不住）：")
        for r in blind:
            out.append(f"     「{r['instruction']}」→ 期望 {','.join(r['missed'])}｜{r['note']}")
        out.append("")

    if coverage["untested_mechanisms"]:
        out.append(f"  ⚠️ 无用例覆盖的机制：{'、'.join(coverage['untested_mechanisms'])}")
        out.append("")

    out += ["## 二、上下文加载成本（token 量级估算）", ""]
    res = cost["resident"]
    out.append(f"  常驻层（每轮固定）：{res['total']} tokens　" + "、".join(
        f"{x['file']} {x['tokens']}" for x in res["steps"]
    ))
    out.append("")
    out.append(f"  {'机制':<14}{'常驻':>7}{'Hook':>7}{'规则读取':>10}{'合计':>8}{'仅读锚点':>10}{'可省':>8}")
    out.append("  " + "-" * 66)
    for sc in cost["scenarios"]:
        st = sc["steps"]
        saved = sc["total"] - sc["total_anchor_only"]
        pct = f"{saved * 100 // sc['total']}%" if sc["total"] else "-"
        out.append(
            f"  {sc['mechanism']:<14}{st['1_常驻层']:>7}{st['2_Hook注入']:>7}"
            f"{st['3_规则读取']:>10}{sc['total']:>8}{sc['total_anchor_only']:>10}{pct:>8}"
        )
    out.append("")
    out.append("  「仅读锚点」= 只读 anchor 指向的小节而非整篇。差额即整篇读取的浪费量。")
    out.append("")

    heaviest = cost["scenarios"][0] if cost["scenarios"] else None
    if heaviest:
        out.append("  最重场景逐文件明细：" + heaviest["mechanism"])
        for f in heaviest["rule_files"]:
            flag = "  ← 文件缺失" if f.get("missing") else ""
            out.append(f"     {f['file']}  整篇 {f['tokens']} / 锚点 {f['anchor_tokens']} tokens{flag}")
        if heaviest["secondary_refs"]:
            out.append("     二级引用（正文指向、实际常被追加读取）：" + "、".join(heaviest["secondary_refs"]))
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="审计指令路由覆盖率与各场景上下文加载成本")
    parser.add_argument("--json", action="store_true", help="输出机器可解析的 JSON")
    parser.add_argument("--strict", action="store_true", help="存在漏检或误触发时以退出码 1 阻断")
    args = parser.parse_args()

    try:
        routes = load_routes()
        cases = load_cases()
    except AuditError as exc:
        print(exc, file=sys.stderr)
        return 2

    if not routes:
        print("❌ 路由表为空，无可审计对象。\n👉 检查 .system/config/route_map.json 的 routes 数组。", file=sys.stderr)
        return 2

    coverage = audit_coverage(routes, cases)
    cost = audit_cost(routes)

    if args.json:
        print(json.dumps({"coverage": coverage, "cost": cost}, ensure_ascii=False, indent=2))
    else:
        print(render(coverage, cost))

    if args.strict:
        failures = [r for r in coverage["results"] if r["kind"] != "paraphrase" and (r["missed"] or r["extra"])]
        if failures:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
