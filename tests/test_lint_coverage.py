"""预算、去重、来源与命名检查的正常/越界/排除回归。"""
import tempfile
import unittest
from pathlib import Path

from tools import lint_workspace as lint
from tools import paths


class LintCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def rule(self, name, content):
        return self.write(f"{paths.SYSTEM_DIRNAME}/rules/{name}.md", content)

    def test_resident_budget_boundary_and_first_party_scope(self):
        self.write("AGENTS.md", "a\n" * lint.RESIDENT_MAX_LINES)
        self.write("project/AGENTS.md", "a\n" * lint.PROJECT_AGENTS_MAX_LINES)
        self.write("Archive/AGENTS.md", "a\n" * 200)
        self.assertEqual(lint.check_resident_budget(self.root), [])
        self.write("AGENTS.md", "a\n" * (lint.RESIDENT_MAX_LINES + 1))
        self.write("project/AGENTS.md", "@AGENTS.md\n" + "a\n" * lint.PROJECT_AGENTS_MAX_LINES)
        issues = lint.check_resident_budget(self.root)
        self.assertEqual(len(issues), 3)
        self.assertTrue(any("入口自引用" in i for i in issues))

    def test_current_state_budget_boundary_and_both_paths(self):
        names = ("project/DECISIONS.md", "project/_契约/当前状态.md")
        for rel in names:
            self.write(rel, "a\n" * lint.MAX_CURRENT_STATE_LINES)
        self.write("Archive/DECISIONS.md", "a\n" * 200)
        self.assertEqual(lint.check_current_state_bloat(self.root), [])
        for rel in names:
            self.write(rel, "a\n" * (lint.MAX_CURRENT_STATE_LINES + 1))
        self.assertEqual(len(lint.check_current_state_bloat(self.root)), 2)

    def test_rule_deduplication_ignores_generic_headings(self):
        self.assertEqual(lint.check_rule_deduplication(self.root), [])
        self.rule("a", "## 引言\n## 唯一甲\n")
        self.rule("b", "## 引言\n## 唯一乙\n")
        self.assertEqual(lint.check_rule_deduplication(self.root), [])
        self.rule("b", "## 唯一甲\n")
        issues = lint.check_rule_deduplication(self.root)
        self.assertEqual(len(issues), 1)
        self.assertIn("唯一甲", issues[0])

    def test_rule_budget_boundary(self):
        self.assertEqual(lint.check_rule_budget(self.root), [])
        self.rule("a", "a\n" * lint.RULE_MAX_LINES)
        self.assertEqual(lint.check_rule_budget(self.root), [])
        self.rule("a", "a\n" * (lint.RULE_MAX_LINES + 1))
        self.assertEqual(len(lint.check_rule_budget(self.root)), 1)

    def test_syntax_tax_checks_entry_and_rules(self):
        self.rule("a", "普通正文。\n")
        entry = f"{paths.SYSTEM_DIRNAME}/entrypoints/AGENTS.md"
        self.write(entry, "普通入口。\n")
        self.assertEqual(lint.check_syntax_tax_budget(self.root), [])
        table = "| 字段 | 内容 |\n|---|---|\n| a | b |\n"
        self.rule("a", table)
        self.write(entry, table)
        issues = lint.check_syntax_tax_budget(self.root)
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("常驻入口" in i for i in issues))

    def test_repetition_boundary_and_code_exclusion(self):
        fragment = "重复正文" * lint.RULE_DUP_WINDOW
        self.rule("a", f"```\n{fragment}\n```\n")
        self.rule("b", f"```\n{fragment}\n```\n")
        self.assertEqual(lint.check_rule_text_repetition(self.root), [])
        self.rule("a", "甲" * (lint.RULE_DUP_WINDOW - 1))
        self.rule("b", "甲" * (lint.RULE_DUP_WINDOW - 1))
        self.assertEqual(lint.check_rule_text_repetition(self.root), [])
        self.rule("a", fragment)
        self.rule("b", fragment)
        issues = lint.check_rule_text_repetition(self.root)
        self.assertEqual(len(issues), 1)
        self.assertIn("a.md, b.md", issues[0])

    def test_provenance_scope(self):
        self.assertEqual(lint.check_data_provenance(self.root), [])
        base = f"{paths.SYSTEM_DIRNAME}/data"
        self.write(f"{base}/credentials/private.json", "{}")
        self.write(f"{base}/docs/report.md", "报告")
        self.write(f"{base}/templates/config.json", '{"_meta": {"policy": "merge-only"}}')
        self.write(f"{base}/rules/example.md", "---\npolicy: append-only\n---\n正文")
        self.assertEqual(lint.check_data_provenance(self.root), [])
        self.write(f"{base}/templates/config.json", "{}")
        self.write(f"{base}/rules/example.md", "正文")
        self.assertEqual(len(lint.check_data_provenance(self.root)), 2)

    def test_open_bypass_flags_argv_and_fence_not_prose_or_opener(self):
        base = paths.SYSTEM_DIRNAME
        self.write(f"{base}/tools/open_file.py", 'return ["open", path]\nreturn ["xdg-open", path]\n')
        self.write(f"{base}/tools/bootstrap.py", 'selected_cmd = "open"\nselected_cmd = "xdg-open"\n')
        self.write(f"{base}/skills/demo/SKILL.md", "不要调用 `open` 或 xdg-open。\n```bash\npython3 open_file.py a.pdf\n```\n")
        self.assertEqual(lint.check_open_bypass(self.root), [])
        self.write(f"{base}/tools/evil.py", 'subprocess.run(["open", path])\n')
        self.write(f"{base}/skills/demo/SKILL.md", "```bash\nopen a.pdf\nxdg-open a.pdf\n```\n")
        issues = lint.check_open_bypass(self.root)
        self.assertEqual(len(issues), 3, issues)
        self.assertTrue(any("evil.py" in i for i in issues))
        self.assertTrue(sum("SKILL.md" in i for i in issues) == 2)
