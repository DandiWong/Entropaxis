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
  2. **只报不判**：本工具不对可优化项做取舍，原样透传体检建议项；是否整改由人裁决。
  3. **零写入**：纯只读装配，不落盘任何文件。报告是否落盘按《文件交付》第 1 节门禁另行决定。
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

# 元规则九条公理 → 机械承接它的体检项编号。
# 真源是 rules/00_元规则.md；此处只记「谁在机械地守它」，不复述公理正文。
# 值为空列表 = 该公理**没有**任何检查器承接，必须如实报未验证（见设计不变量 1）。
AXIOM_COVERAGE = {
    "1. 一个事实，一个归属": ["10", "18"],
    "2. 分层覆盖与就近优先": [],
    "3. 薄入口，按需读取与 Token 经济性": ["8", "12", "17", "24"],
    "4. 事实与物理实现优先": ["19b", "21"],
    "5. 最小作用量与克制": [],
    "6. 尊重边界与物理动作闭环": ["15b"],
    "7. 本地优先与自闭环": ["22"],
    "8. 动作驱动优于场景穷举": ["3", "7"],
    "9. 三权分立与空间纯净": ["1", "2", "13", "19", "20", "23"],
}


def _run(argv: list[str], root: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, cwd=str(root),
                          timeout=timeout, check=False)


def run_unittest(root: Path) -> dict:
    """第二遍门禁之一：单元测试。条数从 unittest 自身输出取，不手抄。"""
    proc = _run([sys.executable, "-m", "unittest", "discover",
                 "-s", f"{paths.SYSTEM_DIRNAME}/tests", "-t", paths.SYSTEM_DIRNAME], root)
    tail = proc.stderr or proc.stdout
    ran = re.search(r"^Ran (\d+) tests? in ([\d.]+)s", tail, re.MULTILINE)
    ok = re.search(r"^OK(?:\s|$)", tail, re.MULTILINE) is not None
    failures = re.search(r"failures=(\d+)", tail)
    errors = re.search(r"errors=(\d+)", tail)
    return {
        "tests": int(ran.group(1)) if ran else None,
        "seconds": float(ran.group(2)) if ran else None,
        "ok": ok and proc.returncode == 0,
        "failures": int(failures.group(1)) if failures else 0,
        "errors": int(errors.group(1)) if errors else 0,
        "exit_code": proc.returncode,
    }


def run_lint(root: Path, fix_claude_md: bool = True) -> dict:
    argv = [sys.executable, f"{paths.SYSTEM_DIRNAME}/tools/lint_workspace.py", "--json"]
    if fix_claude_md:
        argv.append("--fix-claude-md")
    proc = _run(argv, root)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": (proc.stdout + proc.stderr)[:400], "passed": False,
                "blocking_count": None, "advisory_count": None, "checks": []}


def run_distribution(root: Path) -> dict:
    proc = _run([sys.executable, f"{paths.SYSTEM_DIRNAME}/tools/check_distribution.py", "--json"], root)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": (proc.stdout + proc.stderr)[:400], "exit_code": proc.returncode}


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

    covered = {a: c for a, c in AXIOM_COVERAGE.items() if c}
    uncovered = [a for a, c in AXIOM_COVERAGE.items() if not c]

    dist_block, dist_adv, dist_notes = _dist_counts(dist)
    lint_ok = bool(lint.get("passed")) and "error" not in lint
    dist_ok = ("error" not in dist) and (dist_block == 0 if dist_block is not None else False)

    rows = [
        {
            "对象": "单元测试套件（.entropaxis/tests）",
            "证据": (f"Ran {tests['tests']} tests in {tests['seconds']}s · OK"
                     if tests["ok"] else
                     f"退出码 {tests['exit_code']} · failures={tests['failures']} errors={tests['errors']}"),
            "结论": "通过" if tests["ok"] else "阻断",
        },
        {
            "对象": f"工作区体检（{lint.get('total_checks', '?')} 项）",
            "证据": (f"阻断 {lint.get('blocking_count')} 项 · 建议 {lint.get('advisory_count')} 项"
                     if "error" not in lint else f"体检输出解析失败: {lint['error'][:120]}"),
            "结论": "通过" if lint_ok else "阻断",
        },
        {
            "对象": "收件方视角分发求值（新克隆工作区）",
            "证据": (f"阻断 {dist_block} 项 · 建议 {dist_adv if dist_adv is not None else '?'} 项"
                     if "error" not in dist else f"分发核验失败: {dist['error'][:120]}"),
            "结论": "通过" if dist_ok else "阻断",
        },
        {
            "对象": "元规则九条公理机械承接",
            "证据": f"{len(covered)}/9 条有体检项承接；第 "
                    + "、".join(a.split(".")[0] for a in uncovered) + " 条无机械检查器",
            "结论": "部分未验证",
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

    gates_passed = tests["ok"] and lint_ok and dist_ok
    return {
        "passed": gates_passed,
        "rows": rows,
        "blocking": blocking,
        "advisories": advisories,
        "distribution_notes": dist_notes,
        "uncovered_axioms": uncovered,
        "raw": {"tests": tests, "lint_summary": {k: lint.get(k) for k in
                ("total_checks", "blocking_count", "advisory_count", "passed")}, "distribution": dist},
    }


def render_markdown(report: dict) -> str:
    out = ["## 验证结论", "", "| 检查对象 | 证据 | 结论 |", "|---|---|---|"]
    for r in report["rows"]:
        out.append(f"| {r['对象']} | {r['证据']} | {r['结论']} |")

    uncovered = "、".join(
        f"第 {a.split('.', 1)[0]} 条（{a.split('. ', 1)[-1]}）" for a in report["uncovered_axioms"]
    )
    out += ["", f"**未覆盖范围**：元规则{uncovered}属价值判断，无机械检查器承接，本轮未验证；"
                f"业务项目自身内容与 `.entropaxis/data/` 实例数据按机制定义不在自检范围。"]
    if report["distribution_notes"]:
        out.append(f"分发核验另有建议项 {len(report['distribution_notes'])} 类（不阻断）。")

    out += ["", "## 可优化项", ""]
    if report["blocking"]:
        out += [f"- **[阻断]** {b}" for b in report["blocking"]]
    if report["advisories"]:
        out += [f"- {a}" for a in report["advisories"]]
    if not report["blocking"] and not report["advisories"]:
        out.append("本轮无待优化项，全项健康。")
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
