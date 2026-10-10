"""report_selfcheck 的装配逻辑、三态结论与漂移守卫。

注意：本文件**不调用** assemble() 的真实执行路径——它内部会 `python -m unittest discover`，
在测试套件里跑等于递归自举。所有装配用例一律注入桩函数，只验证装配与渲染逻辑本身。
"""
import ast
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import report_selfcheck as rs

SYSTEM_ROOT = Path(__file__).resolve().parent.parent

GREEN_TESTS = {"tests": 393, "seconds": 35.8, "ok": True, "failures": 0, "errors": 0, "exit_code": 0}
GREEN_LINT = {"total_checks": 24, "blocking_count": 0, "advisory_count": 1, "passed": True,
              "fixed_claude_md": [], "checks": [
                  {"title": "17. 规则文件行数预算检查", "blocking": False, "issues": ["[规则超预算] 某文件超线"]}]}
GREEN_DIST = {"status": "pass", "blocking": [], "advisory": ["[新环境体检建议] 4 类建议项"]}
LINT_TREE = ast.parse((SYSTEM_ROOT / "tools/lint_workspace.py").read_text(encoding="utf-8"))
CHECK_TITLES = next([item.elts[0].value for item in node.value.elts]
                    for node in ast.walk(LINT_TREE) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "checks" for t in node.targets))
GREEN_LINT["passed_checks"] = [t for t in CHECK_TITLES if not t.startswith("17.")]


def _assemble(tests=None, lint=None, dist=None):
    with mock.patch.object(rs, "run_unittest", return_value=tests or dict(GREEN_TESTS)), \
         mock.patch.object(rs, "run_lint", return_value=lint or dict(GREEN_LINT)), \
         mock.patch.object(rs, "run_distribution", return_value=dist or dict(GREEN_DIST)):
        return rs.assemble(SYSTEM_ROOT.parent)


class AxiomCoverageDriftTests(unittest.TestCase):
    def test_coverage_table_matches_meta_rule_axioms(self):
        """AXIOM_COVERAGE 的条目必须与 00_元规则.md 的九条公理逐条对齐（防真源漂移）。"""
        text = (SYSTEM_ROOT / "rules" / "00_元规则.md").read_text(encoding="utf-8")
        axioms = re.findall(r"^(\d+)\.\s+\*\*([^*]+?)\*\*", text, re.MULTILINE)
        self.assertEqual(len(axioms), 9, "元规则应恰好九条公理")
        expected = {f"{num}. {name.strip()}" for num, name in axioms}
        self.assertEqual(set(rs.AXIOM_COVERAGE), expected,
                         "AXIOM_COVERAGE 与 00_元规则.md 已漂移，须同步")

    def test_uncovered_axioms_are_declared_not_silently_passed(self):
        report = _assemble()
        self.assertTrue(report["uncovered_axioms"], "无机械承接的公理必须被点名")
        row = next(r for r in report["rows"] if "元规则" in r["对象"])
        self.assertEqual(row["结论"], "未验证")

    def test_coverage_references_registered_checks(self):
        registered = {t.split(".", 1)[0] for t in CHECK_TITLES}
        for axiom, ids in rs.AXIOM_COVERAGE.items():
            self.assertFalse(set(ids) - registered, axiom)

    def test_missing_execution_evidence_blocks(self):
        lint = {**GREEN_LINT, "passed_checks": [t for t in GREEN_LINT["passed_checks"] if not t.startswith("24.")]}
        report = _assemble(lint=lint)
        self.assertFalse(report["passed"])
        self.assertTrue(any("24" in gap for gap in report["coverage_gaps"]))


class AssemblyTests(unittest.TestCase):
    def test_all_green_passes(self):
        report = _assemble()
        self.assertTrue(report["passed"])
        self.assertEqual([r["结论"] for r in report["rows"][:3]], ["通过", "通过", "通过"])

    def test_failing_tests_block(self):
        red = {**GREEN_TESTS, "ok": False, "failures": 2, "exit_code": 1}
        report = _assemble(tests=red)
        self.assertFalse(report["passed"])
        self.assertEqual(report["rows"][0]["结论"], "阻断")
        self.assertIn("failures=2", report["rows"][0]["证据"])

    def test_lint_blocking_issue_blocks_and_is_listed(self):
        red = {**GREEN_LINT, "blocking_count": 1, "passed": False,
               "checks": [{"title": "1. 结构完整性", "blocking": True, "issues": ["缺目录"]}]}
        report = _assemble(lint=red)
        self.assertFalse(report["passed"])
        self.assertTrue(any("缺目录" in b for b in report["blocking"]))

    def test_distribution_blocking_blocks(self):
        red = {"status": "fail", "blocking": ["[缺文件] x"], "advisory": []}
        report = _assemble(dist=red)
        self.assertFalse(report["passed"])
        self.assertEqual(report["rows"][2]["结论"], "阻断")
        self.assertIn("[缺文件] x", rs.render_markdown(report))

    def test_advisories_passed_through_verbatim(self):
        """可优化项只透传不裁剪——是否整改由人裁决。"""
        report = _assemble()
        self.assertEqual(len(report["advisories"]), 1)
        self.assertIn("[规则超预算] 某文件超线", report["advisories"][0])

    def test_unknown_distribution_shape_blocks_with_action(self):
        report = _assemble(dist={"unexpected": True})
        self.assertFalse(report["passed"])
        self.assertTrue(report["blocking"])

    def test_error_details_survive_json_and_markdown(self):
        report = _assemble(lint={"error": "运行超时"})
        self.assertFalse(report["passed"])
        self.assertIn("运行超时", rs.render_markdown(report))
        json.dumps(report)

    def test_contradictory_lint_counts_cannot_pass(self):
        self.assertFalse(_assemble(lint={**GREEN_LINT, "blocking_count": 1})["passed"])


class DistCountsTests(unittest.TestCase):
    def test_reads_list_shaped_keys(self):
        blocking, advisory, notes = rs._dist_counts(GREEN_DIST)
        self.assertEqual((blocking, advisory), (0, 1))
        self.assertEqual(len(notes), 1)

    def test_reads_int_shaped_keys(self):
        blocking, advisory, _ = rs._dist_counts({"blocking_count": 2, "advisory_count": 3})
        self.assertEqual((blocking, advisory), (2, 3))

    def test_unknown_shape_yields_none_not_zero(self):
        """取不到就报 None，不得把"没读到"静默当成"零阻断"。"""
        blocking, _, _ = rs._dist_counts({"unexpected": True})
        self.assertIsNone(blocking)


class RenderTests(unittest.TestCase):
    def test_renders_exactly_two_sections(self):
        md = rs.render_markdown(_assemble())
        self.assertEqual(re.findall(r"^## (.+)$", md, re.MULTILINE), ["验证结论", "可优化项"])

    def test_uncovered_range_reads_naturally(self):
        md = rs.render_markdown(_assemble())
        self.assertIn("**未覆盖范围**：", md)
        self.assertRegex(md, r"第 \d+ 条（[^）]+）")

    def test_all_healthy_states_no_pending_items(self):
        clean = {**GREEN_LINT, "advisory_count": 0, "checks": [], "passed_checks": CHECK_TITLES}
        md = rs.render_markdown(_assemble(lint=clean, dist={"status": "pass", "blocking": [], "advisory": []}))
        self.assertIn("本轮无待优化项", md)

    def test_distribution_advisories_are_rendered(self):
        md = rs.render_markdown(_assemble())
        self.assertIn(GREEN_DIST["advisory"][0], md)
        self.assertNotIn("全项健康", md)

    def test_all_verdicts_use_three_states(self):
        self.assertTrue(all(r["结论"] in {"通过", "阻断", "未验证"} for r in _assemble()["rows"]))


class ExecutionTests(unittest.TestCase):
    def test_unittest_zero_tests_and_missing_summary_do_not_pass(self):
        for output in ("Ran 0 tests in 0.001s\n\nOK\n", "OK\n"):
            with self.subTest(output=output), mock.patch.object(rs, "_run", return_value=subprocess.CompletedProcess([], 0, "", output)):
                self.assertFalse(rs.run_unittest(SYSTEM_ROOT.parent)["ok"])

    def test_unittest_result_is_parsed(self):
        proc = subprocess.CompletedProcess([], 0, "", "Ran 3 tests in 0.123s\n\nOK (skipped=1)\n")
        with mock.patch.object(rs, "_run", return_value=proc):
            result = rs.run_unittest(SYSTEM_ROOT.parent)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tests"], 3)

    def test_timeout_and_oserror_become_blocking_results(self):
        for error in (subprocess.TimeoutExpired(["python"], 1), OSError("不可执行")):
            with self.subTest(error=error), mock.patch.object(rs.subprocess, "run", side_effect=error):
                report = rs.assemble(SYSTEM_ROOT.parent)
                self.assertFalse(report["passed"])
                self.assertIn("修复建议", rs.render_markdown(report))

    def test_invalid_json_and_nonzero_exit_are_not_green(self):
        for output, code in (("garbled", 0), ("[]", 0), ('{"passed": true, "blocking_count": 0}', 1)):
            with self.subTest(output=output, code=code):
                self.assertIn("error", rs._json_result(subprocess.CompletedProcess([], code, output, "")))

    def test_alternate_root_selects_its_own_checkers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(rs, "_run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
                rs.run_lint(root, fix_claude_md=False)
                rs.run_distribution(root)
            for call in run.call_args_list:
                self.assertTrue(Path(call.args[0][1]).is_relative_to(root))

    def test_readonly_option_omits_rewrite(self):
        with mock.patch.object(rs, "_run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            rs.run_lint(SYSTEM_ROOT.parent, fix_claude_md=False)
        self.assertNotIn("--fix-claude-md", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
