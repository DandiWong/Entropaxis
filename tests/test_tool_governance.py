import sys
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent


class ToolGovernanceRuleTests(TestCase):
    def test_tool_governance_rule_contains_core_framework(self) -> None:
        """形态判定已并入《知识沉淀》七形态表；工具技能.md 因整节复述而退役。"""
        self.assertFalse(
            (SYSTEM_ROOT / "rules" / "工具技能.md").exists(),
            "工具技能.md 已退役，其形态判定并入知识沉淀.md，红线各归真源",
        )
        taxonomy = (SYSTEM_ROOT / "rules" / "知识沉淀.md").read_text(encoding="utf-8")
        self.assertIn("沉淀形态判定模型", taxonomy)
        for form in ("落为规则 (Rule)", "生成 Skill", "转为 Tool", "声明 Schema", "局部 Script"):
            self.assertIn(form, taxonomy)

        tool_content = (SYSTEM_ROOT / "rules" / "工具设计.md").read_text(encoding="utf-8")
        skill_content = (SYSTEM_ROOT / "rules" / "技能设计.md").read_text(encoding="utf-8")
        self.assertIn("五维准入漏斗", tool_content)
        self.assertIn("自由度阶梯匹配", skill_content)
        self.assertIn("渐进式披露架构", skill_content)
        self.assertIn("单跳引用与 500 行预算铁律", skill_content)
        self.assertIn("行动导向的错误契约", tool_content)
        # 红线归各自真源，不再由单一文件复述
        governance = (SYSTEM_ROOT / "rules" / "01_根系统治理.md").read_text(encoding="utf-8")
        layout = (SYSTEM_ROOT / "rules" / "控制面布局.md").read_text(encoding="utf-8")
        self.assertIn("零系统绑定铁律", governance)
        self.assertIn("credentials", layout)

    def test_tool_crafter_skill_metadata_and_structure(self) -> None:
        skill_dir = SYSTEM_ROOT / "skills" / "tool-crafter"
        self.assertTrue(skill_dir.is_dir(), "tool-crafter skill dir must exist")

        skill_md = skill_dir / "SKILL.md"
        self.assertTrue(skill_md.exists(), "tool-crafter SKILL.md must exist")
        content = skill_md.read_text(encoding="utf-8")
        self.assertIn("name: tool-crafter", content)
        self.assertIn("工具设计.md", content)

        template_path = skill_dir / "templates" / "tool.template.py"
        self.assertTrue(template_path.exists(), "tool.template.py must exist")
        template_code = template_path.read_text(encoding="utf-8")
        self.assertIn("class ToolError", template_code)
        self.assertIn("argparse", template_code)

        # 校验模板 Python 语法（替换占位符后编译）
        test_code = template_code.replace("{{TOOL_DESCRIPTION}}", "Test tool").replace("{{TOOL_NAME}}", "demo")
        compile(test_code, str(template_path), "exec")

    def test_agents_routing_and_instruction_parsing_wires_tool_governance(self) -> None:
        agents_path = WORKSPACE_ROOT / "AGENTS.md"
        self.assertTrue(agents_path.exists(), "AGENTS.md must exist")
        agents_content = agents_path.read_text(encoding="utf-8")
        self.assertIn(".system/rules/工具设计.md", agents_content)

        parse_path = SYSTEM_ROOT / "rules" / "治理指令.md"
        self.assertTrue(parse_path.exists(), "治理指令.md must exist")
        parse_content = parse_path.read_text(encoding="utf-8")
        self.assertIn("机制转工具 / 制作工具", parse_content)
        self.assertIn("tool-crafter", parse_content)

    def test_tips_contain_tool_crafter_hints(self) -> None:
        tips_path = WORKSPACE_ROOT / ".data" / "rules" / "tips.md"
        if not tips_path.exists():
            # tips.md 是工作区实例数据（.data/rules/ 桶无模板），新克隆的工作区首次使用前
            # 并不存在。断言它必须存在会把 .system 的测试套件绑死在本机实例数据上。
            self.skipTest("tips.md 尚未落地，属新工作区正常初始态")
        content = tips_path.read_text(encoding="utf-8")
        self.assertIn("- TIP：你可以说“机制转工具”或“tool-crafter”", content)
        self.assertIn("《工具设计》", content)


if __name__ == "__main__":
    import unittest
    unittest.main()
