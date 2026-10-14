"""resolve_stakeholder 的干系人矩阵、人员消歧、层级继承与断链体检契约。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import resolve_stakeholder as RS  # noqa: E402


SAMPLE_STAKEHOLDERS_YAML = """\
_meta:
  source: .entropaxis/templates/instance/stakeholders.template.yaml
  managed_by: tools/resolve_stakeholder.py + 人工维护
  policy: merge-only
  schema: .entropaxis/schemas/stakeholders.schema.json

people:
  - id: wangjianchun
    name: 王建春
    org: 复星医药/AI数智部
    default_title: 高级项目经理
    aliases: [建春, 建哥, Jason]
  - id: majunyi
    name: 马俊义
    org: 复星医药/AI数智部
    default_title: 部门负责人/总监
    aliases: [君一, Junyi Ma]
  - id: wanghao
    name: 王浩
    org: 复星医药/管理层
    default_title: 副总裁/分管领导
    aliases: [浩总]
  - id: zhouyuan
    name: 周嫄
    org: 复星医药/临床研发部
    default_title: 临床医学专家
    aliases: [周博]

projects:
  medalkaid:
    name: MedAlkaid
    decision_maker: wanghao
    business_owner: zhouyuan
    project_manager: wangjianchun
    product_manager: wangjianchun
    rd_engineers:
      - wangjianchun
  medalkaid-paper:
    name: 文章发表 (PaperPro)
    business_owner: zhouyuan
    project_manager: wangjianchun
    product_manager: zhouyuan
"""

SAMPLE_REGISTRY_YAML = """\
projects:
  - {id: medalkaid, name: MedAlkaid, path: 04MedAIkaid/}
  - {id: medalkaid-paper, name: 文章发表, path: 04MedAIkaid/05文章发表/, parent: medalkaid}
"""


class ResolveStakeholderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        tmpl_dir = self.root / ".entropaxis" / "data" / "templates"
        tmpl_dir.mkdir(parents=True)
        (tmpl_dir / "stakeholders.yaml").write_text(SAMPLE_STAKEHOLDERS_YAML, encoding="utf-8")
        (tmpl_dir / "registry.yaml").write_text(SAMPLE_REGISTRY_YAML, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_resolve_person_by_id(self):
        p = RS.resolve_person("wangjianchun", self.root)
        self.assertIsNotNone(p)
        self.assertEqual(p["name"], "王建春")

    def test_resolve_person_by_exact_name(self):
        p = RS.resolve_person("周嫄", self.root)
        self.assertIsNotNone(p)
        self.assertEqual(p["id"], "zhouyuan")

    def test_resolve_person_by_alias(self):
        p1 = RS.resolve_person("建哥", self.root)
        self.assertIsNotNone(p1)
        self.assertEqual(p1["name"], "王建春")

        p2 = RS.resolve_person("junyi ma", self.root)
        self.assertIsNotNone(p2)
        self.assertEqual(p2["name"], "马俊义")

    def test_resolve_person_by_contained_alias(self):
        p = RS.resolve_person("周博发言记录", self.root)
        self.assertIsNotNone(p)
        self.assertEqual(p["id"], "zhouyuan")

    def test_resolve_project_matrix_with_inheritance(self):
        # medalkaid-paper 未直接声明 decision_maker，但应沿 parent 继承 medalkaid 的 wanghao
        matrix = RS.resolve_project_matrix("medalkaid-paper", self.root, inherit=True)
        self.assertIsNotNone(matrix)
        self.assertEqual(matrix["decision_maker"], "wanghao")
        self.assertIn("王浩", matrix["decision_maker_display"])
        self.assertEqual(matrix["product_manager"], "zhouyuan")
        self.assertEqual(matrix["_inherited_from"].get("decision_maker"), "medalkaid")

    def test_find_person_roles(self):
        roles = RS.find_person_roles("wangjianchun", self.root)
        project_ids = [r["project_id"] for r in roles]
        self.assertIn("medalkaid", project_ids)
        self.assertIn("medalkaid-paper", project_ids)

    def test_lint_stakeholders_valid(self):
        issues = RS.lint_stakeholders(self.root)
        self.assertEqual(issues, [])

    def test_lint_stakeholders_catches_broken_link(self):
        broken_yaml = SAMPLE_STAKEHOLDERS_YAML.replace("decision_maker: wanghao", "decision_maker: non_existent_user")
        (self.root / ".entropaxis" / "data" / "templates" / "stakeholders.yaml").write_text(broken_yaml, encoding="utf-8")
        issues = RS.lint_stakeholders(self.root)
        self.assertTrue(any("人员引用断链" in i for i in issues))

    def test_format_matrix_text(self):
        matrix = RS.resolve_project_matrix("medalkaid-paper", self.root, inherit=True)
        text = RS.format_matrix_text(matrix)
        self.assertIn("[medalkaid-paper]", text)
        self.assertIn("决策人: 王浩", text)
        self.assertIn("PM: 王建春", text)
        self.assertIn("PO: 周嫄", text)


if __name__ == "__main__":
    unittest.main()
