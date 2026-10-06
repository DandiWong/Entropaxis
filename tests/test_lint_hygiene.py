import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools import paths
from tools.lint_workspace import check_workspace_hygiene


class WorkspaceHygieneTests(TestCase):
    def test_reports_three_advisory_categories(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "20260626会议").mkdir()
            (root / "20260626_会议").mkdir()
            (root / "node_modules" / "20260626x").mkdir(parents=True)
            (root / "_知识库").mkdir()
            (root / "_知识库" / "index.md").write_text("# 索引\n", encoding="utf-8")
            (root / "_知识库" / "a.xlsx").write_bytes(b"x")
            with (root / "大文件.bin").open("wb") as sparse:
                sparse.truncate(51 * 1024 * 1024)

            issues = check_workspace_hygiene(root)

            self.assertEqual(len(issues), 3, issues)
            underscore = next(i for i in issues if i.startswith("[胶囊缺下划线]"))
            self.assertIn("共 1 处", underscore)
            self.assertIn("20260626会议", underscore)
            self.assertNotIn("20260626_会议", underscore)
            self.assertNotIn("node_modules", underscore)
            scattered = next(i for i in issues if i.startswith("[知识库根散落]"))
            self.assertIn("共 1 处", scattered)
            self.assertIn("_知识库/a.xlsx", scattered)
            self.assertNotIn("index.md", scattered.split("；处置建议")[0])
            oversized = next(i for i in issues if i.startswith("[超大文件]"))
            self.assertIn("共 1 处", oversized)
            self.assertIn("大文件.bin", oversized)

    def test_shared_dir_with_kb_flag_counts_as_kb_root(self) -> None:
        """shared_dirs 标 kb: true 的目录按知识库根体检；未标注时只查 _知识库/。"""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / paths.SYSTEM_DIRNAME / "data" / "templates"
            config_dir.mkdir(parents=True)
            (config_dir / "workspace-config.yaml").write_text(
                "shared_dirs:\n  - {dir: 00_知识库/, purpose: 共享, kb: true}\n",
            )
            (root / "00_知识库").mkdir()
            (root / "00_知识库" / "index.md").write_text("# 索引\n", encoding="utf-8")
            (root / "00_知识库" / "报表.xlsx").write_bytes(b"x")

            issues = check_workspace_hygiene(root)

            self.assertEqual(len(issues), 1, issues)
            self.assertIn("[知识库根散落] 共 1 处", issues[0])
            self.assertIn("00_知识库/报表.xlsx", issues[0])

            # 去掉 kb 标记后同一散落文件不再被报出
            (config_dir / "workspace-config.yaml").write_text(
                "shared_dirs:\n  - {dir: 00_知识库/, purpose: 共享}\n",
                encoding="utf-8",
            )
            self.assertEqual(check_workspace_hygiene(root), [])

    def test_explicit_kb_roots_respect_pruning(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ["Archive/_知识库", f"{paths.SYSTEM_DIRNAME}/_知识库", "node_modules/_知识库"]
            for name in names:
                (root / name).mkdir(parents=True)
                (root / name / "a.xlsx").write_bytes(b"x")
            config = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "workspace-config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "shared_dirs:\n"
                + "".join(f"  - {{dir: {name}/, purpose: fixture, kb: true}}\n" for name in names),
                encoding="utf-8",
            )
            self.assertEqual(check_workspace_hygiene(root), [])

