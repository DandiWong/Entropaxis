from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools.init_project import ProjectInitError, init_project


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SYSTEM_ROOT / "templates"


class InitProjectTests(TestCase):
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
                start="20260728",
            )

            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "_项目总览.md").is_file())
            self.assertTrue((target / "_契约" / "当前状态.md").is_file())
            self.assertTrue((target / "_知识库" / "index.md").is_file())
            self.assertTrue((target / "_知识库" / "项目资料").is_dir())
            self.assertIn(
                "../01公司资料",
                (target / "_知识库" / "index.md").read_text(encoding="utf-8"),
            )

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

