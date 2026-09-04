import sys
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent


class InstructionParsingRuleTests(TestCase):
    def test_instruction_parsing_routes_to_command_rules(self) -> None:
        """核心只留解析方法，机制语义按业务/治理两个切面外置。"""
        rule_path = SYSTEM_ROOT / "rules" / "指令解析.md"
        self.assertTrue(rule_path.exists(), "指令解析.md must exist")
        content = rule_path.read_text(encoding="utf-8")

        self.assertIn("工作流指令.md", content)
        self.assertIn("治理指令.md", content)
        self.assertIn("## 通用任务解析", content)

    def test_governance_commands_define_system_actions(self) -> None:
        rule_path = SYSTEM_ROOT / "rules" / "治理指令.md"
        self.assertTrue(rule_path.exists(), "治理指令.md must exist")
        content = rule_path.read_text(encoding="utf-8")

        self.assertIn("## 系统自检", content)
        self.assertIn("## 审计", content)
        self.assertIn("## 修正", content)
        self.assertIn("## 新迭代 / 新 Feature", content)
        self.assertIn("--fix-claude-md", content)
        self.assertIn("产物（必显五维评估表）", content)
        self.assertIn("Reviewer (审计/红队)", content)
        self.assertIn("05_审计报告.md", content)
        self.assertIn("审计意见响应与修订记录", content)
        self.assertIn("git status --short --branch", content)
        self.assertIn("严禁改动 `Tasks.md`", content)

    def test_software_engineering_references_iteration_and_spec_audit(self) -> None:
        rule_path = SYSTEM_ROOT / "rules" / "软件工程.md"
        self.assertTrue(rule_path.exists(), "软件工程.md must exist")
        content = rule_path.read_text(encoding="utf-8")

        self.assertIn("新迭代 / 新 Feature", content)
        self.assertIn("方案设计与审修阶段", content)
        self.assertIn("05_审计报告.md", content)
        self.assertIn("状态隔离铁律", content)
        self.assertIn("严禁改动 `docs/Tasks.md`", content)

    def test_collaboration_references_audit_amend_loop(self) -> None:
        rule_path = SYSTEM_ROOT / "rules" / "角色协作.md"
        self.assertTrue(rule_path.exists(), "角色协作.md must exist")
        content = rule_path.read_text(encoding="utf-8")

        self.assertIn("方案审修协同闭环", content)
        self.assertIn("05_审计报告.md", content)
        self.assertIn("审计意见响应与修订记录", content)
        self.assertIn("实施准入触发任务流", content)
        self.assertIn("终审判据与人工锚点", content)
        self.assertIn("人工批量签字", content)
        self.assertIn("轮次上限", content)
        self.assertIn("僵局", content)
        self.assertIn("跨 CLI 审修协调", content)
        self.assertIn("优雅降级", content)

    def test_workspace_config_declares_external_reviewer_cli(self) -> None:
        config_path = WORKSPACE_ROOT / ".data" / "workspace-config.md"
        self.assertTrue(config_path.exists(), "workspace-config.md must exist")
        content = config_path.read_text(encoding="utf-8")

        self.assertIn("审计角色外置 CLI 声明", content)
        self.assertIn("omp --model openai-codex/gpt-5.6-terra", content)

    def test_tips_contain_new_command_hints(self) -> None:
        tips_path = WORKSPACE_ROOT / ".data" / "tips.md"
        self.assertTrue(tips_path.exists(), "tips.md must exist")
        content = tips_path.read_text(encoding="utf-8")

        self.assertIn("- TIP：你可以说“审计”", content)
        self.assertIn("- TIP：你可以说“修正”", content)
        self.assertIn("- TIP：你可以说“新迭代”或“新 feature”", content)
        self.assertIn("方案设计、审计与修正阶段严禁改动 Tasks.md", content)


if __name__ == "__main__":
    import unittest
    unittest.main()
