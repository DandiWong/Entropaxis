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

    def test_robustness_scoring_covers_dangling_fallback(self) -> None:
        """悬空兜底必须计入健壮性评分。

        该断言原先钉在 01_根系统治理.md 的五维表副本上，反过来阻止了副本被消除；
        五维评分细则的唯一真源是 五维评估.md，断言随之归位。
        """
        rule_path = SYSTEM_ROOT / "rules" / "五维评估.md"
        self.assertTrue(rule_path.exists(), "五维评估.md must exist")
        content = rule_path.read_text(encoding="utf-8")

        self.assertIn("含新增 `.data/` 声明外置字段的悬空兜底覆盖", content)


if __name__ == "__main__":
    import unittest

    unittest.main()
