from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools.backfill_frontmatter import classify
from tools.init_app import AppInitError, init_app
from tools.init_project import ProjectInitError, init_project
from tools.lint_workspace import (
    check_system_entry_sync,
    check_system_layout,
    check_system_markdown_links,
    check_system_skills,
    check_system_templates,
    check_system_tools_compile,
)


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
            classify(Path("docs/04_architecture/specs/req-2a-token-metrics.md")),
            ("Spec", "req"),
        )

    def test_creates_standard_5domain_project(self) -> None:
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

            # 1. 契约与入口
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "_项目总览.md").is_file())
            self.assertTrue((target / "_契约" / "当前状态.md").is_file())
            self.assertTrue((target / "_知识库" / "index.md").is_file())
            self.assertTrue((target / "_知识库" / "项目资料").is_dir())
            self.assertTrue((target / "RawInput").is_dir())
            self.assertTrue((target / "RawInput" / ".gitkeep").is_file())

            # 2. 标准 5 域子目录
            self.assertTrue((target / "00_材料" / "基础数据底册").is_dir())
            self.assertTrue((target / "00_材料" / "合作与协议").is_dir())
            self.assertTrue((target / "00_材料" / "业务凭据").is_dir())
            self.assertTrue((target / "01_项目" / "01_立项与规划").is_dir())
            self.assertTrue((target / "01_项目" / "02_会议与决策").is_dir())
            self.assertTrue((target / "01_项目" / "03_调研与分析").is_dir())
            self.assertTrue((target / "01_项目" / "04_方案与设计").is_dir())
            self.assertTrue((target / "02_开发").is_dir())
            self.assertTrue((target / "03_交付" / "汇报与展示").is_dir())
            self.assertTrue((target / "03_交付" / "评审与验收").is_dir())
            self.assertTrue((target / "03_交付" / "正式发布包").is_dir())
            self.assertTrue((target / "Archive").is_dir())

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
            self.assertEqual(
                (target / "CLAUDE.md").read_text(encoding="utf-8"),
                "# 示例项目 · Claude Code 入口\n\n@AGENTS.md\n",
            )

    def test_creates_software_app_repo(self) -> None:
        with TemporaryDirectory() as temporary:
            target = init_app(
                "demo-app",
                target_dir=Path(temporary),
                templates=TEMPLATES,
                purpose="智能方案审阅引擎",
                users="临床医生与审阅专员",
                start="20260831",
            )

            self.assertTrue((target / "PRODUCT.md").is_file())
            self.assertTrue((target / "tasks.md").is_file())
            self.assertTrue((target / "src").is_dir())
            self.assertTrue((target / "test").is_dir())
            self.assertTrue((target / "docs" / "README.md").is_file())

            for directory in (
                "00_project",
                "01_research",
                "02_product",
                "03_design/mockups",
                "04_architecture/specs",
                "05_reports",
                "06_archive",
            ):
                self.assertTrue((target / "docs" / directory).is_dir())

            product_doc = (target / "PRODUCT.md").read_text(encoding="utf-8")
            self.assertIn("demo-app", product_doc)
            self.assertIn("智能方案审阅引擎", product_doc)

            tasks_doc = (target / "tasks.md").read_text(encoding="utf-8")
            self.assertIn("demo-app", tasks_doc)

    def test_system_control_plane_health_checks_pass(self) -> None:
        workspace = SYSTEM_ROOT.parent
        for check in (
            check_system_layout,
            check_system_entry_sync,
            check_system_markdown_links,
            check_system_templates,
            check_system_skills,
            check_system_tools_compile,
        ):
            self.assertEqual(check(workspace), [], check.__name__)

    def test_app_initializer_refuses_to_overwrite_nonempty_target(self) -> None:
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "demo-app"
            target.mkdir()
            (target / "keep.txt").write_text("user work", encoding="utf-8")
            with self.assertRaises(AppInitError):
                init_app("demo-app", target_dir=target, templates=TEMPLATES)

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
