"""resolve_project 的项目解析、别名匹配与代码真源提取契约。"""
import io
import json
import subprocess
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

    def test_cli_invocation_default_text(self):
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = RP.main(["PaperPro", "--workspace-root", str(self.root)])
            self.assertEqual(ret, 0)
            out = buf.getvalue()
            self.assertIn("[medalkaid-paper]", out)
            self.assertIn("code: 04MedAIkaid/05文章发表/03_工程研发/PaperPro", out)
        finally:
            sys.stdout = old_stdout

    def test_cli_invocation_list_text(self):
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = RP.main(["--list", "--workspace-root", str(self.root)])
            self.assertEqual(ret, 0)
            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("[medalkaid]"))
        finally:
            sys.stdout = old_stdout

    def test_cli_json_returns_machine_contract(self):
        """M-1：真实消费方（会议 Skill workspace_locator）读 `--json` 的 abs_dir/id 契约。"""
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = RP.main(["PaperPro", "--json", "--workspace-root", str(self.root)])
            self.assertEqual(ret, 0)
            data = json.loads(buf.getvalue())
        finally:
            sys.stdout = old_stdout
        self.assertEqual(data["id"], "medalkaid-paper")
        self.assertEqual(data["dir"], "04MedAIkaid/05文章发表")
        self.assertEqual(data["abs_dir"], str((self.root / "04MedAIkaid/05文章发表").resolve()))

    def test_cli_list_json_outputs_array(self):
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = RP.main(["--list", "--json", "--workspace-root", str(self.root)])
            self.assertEqual(ret, 0)
            data = json.loads(buf.getvalue())
        finally:
            sys.stdout = old_stdout
        self.assertEqual(len(data), 3)
        self.assertEqual(data[1]["id"], "medalkaid-paper")

    def test_real_cli_json_exit_code_matches_consumer_contract(self):
        """消费方以子进程调 resolver 并按退出码分流；真实 CLI 须退出 0 且 stdout 是合法 JSON。"""
        done = subprocess.run(
            [sys.executable, str(SYSTEM_ROOT / "tools" / "resolve_project.py"),
             "PaperPro", "--json", "--workspace-root", str(self.root)],
            capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(done.stdout)
        self.assertTrue(data.get("abs_dir"))


if __name__ == "__main__":
    unittest.main()
