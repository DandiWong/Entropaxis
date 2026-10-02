import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.init_app import ROOT_PATH_UNKNOWN, AppInitError, detect_test_commands, init_app, record_in_parent

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


class InitAppEntryAndDetectionTests(TestCase):
    def test_entry_only_fills_missing_entries_and_keeps_existing(self) -> None:
        with TemporaryDirectory() as temporary:
            repo = Path(temporary) / "LegacyRepo"
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "test_core.py").write_text("", encoding="utf-8")
            (repo / "CLAUDE.md").write_text("原有内容\n", encoding="utf-8")
            notes: list[str] = []
            init_app("LegacyRepo", target_dir=Path(temporary), templates=TEMPLATES, entry_only=True, notes=notes)

            self.assertEqual((repo / "CLAUDE.md").read_text(encoding="utf-8"), "原有内容\n")
            agents = (repo / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("`python3 -m pytest <相关测试文件>`（探测候选，待确认）", agents)
            self.assertIn("`python3 -m pytest`（探测候选，待确认）", agents)
            self.assertNotIn("{{", agents)
            self.assertFalse((repo / "docs").exists())  # 只补入口，不铺骨架
            self.assertIn("CLAUDE.md 已存在，跳过不动", notes)
            self.assertTrue(any(n.startswith("AGENTS.md 第 ") for n in notes))

    def test_entry_only_requires_existing_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaises(AppInitError):
                init_app("Missing", target_dir=Path(temporary), templates=TEMPLATES, entry_only=True)

    def test_detection_leaves_unknown_layers_unfilled(self) -> None:
        with TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "package.json").write_text('{"scripts": {"test": "node --test"}}', encoding="utf-8")
            (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            found = detect_test_commands(repo)
            self.assertEqual(found["全量测试命令"], "`npm test`（探测候选，待确认）")
            self.assertIn("Dockerfile", found["集成测试命令"])
            self.assertEqual(found["单元测试命令"], "待补充")
            self.assertEqual(found["效果验证命令"], "待补充")

    def test_root_path_outside_workspace_is_visible_marker(self) -> None:
        with TemporaryDirectory() as temporary:
            app = init_app("Outside", target_dir=Path(temporary), templates=TEMPLATES, git_init=False)
            self.assertIn(ROOT_PATH_UNKNOWN, (app / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertNotIn("../../../../AGENTS.md", (app / "AGENTS.md").read_text(encoding="utf-8"))

    def test_new_app_gets_gitignore_and_git_repo_without_commit(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git 未安装")
        with TemporaryDirectory() as temporary:
            notes: list[str] = []
            app = init_app("Fresh", target_dir=Path(temporary), templates=TEMPLATES, notes=notes)
            self.assertIn(".env", (app / ".gitignore").read_text(encoding="utf-8"))
            self.assertTrue((app / ".git").is_dir())
            log = subprocess.run(["git", "-C", str(app), "log"], capture_output=True, text=True)
            self.assertNotEqual(log.returncode, 0)  # 无任何提交

    def test_record_in_parent_appends_timeline_once(self) -> None:
        with TemporaryDirectory() as temporary:
            parent = Path(temporary) / "Proj"
            app = parent / "03_工程研发" / "Svc"
            app.mkdir(parents=True)
            (parent / "README.md").write_text(
                "# Proj\n\n## 2. 关键里程碑与时间线\n\n- `20260101`：项目初始化\n\n---\n\n## 3. 使用规范提示\n",
                encoding="utf-8",
            )
            record_in_parent(parent, app, "20261002", "新建")
            note = record_in_parent(parent, app, "20261002", "新建")
            text = (parent / "README.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("新建代码仓库 `03_工程研发/Svc/`"), 1)
            self.assertLess(text.index("新建代码仓库"), text.index("---"))
            self.assertIn("跳过", note)

    def test_record_in_parent_skips_without_timeline(self) -> None:
        with TemporaryDirectory() as temporary:
            parent = Path(temporary)
            (parent / "README.md").write_text("# 无时间线\n", encoding="utf-8")
            note = record_in_parent(parent, parent / "App", "20261002", "新建")
            self.assertIn("未登记", note)
            self.assertEqual((parent / "README.md").read_text(encoding="utf-8"), "# 无时间线\n")
