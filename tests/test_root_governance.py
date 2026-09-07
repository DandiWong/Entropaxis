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

    def test_robustness_review_covers_dangling_fallback(self) -> None:
        """悬空兜底必须计入健壮性审阅提示。

        五维数字评分已退役（改为检查对象/证据/结论/未覆盖范围），审阅提示的
        唯一真源迁移至 软件工程.md「检查按影响选」，断言随之归位。
        """
        rule_path = SYSTEM_ROOT / "rules" / "软件工程.md"
        self.assertTrue(rule_path.exists(), "软件工程.md must exist")
        content = rule_path.read_text(encoding="utf-8")

        self.assertIn("含新增 `.data/` 声明外置字段的悬空兜底覆盖", content)
        self.assertFalse((SYSTEM_ROOT / "rules" / "五维评估.md").exists(), "五维评估.md 应已退役")


if __name__ == "__main__":
    import unittest

    unittest.main()
