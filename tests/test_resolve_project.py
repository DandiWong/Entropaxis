"""resolve_project 的项目解析、别名匹配与代码真源提取契约。"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import resolve_project as RP  # noqa: E402

SAMPLE_REGISTRY = """\
exclude: [Archive/]
projects:
  - {id: medalkaid, name: MedAlkaid, path: 04MedAIkaid/, aliases: [MedAlkaid, 奥科智研], note: 大项目}
  - id: medalkaid-paper
    name: 文章发表
    path: 04MedAIkaid/05文章发表/
    parent: medalkaid
    aliases: [PaperPro, 文章发表]
    code: [04MedAIkaid/05文章发表/03_工程研发/PaperPro/]
  - {id: strategy, name: 战略发展, path: 05战略发展/, aliases: [战略汇报, 年中规划]}
"""


class ResolveProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        reg_dir = self.root / ".entropaxis" / "data" / "templates"
        reg_dir.mkdir(parents=True)
        (reg_dir / "registry.yaml").write_text(SAMPLE_REGISTRY, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_load_registry_projects(self):
        projects = RP.load_registry_projects(self.root)
        self.assertEqual(len(projects), 3)
        self.assertEqual([p["id"] for p in projects], ["medalkaid", "medalkaid-paper", "strategy"])

    def test_resolve_by_exact_alias(self):
        res = RP.resolve_project("PaperPro", self.root)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "medalkaid-paper")
        self.assertEqual(res["dir"], "04MedAIkaid/05文章发表")
        self.assertEqual(res["code_source"], "04MedAIkaid/05文章发表/03_工程研发/PaperPro")

    def test_resolve_by_chinese_name_and_alias(self):
        res1 = RP.resolve_project("文章发表", self.root)
        self.assertEqual(res1["id"], "medalkaid-paper")

        res2 = RP.resolve_project("战略汇报", self.root)
        self.assertEqual(res2["id"], "strategy")

    def test_resolve_by_id(self):
        res = RP.resolve_project("medalkaid", self.root)
        self.assertEqual(res["name"], "MedAlkaid")

    def test_resolve_by_natural_language_sentence(self):
        res = RP.resolve_project("对 PaperPro 最新的方案进行审计", self.root)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "medalkaid-paper")

    def test_format_project_text(self):
        res = RP.resolve_project("PaperPro", self.root)
        text = RP.format_project_text(res)
        self.assertIn("[medalkaid-paper]", text)
        self.assertIn("04MedAIkaid/05文章发表/", text)
        self.assertIn("code: 04MedAIkaid/05文章发表/03_工程研发/PaperPro", text)

    def test_cli_invocation_json(self):
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = RP.main(["PaperPro", "--json", "--workspace-root", str(self.root)])
            self.assertEqual(ret, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["id"], "medalkaid-paper")
        finally:
            sys.stdout = old_stdout


if __name__ == "__main__":
    unittest.main()
