"""lint_workspace --json 的结构化输出契约。

下游（report_selfcheck）靠这份契约装配自检报告；键名或退出码语义变了，自检的证据就静默失真。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent
LINT = SYSTEM_ROOT / "tools" / "lint_workspace.py"


class LintJsonContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, str(LINT), "--json"],
                              capture_output=True, text=True, cwd=str(WORKSPACE_ROOT))
        cls.proc = proc
        cls.payload = json.loads(proc.stdout)

    def test_stdout_is_pure_json(self):
        """--json 下不得混入人读散文，否则下游解析即碎。"""
        self.assertIsInstance(self.payload, dict)

    def test_exposes_counts_and_verdict(self):
        for key in ("root", "total_checks", "blocking_count", "advisory_count",
                    "passed", "checks", "passed_checks", "fixed_claude_md"):
            self.assertIn(key, self.payload)
        self.assertIsInstance(self.payload["blocking_count"], int)
        self.assertIsInstance(self.payload["advisory_count"], int)
        self.assertIsInstance(self.payload["passed"], bool)

    def test_exit_code_tracks_blocking_only(self):
        """建议项不改变退出码——把建议项当阻断会让自检永远红。"""
        expected = 1 if self.payload["blocking_count"] else 0
        self.assertEqual(self.proc.returncode, expected)

    def test_failed_and_passed_checks_partition_all_checks(self):
        failed = {c["title"] for c in self.payload["checks"]}
        passed = set(self.payload["passed_checks"])
        self.assertFalse(failed & passed, "同一检查项不得既失败又通过")
        self.assertEqual(len(failed | passed), self.payload["total_checks"])

    def test_each_failed_check_carries_issues_and_severity(self):
        for chk in self.payload["checks"]:
            self.assertIn("blocking", chk)
            self.assertIsInstance(chk["blocking"], bool)
            self.assertTrue(chk["issues"], f"{chk['title']} 被列为失败却无 issues 明细")

    def test_counts_match_issue_totals(self):
        blocking = sum(len(c["issues"]) for c in self.payload["checks"] if c["blocking"])
        advisory = sum(len(c["issues"]) for c in self.payload["checks"] if not c["blocking"])
        self.assertEqual(blocking, self.payload["blocking_count"])
        self.assertEqual(advisory, self.payload["advisory_count"])


if __name__ == "__main__":
    unittest.main()
