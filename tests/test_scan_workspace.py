"""scan_workspace 单元测试：验证自动扫描工作区并注册项目、工程与知识库的正确性。"""
import tempfile
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import project_registry
import scan_workspace


class ScanWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 初始化 .entropaxis 结构
        data_dir = self.root / ".entropaxis" / "data" / "templates"
        data_dir.mkdir(parents=True)
        reg_file = data_dir / "registry.yaml"
        reg_file.write_text("exclude: [repo/]\nprojects:\n", encoding="utf-8")
        ws_file = data_dir / "workspace-config.yaml"
        ws_file.write_text("shared_dirs: []\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_and_classify(self):
        # 创建一个代码工程
        proj1 = self.root / "my-app"
        proj1.mkdir()
        (proj1 / "package.json").write_text("{}", encoding="utf-8")
        (proj1 / ".git").mkdir()

        # 创建一个复合项目（含子模块）
        proj2 = self.root / "complex-sys"
        proj2.mkdir()
        (proj2 / "apps").mkdir()
        subapp = proj2 / "apps" / "api"
        subapp.mkdir(parents=True)
        (subapp / "pyproject.toml").write_text("", encoding="utf-8")

        # 创建一个知识库目录
        kb_dir = self.root / "domain-assets"
        kb_dir.mkdir()
        for i in range(3):
            (kb_dir / f"doc_{i}.pdf").write_text("pdf", encoding="utf-8")

        # 创建一个排除目录
        ex_dir = self.root / "Tools Install"
        ex_dir.mkdir()
        (ex_dir / "setup.exe").write_text("exe", encoding="utf-8")

        scanned = scan_workspace.scan_workspace(self.root)

        # 验证项目分类
        proj_names = {p["name"] for p in scanned["projects"]}
        self.assertIn("my-app", proj_names)
        self.assertIn("complex-sys", proj_names)

        # 验证工程提取
        my_app_entry = next(p for p in scanned["projects"] if p["name"] == "my-app")
        self.assertIn("my-app", my_app_entry["code"])

        complex_entry = next(p for p in scanned["projects"] if p["name"] == "complex-sys")
        self.assertTrue(any("complex-sys/apps/api" in c for c in complex_entry["code"]))

        # 验证知识库识别
        kb_names = {k["name"] for k in scanned["shared_kbs"]}
        self.assertIn("domain-assets", kb_names)

        # 验证排除目录识别
        ex_names = {e["name"] for e in scanned["excludes"]}
        self.assertIn("Tools Install", ex_names)

        # 验证注册逻辑
        ok = scan_workspace.register_scanned(self.root, scanned, apply=True)
        self.assertTrue(ok)

        # 校验 registry.yaml
        projects = project_registry.load_projects(self.root)
        registered_ids = {p["id"] for p in projects}
        self.assertIn("my-app", registered_ids)
        self.assertIn("complex-sys", registered_ids)

        excludes = project_registry.load_excludes(self.root)
        self.assertTrue(any("Tools Install" in e for e in excludes))
        self.assertTrue(any("domain-assets" in e for e in excludes))


if __name__ == "__main__":
    unittest.main()
