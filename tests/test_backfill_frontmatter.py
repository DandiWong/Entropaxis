import unittest
from pathlib import Path

from tools.backfill_frontmatter import classify


class ClassifyNamingTests(unittest.TestCase):
    def test_capsule_new_naming_resolves(self) -> None:
        self.assertEqual(classify(Path("20260907_主题/04_Spec_Tech-19.md")), ("Spec", "Tech-19"))

    def test_capsule_new_naming_with_topic_resolves(self) -> None:
        self.assertEqual(classify(Path("20260907_主题/04_Spec_Tech-19_修复重试.md")), ("Spec", "Tech-19"))

    def test_independent_spec_new_naming_resolves(self) -> None:
        self.assertEqual(classify(Path("docs/Tech-19_修复重试.md")), ("Spec", "Tech-19"))

    def test_legacy_spec_prefix_still_readable(self) -> None:
        """旧命名只读兼容：Spec_<ID>_主题方案.md 不被新规则拒识别。"""
        self.assertEqual(classify(Path("docs/20260902_主题/Spec_Tech-2_权限分层方案.md")), ("Spec", "Tech-2"))

    def test_legacy_specs_dir_dated_format_still_readable(self) -> None:
        self.assertEqual(classify(Path("docs/specs/20260902_权限分层_Tech2.md")), ("Spec", "权限分层"))

    def test_audit_prefixed_file_not_classified_as_spec(self) -> None:
        self.assertIsNone(classify(Path("20260907_主题/Audit_Tech-19_主题方案审计.md")))

    def test_tasks_and_backlog_unaffected(self) -> None:
        self.assertEqual(classify(Path("docs/Tasks.md")), ("Tasks", "tasks"))
        self.assertEqual(classify(Path("docs/todo.md")), ("Backlog", "todo"))


if __name__ == "__main__":
    unittest.main()
