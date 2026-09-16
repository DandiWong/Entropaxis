import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.init_app import AppInitError, init_app

TEMPLATES = SYSTEM_ROOT / "templates" / "project"


class InitAppTests(TestCase):
    def test_init_app_creates_standard_app_scaffold(self) -> None:
        with TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            app_path = init_app(
                "MyAwesomeService",
                target_dir=target_dir,
                templates=TEMPLATES,
                purpose="微服务治理与调度中枢",
                users="基础架构研发人员",
                start="20260916",
            )
            self.assertTrue(app_path.is_dir())
            self.assertEqual(app_path.name, "MyAwesomeService")

            # 根目录文件验证
            agents_md = app_path / "AGENTS.md"
            claude_md = app_path / "CLAUDE.md"
            product_md = app_path / "PRODUCT.md"
            self.assertTrue(agents_md.exists())
            self.assertTrue(claude_md.exists())
            self.assertTrue(product_md.exists())

            agents_text = agents_md.read_text(encoding="utf-8")
            self.assertIn("MyAwesomeService · Agent 工作规则", agents_text)
            self.assertIn("## 按需入口", agents_text)
            self.assertIn("## 项目硬约束", agents_text)
            self.assertIn("## 本项目特有增补", agents_text)

            claude_text = claude_md.read_text(encoding="utf-8")
            self.assertEqual(claude_text.strip(), "# MyAwesomeService · Claude Code 入口\n\n@AGENTS.md")

            # docs 目录工作流文件验证
            docs_dir = app_path / "docs"
            self.assertTrue((docs_dir / "README.md").exists())
            self.assertTrue((docs_dir / "Tasks.md").exists())
            self.assertTrue((docs_dir / "Changelog.md").exists())
            self.assertTrue((docs_dir / "Benchmark.md").exists())
            self.assertTrue((docs_dir / "ReleaseNote.md").exists())

            # 目录骨架验证
            self.assertTrue((app_path / "src").is_dir())
            self.assertTrue((app_path / "test").is_dir())

    def test_init_app_rejects_unsafe_or_existing_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            with self.assertRaises(AppInitError):
                init_app("../bad_name", target_dir=target_dir, templates=TEMPLATES)

            # 已存在且非空时拒绝覆盖
            existing = target_dir / "ExistingApp"
            existing.mkdir()
            (existing / "file.txt").write_text("content", encoding="utf-8")
            with self.assertRaises(AppInitError):
                init_app("ExistingApp", target_dir=target_dir, templates=TEMPLATES)
