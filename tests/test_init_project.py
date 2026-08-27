from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools.backfill_frontmatter import classify
from tools.init_project import ProjectInitError, init_project


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SYSTEM_ROOT / "templates"


class InitProjectTests(TestCase):
    def test_undated_spec_filename_preserves_task_id(self) -> None:
        self.assertEqual(
            classify(Path("docs/04_architecture/specs/Bug-28_StageLogLost.md")),
            ("Spec", "Bug-28"),
        )
        self.assertEqual(
            classify(Path("docs/04_architecture/specs/Req-3h_ExampleTopic.md")),
            ("Spec", "Req-3h"),
        )

    def test_legacy_dated_and_kebab_spec_filenames_still_parse(self) -> None:
        # 旧带日期前缀与旧 kebab 命名的历史文件仍可解析
        self.assertEqual(
            classify(Path("docs/04_architecture/specs/20260821_Bug-28_StageLogLost.md")),
            ("Spec", "Bug-28"),
        )
        self.assertEqual(
            classify(Path("docs/04_architecture/specs/M1-pubmed-only.md")),
            ("Spec", "M1"),
        )

    def test_creates_minimal_project_with_both_knowledge_layers(self) -> None:
        with TemporaryDirectory() as temporary:
            target = init_project(
                "示例项目",
                workspace=Path(temporary),
                templates=TEMPLATES,
                purpose_goal="产品迭代与培训",
                people="张三（负责人）；李四（需求方）",
                next_event="20260801 需求评审",
                materials="/资料/会议纪要.docx",
                project_knowledge="客户提供的项目资料",
                shared_knowledge="../01公司资料；../03个人资料/KB",
                sensitivity="内部敏感",
                dashboard_project_id="enablement",
                start="20260728",
            )

            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "_项目总览.md").is_file())
            self.assertTrue((target / "_契约" / "当前状态.md").is_file())
            self.assertTrue((target / "_知识库" / "index.md").is_file())
            self.assertTrue((target / "_知识库" / "项目资料").is_dir())
            self.assertFalse((target / "docs").exists())
            self.assertIn(
                "../01公司资料",
                (target / "_知识库" / "index.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Dashboard 项目 ID：`enablement`",
                (target / "_项目总览.md").read_text(encoding="utf-8"),
            )
            agent_rules = (target / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("本文件只记录项目特殊规则", agent_rules)
            self.assertIn("工作区根 `AGENTS.md`", agent_rules)
            self.assertNotIn("source_ref", agent_rules)
            self.assertEqual(
                (target / "CLAUDE.md").read_text(encoding="utf-8"),
                "# 示例项目 · Claude Code 入口\n\n@AGENTS.md\n",
            )

    def test_creates_optional_classified_docs_layout(self) -> None:
        with TemporaryDirectory() as temporary:
            target = init_project(
                "开发项目",
                workspace=Path(temporary),
                templates=TEMPLATES,
                start="20260819",
                docs_layout=True,
            )

            docs = target / "docs"
            self.assertTrue((docs / "README.md").is_file())
            for directory in (
                "00_project",
                "01_research",
                "02_product",
                "03_design",
                "03_design/mockups",
                "04_architecture",
                "04_architecture/specs",
                "05_reports",
                "06_archive",
            ):
                self.assertTrue((docs / directory).is_dir())

            index = (docs / "README.md").read_text(encoding="utf-8")
            self.assertIn("开发项目 · 文档导航", index)
            self.assertIn("20260819_SystemDesign.md", index)
            self.assertIn("TaskID_PascalCaseTopic.md", index)
            self.assertIn("03_design/mockups/", index)

    def test_refuses_to_overwrite_existing_project(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "已有项目").mkdir()
            with self.assertRaises(ProjectInitError):
                init_project(
                    "已有项目",
                    workspace=workspace,
                    templates=TEMPLATES,
                )

    def test_rejects_unsafe_name_and_invalid_date(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with self.assertRaises(ProjectInitError):
                init_project(
                    "../越界",
                    workspace=workspace,
                    templates=TEMPLATES,
                )
            with self.assertRaises(ProjectInitError):
                init_project(
                    "日期错误",
                    workspace=workspace,
                    templates=TEMPLATES,
                    start="2026-07-28",
                )
            with self.assertRaises(ProjectInitError):
                init_project(
                    "ID错误",
                    workspace=workspace,
                    templates=TEMPLATES,
                    dashboard_project_id="../越界",
                )
