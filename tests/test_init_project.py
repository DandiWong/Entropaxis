import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools import paths
from tools.backfill_frontmatter import classify
from tools.init_app import AppInitError, init_app
from tools.init_project import ProjectInitError, init_project
from tools.lint_workspace import (
    check_data_declaration_links,
    check_rules_zero_system_binding,
    check_system_entry_sync,
    check_system_layout,
    check_system_markdown_links,
    check_system_skills,
    check_system_templates,
    check_system_tools_compile,
)


TEMPLATES = SYSTEM_ROOT / "templates" / "project"


class InitProjectTests(TestCase):
    def test_undated_spec_filename_preserves_task_id(self) -> None:
        self.assertEqual(
            classify(Path("docs/20260902_权限分层/Spec_Tech-19_权限分层方案.md")),
            ("Spec", "Tech-19"),
        )
        self.assertEqual(
            classify(Path("docs/20260902_权限分层/Spec_Req-3h_权限分层方案.md")),
            ("Spec", "Req-3h"),
        )
        self.assertEqual(classify(Path("docs/Tasks.md")), ("Tasks", "tasks"))
    def test_legacy_dated_and_kebab_spec_filenames_still_parse(self) -> None:
        # 旧带日期前缀与旧 kebab 命名的历史文件仍可解析
        self.assertEqual(
            classify(Path("docs/specs/20260821_Bug-28_StageLogLost.md")),
            ("Spec", "Bug-28"),
        )
        self.assertEqual(
            classify(Path("docs/specs/req-2a-token-metrics.md")),
            ("Spec", "req"),
        )

    def test_init_creates_only_top_level_domains(self) -> None:
        with TemporaryDirectory() as temporary:
            warnings: list[str] = []
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
                warnings=warnings,
            )

            # 1. 四个顶层域存在，且域下只有契约文件、无预建二级子目录
            for domain in ("01_项目管理", "02_产品设计", "03_工程研发", "04_运营增长"):
                self.assertTrue((target / domain).is_dir(), domain)
                self.assertTrue(
                    all(entry.is_file() for entry in (target / domain).iterdir()), domain
                )
            self.assertFalse((target / "01_项目管理" / "01_资料").exists())
            self.assertFalse((target / "Archive").exists())
            self.assertFalse((target / "_知识库" / "项目资料").exists())
            # 2. 契约文件与暂存投递箱仍预建
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "CLAUDE.md").is_file())
            self.assertTrue((target / "README.md").is_file())
            self.assertTrue((target / "01_项目管理" / "DECISIONS.md").is_file())
            self.assertFalse((target / "DECISIONS.md").exists())
            self.assertTrue((target / "_知识库" / "index.md").is_file())
            self.assertTrue((target / "RawInput").is_dir())
            self.assertTrue((target / "RawInput" / ".gitkeep").is_file())
            self.assertFalse((target / "_契约").exists())
            self.assertFalse((target / "01_项目管理" / "Changelog.md").exists())
            self.assertFalse((target / "04_运营增长" / "01_上线发布" / "ReleaseNote.md").exists())
            self.assertFalse((target / "docs").exists())
            self.assertIn(
                "../01公司资料",
                (target / "_知识库" / "index.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "**看板项目 ID**：`enablement`",
                (target / "README.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "产品迭代与培训",
                (target / "01_项目管理" / "DECISIONS.md").read_text(encoding="utf-8"),
            )
            agent_rules = (target / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("本文件只记录项目特殊规则", agent_rules)
            self.assertIn("工作区根 `AGENTS.md`", agent_rules)
            self.assertEqual(
                (target / "CLAUDE.md").read_text(encoding="utf-8"),
                "# 示例项目 · Claude Code 入口\n\n@AGENTS.md\n",
            )

    @staticmethod
    def _write_registry(workspace: Path) -> Path:
        registry = workspace / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.yaml"
        registry.parent.mkdir(parents=True)
        registry.write_text("projects: []\n", encoding="utf-8")
        return registry

    def test_register_raise_removes_target(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            registry = self._write_registry(workspace)
            before = registry.read_bytes()
            with mock.patch("tools.init_project.register_project", side_effect=OSError("登记写入失败")):
                with self.assertRaises(ProjectInitError) as ctx:
                    init_project("试点项目", workspace=workspace, templates=TEMPLATES)
            self.assertIn("已删除新建目录", str(ctx.exception))
            self.assertFalse((workspace / "试点项目").exists())
            self.assertEqual(registry.read_bytes(), before)

    def test_register_false_removes_target(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            registry = self._write_registry(workspace)
            before = registry.read_bytes()
            with mock.patch("tools.init_project.project_registry.append_project", return_value=False):
                with self.assertRaises(ProjectInitError) as ctx:
                    init_project("试点项目", workspace=workspace, templates=TEMPLATES)
            self.assertIn("append_project 未写入", str(ctx.exception.__cause__))
            self.assertFalse((workspace / "试点项目").exists())
            self.assertEqual(registry.read_bytes(), before)

    def test_missing_registry_keeps_dir_with_warning(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            warnings: list[str] = []
            target = init_project(
                "试点项目",
                workspace=workspace,
                templates=TEMPLATES,
                warnings=warnings,
            )
            self.assertTrue(target.is_dir())
            self.assertTrue((target / "01_项目管理").is_dir())
            self.assertTrue(any("bootstrap.py" in warning for warning in warnings), warnings)

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
            self.assertTrue((target / "src").is_dir())
            self.assertTrue((target / "test").is_dir())
            self.assertTrue((target / "docs" / "README.md").is_file())
            self.assertTrue((target / "docs" / "Tasks.md").is_file())
            self.assertTrue((target / "docs" / "Changelog.md").is_file())
            self.assertTrue((target / "docs" / "Benchmark.md").is_file())
            self.assertTrue((target / "docs" / "ReleaseNote.md").is_file())
            self.assertFalse((target / "docs" / "specs").exists())
            self.assertFalse((target / "docs" / "audit-reports").exists())

            product_doc = (target / "PRODUCT.md").read_text(encoding="utf-8")
            self.assertIn("demo-app", product_doc)
            self.assertIn("智能方案审阅引擎", product_doc)
            tasks_doc = (target / "docs" / "Tasks.md").read_text(encoding="utf-8")
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

    def test_missing_data_declaration_is_advisory_not_blocking(self) -> None:
        """未落地的 data/ 实例声明只进建议项——否则新克隆的工作区开箱即体检失败。"""
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            rules = workspace / paths.SYSTEM_DIRNAME / "rules"
            rules.mkdir(parents=True)
            (rules / "示例规则.md").write_text(
                "参数见 [`.entropaxis/data/rules/示例配置.md`](../data/rules/示例配置.md)。\n",
                encoding="utf-8",
            )
            self.assertEqual(check_system_markdown_links(workspace), [])
            advisories = check_data_declaration_links(workspace)
            self.assertEqual(len(advisories), 1, advisories)
            self.assertIn("实例声明待落地", advisories[0])

            (workspace / paths.SYSTEM_DIRNAME / "data" / "rules").mkdir(parents=True)
            (workspace / paths.SYSTEM_DIRNAME / "data" / "rules" / "示例配置.md").write_text("x\n", encoding="utf-8")
            self.assertEqual(check_data_declaration_links(workspace), [])

    def test_zero_system_binding_flags_hardcoded_local_endpoint(self) -> None:
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            tools_dir = workspace / paths.SYSTEM_DIRNAME / "tools"
            tools_dir.mkdir(parents=True)
            # 拆开拼接端点字面量，避免测试源码自身命中端点禁词
            (tools_dir / "demo.py").write_text('URL = "http://127.0' ".0.1:9000" '\n', encoding="utf-8")
            issues = check_rules_zero_system_binding(workspace)
            self.assertTrue(any("硬编码本地端点" in issue for issue in issues), issues)

    def test_zero_system_binding_passes_on_real_workspace(self) -> None:
        # 断言的是"控制面无实体绑定"，而非返回空表：词表未落地时该检查会带一条降级留痕，
        # 那是新工作区的正常初始态（见 tests/test_lint_binding.py 软降级用例）。
        issues = check_rules_zero_system_binding(SYSTEM_ROOT.parent)
        self.assertEqual([i for i in issues if "实体词表未声明" not in i], [])

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
