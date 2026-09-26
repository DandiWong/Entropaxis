"""2026-09-27 lint_workspace 实现审查的回归：排除按相对路径、胶囊日期有效性。"""
import sys
import tempfile
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
import lint_workspace as lw  # noqa: E402


class FirstPartyScopeTests(unittest.TestCase):
    def test_workspace_under_excluded_named_path_still_checked(self):
        """工作区自身位于含 Archive/skills 字样的路径下时，内容不得被整体排除。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Archive" / "skills" / "ws"
            doc = root / "proj" / "AGENTS.md"
            doc.parent.mkdir(parents=True)
            doc.write_text("# x\n", encoding="utf-8")
            self.assertTrue(lw._is_first_party(doc, root))
            self.assertFalse(lw._is_first_party(root / "Archive" / "a.md", root))


class CapsuleDateTests(unittest.TestCase):
    def test_invalid_calendar_date_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj" / "20261332_某方案").mkdir(parents=True)
            (root / "proj" / "20260927_某方案").mkdir(parents=True)
            issues = [i for i in lw.check_routing_integrity(root) if "胶囊命名异常" in i]
            self.assertEqual(len(issues), 1, issues)
            self.assertIn("20261332", issues[0])


if __name__ == "__main__":
    unittest.main()
