#!/usr/bin/env python3
"""系统自检三遍检查的执行与报告装配。

真源定义: rules/治理指令.md「系统自检」· rules/01_根系统治理.md 自迭代闭环 SOP 第 3-4 步
输出契约: rules/软件工程.md「检查按影响选，不用分数代替证据」（检查对象/证据/结论/未覆盖范围）

存在的理由（output token）：
    自检报告的「验证结论」此前逐次由模型手写，而其内容 ~85% 是三个工具 stdout 的转述
    （测试条数、阻断/建议计数、分发就绪与否）。转述既耗 output token，又给了数字漂移的
    机会——手抄的"393 tests"没有任何东西保证它等于实际跑出来的数。本工具直接从退出码与
    结构化输出装配成品表格，模型只补无法机械求值的人判断项。

设计不变量:
  1. **三态不凑数**：结论只取 通过 / 阻断 / 未验证。没有机械检查器承接的元规则公理一律
     报「未验证」并点名，严禁因"本轮没动它"或"看起来没问题"判通过——那正是《软件工程》
     「不用分数代替证据」要消除的形态。
  2. **只报不判**：本机体检建议原样进入可优化项。新克隆第 13、15c、22 项的合法初始态
     留痕完整写入验证结论，不升格为待整改项——那是「还没开始用」，不是缺陷。
  3. **报告不落盘**：默认允许体检修正 CLAUDE.md；只读检查使用 --no-fix-claude-md。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from . import paths
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths

# 新克隆缺这些声明时必须留痕，静默即阻断。留痕证明降级可见，不构成待整改项。
INITIAL_STATE_MARKERS = ("实体词表未声明", "注册表待登记", "实例声明待落地")

# 各遍次的耗时量级不同：体检是秒级，单测是分钟级，分发求值还要在临时树上再跑一遍单测。
# 统一用一个小常数会把慢机器报成「系统异常」——那正是分发检查自身踩过的坑。
LINT_TIMEOUT = 600
TESTS_TIMEOUT = 1800
DISTRIBUTION_TIMEOUT = 3600


def split_distribution_notes(notes: list[str]) -> tuple[list[str], list[str]]:
    """把分发建议拆成合法初始态留痕与其余待整改项。"""
    traces, actionable = [], []
    for note in notes:
        if any(marker in note for marker in INITIAL_STATE_MARKERS):
            traces.append(note)
        else:
            actionable.append(note)
    return traces, actionable


# 元规则九条公理 → 机械承接它的体检项编号。
# 真源是 rules/00_元规则.md；此处只记「谁在机械地守它」，不复述公理正文。
# 值为空列表 = 该公理**没有**任何检查器承接，必须如实报未验证（见设计不变量 1）。
AXIOM_COVERAGE = {
    "1. 一个事实，一个归属": ["10", "18"],
    "2. 分层覆盖与就近优先": ["33"],
    "3. 薄入口，按需读取与 Token 经济性": ["8", "12", "17", "24"],
    "4. 事实与物理实现优先": ["19b", "21"],
    "5. 最小作用量与克制": ["34", "35"],
    "6. 尊重边界与物理动作闭环": ["31"],
    "7. 本地优先与自闭环": ["22"],
    "8. 动作驱动优于场景穷举": ["3", "7"],
    "9. 三权分立与空间纯净": ["1", "2", "13", "19", "20"],
}


def _run(argv: list[str], root: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, cwd=str(root),
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 1, "", f"❌ 检查执行失败: {exc}\n👉 修复建议: 检查工作区路径、执行环境与超时后重跑。")


def run_unittest(root: Path) -> dict:
    """第二遍门禁之一：单元测试。条数从 unittest 自身输出取，不手抄。"""
    proc = _run([sys.executable, "-m", "unittest", "discover",
                 "-s", f"{paths.SYSTEM_DIRNAME}/tests", "-t", paths.SYSTEM_DIRNAME], root, TESTS_TIMEOUT)
    tail = proc.stderr or proc.stdout
    ran = re.search(r"^Ran (\d+) tests? in ([\d.]+)s", tail, re.MULTILINE)
    ok = re.search(r"^OK(?:\s|$)", tail, re.MULTILINE) is not None
    failures = re.search(r"failures=(\d+)", tail)
    errors = re.search(r"errors=(\d+)", tail)
    return {
        "tests": int(ran.group(1)) if ran else None,
        "seconds": float(ran.group(2)) if ran else None,
        "ok": ok and proc.returncode == 0 and ran is not None and int(ran.group(1)) > 0,
        "failures": int(failures.group(1)) if failures else 0,
        "errors": int(errors.group(1)) if errors else 0,
        "exit_code": proc.returncode,
        "output_tail": tail.strip()[-400:],
    }


def _self_reported_failure(result: dict) -> bool:
    """JSON 已自报未通过时，非 0 退出码是同一事实，不得再标成解析失败。"""
    if result.get("passed") is False:
        return True
    blocking_count = result.get("blocking_count")
    if isinstance(blocking_count, int) and blocking_count > 0:
        return True
    if result.get("status") == "fail":
        return True
    blocking = result.get("blocking")
    return isinstance(blocking, list) and len(blocking) > 0


def _json_result(proc: subprocess.CompletedProcess) -> dict:
    try:
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError("检查输出不是 JSON 对象")
    except ValueError as exc:
        result = {"error": (proc.stdout + proc.stderr).strip()[:400] or str(exc)}
    if proc.returncode != 0 and not _self_reported_failure(result):
        result["error"] = result.get("error") or f"检查退出码 {proc.returncode}: {proc.stderr.strip()[:400]}"
    return result


def _lint_evidence(lint: dict) -> str:
    blocking = lint.get("blocking_count")
    advisory = lint.get("advisory_count")
    if isinstance(blocking, int) and isinstance(advisory, int):
        return f"阻断 {blocking} 项 · 建议 {advisory} 项"
    error = lint.get("error") or "缺少阻断计数"
    return f"体检输出解析失败: {str(error)[:120]}"


def _gate_failure_line(name: str, result: dict) -> str | None:
    """明细已列入时，不再把退出码重复写成一条阻断。"""
    detail = result.get("error") or result.get("output_tail")
    checks = result.get("checks") or []
    itemized = (
        (name == "体检" and any(isinstance(c, dict) and c.get("blocking") for c in checks))
        or (name == "分发" and isinstance(result.get("blocking"), list) and bool(result["blocking"]))
    )
    if itemized and (not detail or str(detail).startswith("检查退出码")):
        return None
    return f"[{name}] {detail or '检查未通过，请查看该项证据与明细。'}"


def run_lint(root: Path, fix_claude_md: bool = True) -> dict:
    argv = [sys.executable, str(root / paths.SYSTEM_DIRNAME / "tools/lint_workspace.py"), "--json"]
    if fix_claude_md:
        argv.append("--fix-claude-md")
    return _json_result(_run(argv, root, LINT_TIMEOUT))


def run_distribution(root: Path) -> dict:
    return _json_result(_run([sys.executable, str(root / paths.SYSTEM_DIRNAME / "tools/check_distribution.py"), "--json"],
                             root, DISTRIBUTION_TIMEOUT))


def _dist_counts(dist: dict) -> tuple[int | None, int | None, list[str]]:
    """check_distribution 的字段名随版本有差异，按多个候选键取值，取不到就报 None。"""
    def pick(*names):
        for n in names:
            if isinstance(dist.get(n), int):
                return dist[n]
            if isinstance(dist.get(n), list):
                return len(dist[n])
        return None
    blocking = pick("blocking_count", "blocking", "阻断项")
    advisory = pick("advisory_count", "advisory", "建议项")
    notes = dist.get("advisory") or dist.get("advisories") or []
    return blocking, advisory, [str(n) for n in (notes if isinstance(notes, list) else [])]


def assemble(root: Path, fix_claude_md: bool = True) -> dict:
    tests = run_unittest(root)
    lint = run_lint(root, fix_claude_md=fix_claude_md)
    dist = run_distribution(root)

    executed = {title.split(".", 1)[0] for title in lint.get("passed_checks", [])}
    executed.update(chk["title"].split(".", 1)[0] for chk in lint.get("checks", []))
    coverage_gaps = [f"[公理承接缺失] {a} 引用的体检项 {c} 未出现在本轮执行结果。"
                     for a, ids in AXIOM_COVERAGE.items() for c in ids if c not in executed]
    covered = {a: c for a, c in AXIOM_COVERAGE.items() if c and set(c) <= executed}
    uncovered = [a for a, c in AXIOM_COVERAGE.items() if not c]

    dist_block, dist_adv, dist_notes = _dist_counts(dist)
    initial_traces, dist_actionable = split_distribution_notes(dist_notes)
    lint_ok = lint.get("passed") is True and lint.get("blocking_count") == 0 and "error" not in lint
    dist_ok = "error" not in dist and dist_block == 0 and dist.get("status") != "fail"

    rows = [
        {
            "对象": "单元测试套件（.entropaxis/tests）",
            "证据": (f"Ran {tests['tests']} tests in {tests['seconds']}s · OK"
                     if tests["ok"] else
                     f"退出码 {tests['exit_code']} · tests={tests['tests']} failures={tests['failures']} errors={tests['errors']}"),
            "结论": "通过" if tests["ok"] else "阻断",
        },
        {
            "对象": f"工作区体检（{lint.get('total_checks', '?')} 项）",
            "证据": _lint_evidence(lint),
            "结论": "通过" if lint_ok else "阻断",
        },
        {
            "对象": "收件方视角分发求值（新克隆工作区）",
            "证据": (f"阻断 {dist_block} 项 · 建议 {dist_adv if dist_adv is not None else '?'} 项"
                     + (f"；已扫描 {'、'.join(dist['scans'])}" if dist.get("scans") else "")
                     + (f"；合法初始态留痕 {len(initial_traces)} 项" if initial_traces else "")
                     if "error" not in dist else f"分发核验失败: {dist['error'][:120]}"),
            "结论": "通过" if dist_ok else "阻断",
        },
        {
            "对象": "元规则九条公理机械承接",
            "证据": (
                f"{len(covered)}/9 条有体检项承接并已执行"
                if not uncovered else
                f"{len(covered)}/9 条有体检项承接；第 "
                + "、".join(a.split('.', 1)[0] for a in uncovered) + " 条无机械检查器"
            ),
            "结论": "阻断" if coverage_gaps else ("未验证" if uncovered else "通过"),
        },
        {
            "对象": "CLAUDE.md 薄壳纯净度自动改写",
            "证据": (f"--fix-claude-md 改写 {len(lint.get('fixed_claude_md', []))} 个文件"
                     if fix_claude_md else "本轮未执行 --fix-claude-md"),
            "结论": "通过" if (fix_claude_md and lint_ok) else "未验证",
        },
    ]

    advisories = []
    for chk in lint.get("checks", []):
        if not chk.get("blocking"):
            advisories += [f"[{chk['title']}] {i}" for i in chk["issues"]]
    blocking = []
    for chk in lint.get("checks", []):
        if chk.get("blocking"):
            blocking += [f"[{chk['title']}] {i}" for i in chk["issues"]]
    blocking += coverage_gaps
    blocking += [f"[分发] {i}" for i in dist.get("blocking", [])] if isinstance(dist.get("blocking"), list) else []
    for name, result, passed in (("单元测试", tests, tests["ok"]), ("体检", lint, lint_ok), ("分发", dist, dist_ok)):
        if not passed:
            line = _gate_failure_line(name, result)
            if line:
                blocking.append(line)

    gates_passed = tests["ok"] and lint_ok and dist_ok and not coverage_gaps
    return {
        "passed": gates_passed,
        "rows": rows,
        "blocking": blocking,
        "advisories": advisories,
        "distribution_notes": dist_notes,
        "distribution_actionable": dist_actionable,
        "initial_state_traces": initial_traces,
        "uncovered_axioms": uncovered,
        "coverage_gaps": coverage_gaps,
        "axiom_coverage": {a: {"declared": ids, "executed": [c for c in ids if c in executed]}
                           for a, ids in AXIOM_COVERAGE.items()},
        "raw": {"tests": tests, "lint_summary": {k: lint.get(k) for k in
                ("total_checks", "blocking_count", "advisory_count", "passed")}, "distribution": dist},
    }


def render_markdown(report: dict) -> str:
    out = ["## 验证结论", "", "| 检查对象 | 证据 | 结论 |", "|---|---|---|"]
    for r in report["rows"]:
        out.append(f"| {r['对象']} | {r['证据']} | {r['结论']} |")

    if report["uncovered_axioms"]:
        uncovered = "、".join(
            f"第 {a.split('.', 1)[0]} 条（{a.split('. ', 1)[-1]}）" for a in report["uncovered_axioms"]
        )
        gap = f"元规则{uncovered}无机械检查器承接，本轮未验证。"
    else:
        gap = "九条公理均有体检项承接。"
    out += ["",
            "**未覆盖范围**：" + gap +
            "第 33 项只拦截明示放宽语句，以及动态执行和系统外壳调用；同义改写不在检测内。"
            "第 34 项只拦截禁用第三方依赖。第 35 项只列出零引用规则，不自动删除。"
            "第 31 项证明源码未直接调用平台打开命令；本报告不落盘，本轮无打开义务。"
            "语法税超过 50% 时第 24 项写明去表化这一兑现方式。"
            "价值回收由第 30 项检查；跨平台回退由第 36 项检查；工具异常路径由第 37 项检查。"
            "分发扫描凭据特征串、家目录路径与实体词；词表、空注册表或实例声明缺失时必须留痕，静默跳过会阻断。"
            "业务正文与 credentials/ 的内容不进入这些扫描。"]
    traces = report.get("initial_state_traces") or []
    if traces:
        out += ["",
                "**新克隆合法初始态留痕**（第 13、15c、22 项已核验；静默缺失会阻断。"
                "本机已初始化时不待整改）："]
        out += [f"- {item}" for item in traces]

    out += ["", "## 可优化项", ""]
    if report["blocking"]:
        out += [f"- **[阻断]** {b}" for b in report["blocking"]]
    if report["advisories"]:
        out += [f"- {a}" for a in report["advisories"]]
    actionable = report.get("distribution_actionable")
    if actionable is None:
        actionable = report.get("distribution_notes") or []
    out += [f"- {a}" for a in actionable]
    if not report["blocking"] and not report["advisories"] and not actionable:
        out.append("本轮无待优化项；已执行的机械门禁通过，未覆盖范围见上。")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="系统自检三遍检查执行与报告装配（《治理指令》系统自检）")
    parser.add_argument("--root", type=Path, default=paths.WORKSPACE_ROOT, help="工作区根")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 而非 Markdown")
    parser.add_argument("--no-fix-claude-md", action="store_true", help="跳过 CLAUDE.md 自动改写")
    args = parser.parse_args()

    report = assemble(args.root.expanduser().resolve(), fix_claude_md=not args.no_fix_claude_md)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_markdown(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
