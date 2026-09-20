"""report_selfcheck 的装配逻辑、三态结论与漂移守卫。

注意：本文件**不调用** assemble() 的真实执行路径——它内部会 `python -m unittest discover`，
在测试套件里跑等于递归自举。所有装配用例一律注入桩函数，只验证装配与渲染逻辑本身。
"""
import re
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


def _assemble(tests=None, lint=None, dist=None):
    with mock.patch.object(rs, "run_unittest", return_value=tests or dict(GREEN_TESTS)), \
         mock.patch.object(rs, "run_lint", return_value=lint or dict(GREEN_LINT)), \
         mock.patch.object(rs, "run_distribution", return_value=dist or dict(GREEN_DIST)):
        return rs.assemble(SYSTEM_ROOT.parent)


class AxiomCoverageDriftTests(unittest.TestCase):
    def test_coverage_table_matches_meta_rule_axioms(self):
        """AXIOM_COVERAGE 的条目必须与 00_元规则.md 的九条公理逐条对齐（防真源漂移）。"""
        text = (SYSTEM_ROOT / "rules" / "00_元规则.md").read_text(encoding="utf-8")
        axioms = re.findall(r"^(\d+)\.\s+\*\*([^(*]+?)\s*\(", text, re.MULTILINE)
        self.assertEqual(len(axioms), 9, "元规则应恰好九条公理")
        expected = {f"{num}. {name.strip()}" for num, name in axioms}
        self.assertEqual(set(rs.AXIOM_COVERAGE), expected,
                         "AXIOM_COVERAGE 与 00_元规则.md 已漂移，须同步")

    def test_uncovered_axioms_are_declared_not_silently_passed(self):
        report = _assemble()
        self.assertTrue(report["uncovered_axioms"], "无机械承接的公理必须被点名")
        row = next(r for r in report["rows"] if "元规则" in r["对象"])
        self.assertEqual(row["结论"], "部分未验证")


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

    def test_advisories_passed_through_verbatim(self):
        """可优化项只透传不裁剪——是否整改由人裁决。"""
        report = _assemble()
        self.assertEqual(len(report["advisories"]), 1)
        self.assertIn("[规则超预算] 某文件超线", report["advisories"][0])


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
        clean = {**GREEN_LINT, "advisory_count": 0, "checks": []}
        md = rs.render_markdown(_assemble(lint=clean, dist={"status": "pass", "blocking": [], "advisory": []}))
        self.assertIn("本轮无待优化项", md)


if __name__ == "__main__":
    unittest.main()
