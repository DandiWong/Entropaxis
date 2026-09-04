from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent


class RootGovernanceRuleTests(TestCase):
    def test_root_governance_requires_dangling_declaration_check(self) -> None:
        rule_path = SYSTEM_ROOT / "rules" / "01_根系统治理.md"
        self.assertTrue(rule_path.exists(), "01_根系统治理.md must exist")
        content = rule_path.read_text(encoding="utf-8")

        self.assertIn("声明外置悬空核验（防隐式假设铁律）", content)
        self.assertIn("必须在同一处规则正文中显式给出兜底降级方案", content)
        self.assertIn("悬空的 `.data/` 实例声明引用", content)
        self.assertIn("含新增 `.data/` 声明外置字段的悬空兜底覆盖", content)


if __name__ == "__main__":
    import unittest

    unittest.main()
