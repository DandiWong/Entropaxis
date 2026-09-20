"""record_iteration 的分节插入、幂等与语种跟随核验。"""
import tempfile
import unittest
from pathlib import Path

from tools import record_iteration as ri

CHANGELOG = """# 变更日志（Changelog）

前言说明。

---
## [Unreleased]

### 新增

- 既有条目 ([Tech-1])

### 修复

- 老修复 ([Tech-2])

## [1.0.0] — 2026-01-01

### 新增
- 初版
"""

TASKS = """# Tasks

- [x] **Tech-1**：已完成的
- [ ] **Tech-92**：待完成的任务
- [ ] **Tech-93**：另一个
"""


class ChangelogTests(unittest.TestCase):
    def test_appends_into_existing_section_not_elsewhere(self):
        new, changed = ri.add_changelog_entry(CHANGELOG, "修复", "修正越权判断", "Tech-92")
        self.assertTrue(changed)
        fixed_block = new.split("### 修复")[1].split("##")[0]
        self.assertIn("- 修正越权判断 ([Tech-92])", fixed_block)
        # 不得串到「新增」小节，也不得污染已发布版本段
        self.assertNotIn("Tech-92", new.split("### 新增")[1].split("###")[0])
        self.assertNotIn("Tech-92", new.split("## [1.0.0]")[1])

    def test_idempotent_on_identical_entry(self):
        once, _ = ri.add_changelog_entry(CHANGELOG, "修复", "修正越权判断", "Tech-92")
        twice, changed = ri.add_changelog_entry(once, "修复", "修正越权判断", "Tech-92")
        self.assertFalse(changed)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("([Tech-92])"), 1)

    def test_creates_missing_category_section(self):
        new, changed = ri.add_changelog_entry(CHANGELOG, "安全", "补齐凭据脱敏", "Tech-93")
        self.assertTrue(changed)
        self.assertIn("### 安全", new)
        self.assertIn("- 补齐凭据脱敏 ([Tech-93])", new.split("### 安全")[1])
        self.assertNotIn("Tech-93", new.split("## [1.0.0]")[1])

    def test_english_category_follows_file_language(self):
        """文件在用中文分类时，传 Added 也写成「新增」，不强行统一他人文件风格。"""
        new, _ = ri.add_changelog_entry(CHANGELOG, "Added", "新能力", "Tech-94")
        self.assertIn("- 新能力 ([Tech-94])", new.split("### 新增")[1].split("###")[0])
        self.assertNotIn("### Added", new)

    def test_english_file_keeps_english_headings(self):
        english = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- old ([T-1])\n"
        new, _ = ri.add_changelog_entry(english, "修复", "a fix", "T-2")
        self.assertIn("- a fix ([T-2])", new)
        self.assertNotIn("### 修复", new)

    def test_creates_unreleased_when_absent(self):
        text = "# Changelog\n\n## [1.0.0] — 2026-01-01\n\n### 新增\n- 初版\n"
        new, changed = ri.add_changelog_entry(text, "修复", "首条", "T-1")
        self.assertTrue(changed)
        self.assertLess(new.index("[Unreleased]"), new.index("[1.0.0]"))
        self.assertNotIn("T-1", new.split("## [1.0.0]")[1])

    def test_unknown_category_rejected_with_guidance(self):
        with self.assertRaises(ri.IterationError) as ctx:
            ri.add_changelog_entry(CHANGELOG, "Refactored", "x", "T-1")
        self.assertIn("👉", str(ctx.exception))


class TasksTests(unittest.TestCase):
    def test_marks_only_target_task(self):
        new, changed, _ = ri.mark_task_done(TASKS, "Tech-92")
        self.assertTrue(changed)
        self.assertIn("- [x] **Tech-92**", new)
        self.assertIn("- [ ] **Tech-93**", new)

    def test_idempotent_on_already_done(self):
        new, changed, note = ri.mark_task_done(TASKS, "Tech-1")
        self.assertFalse(changed)
        self.assertEqual(new, TASKS)
        self.assertIn("已是完成态", note)

    def test_missing_task_reports_actionable_error(self):
        with self.assertRaises(ri.IterationError) as ctx:
            ri.mark_task_done(TASKS, "Tech-999")
        self.assertIn("👉", str(ctx.exception))

    def test_non_checkbox_entry_refuses_rather_than_guess(self):
        text = "# Tasks\n\nTech-50: status: active\n"
        with self.assertRaises(ri.IterationError):
            ri.mark_task_done(text, "Tech-50")


class LocateAndAtomicTests(unittest.TestCase):
    def test_locates_nested_docs_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "docs" / "00_project"
            nested.mkdir(parents=True)
            (nested / "Changelog.md").write_text(CHANGELOG, encoding="utf-8")
            self.assertEqual(ri._locate(None, root, "Changelog.md"), nested / "Changelog.md")

    def test_missing_file_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ri.IterationError) as ctx:
                ri._locate(None, Path(td), "Changelog.md")
            self.assertIn("👉", str(ctx.exception))

    def test_atomic_write_replaces_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.md"
            path.write_text("old", encoding="utf-8")
            ri.atomic_write(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
