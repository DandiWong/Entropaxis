import json
import shutil
import sys
import tempfile as _tempfile  # 模块名避免与实例变量冲突
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.bootstrap import render_instance_configs


class RenderInstanceConfigsTests(TestCase):
    """测试 render_instance_configs 的三件核心契约：
    1. 目标文件已存在则跳过（幂等保护）
    2. 目标文件缺失时首次渲染，占位符替换为工作区目录名兜底
    3. 白名单过滤：仅渲染 board_config + workspace-config 两个 data/ 实例模板，
       不污染项目级脚手架模板（README/AGENTS 等）
    """

    def setUp(self) -> None:
        self.tmpdir = Path(_tempfile.mkdtemp(prefix="render-test-"))
        self.templates_dir = self.tmpdir / "templates"
        self.data_dir = self.tmpdir / ".data"
        (self.templates_dir / "instance").mkdir(parents=True, exist_ok=True)
        (self.templates_dir / "project").mkdir(parents=True, exist_ok=True)

        # 复制真实模板（避免在测试里硬编码两份）
        real_templates = SYSTEM_ROOT / "templates" / "instance"
        for name in ("board_config.template.json", "workspace-config.template.md"):
            shutil.copy2(real_templates / name, self.templates_dir / "instance" / name)

        # 项目脚手架放 project/，确保不被实例渲染流程碰到（目录即白名单，无需硬编码名单）
        (self.templates_dir / "project" / "AGENTS.template.md").write_text(
            "# AGENTS\n{{ORG_FULL_NAME}}", encoding="utf-8"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_render_creates_targets_with_workspace_name_fallback(self) -> None:
        ok = render_instance_configs(
            templates_dir=self.templates_dir,
            data_dir=self.data_dir,
            ws_name="Acme",
            verbose=False,
        )
        self.assertTrue(ok)

        # board_config.json 应被创建
        board = self.data_dir / "templates" / "board_config.json"
        self.assertTrue(board.exists())
        data = json.loads(board.read_text(encoding="utf-8"))
        self.assertIn("providers", data)
        self.assertEqual(data["providers"], {})

        # workspace-config.md 应被创建，且占位符被替换为 "Acme" 兜底
        ws_cfg = self.data_dir / "templates" / "workspace-config.md"
        self.assertTrue(ws_cfg.exists())
        content = ws_cfg.read_text(encoding="utf-8")
        self.assertIn("Acme", content)
        self.assertNotIn("{{ORG_FULL_NAME}}", content)
        self.assertNotIn("{{ORG_FORBIDDEN_ABBR}}", content)

        # AGENTS.template.md 不在白名单 → 不应被渲染
        agents = self.data_dir / "templates" / "AGENTS.md"
        self.assertFalse(
            agents.exists(),
            "AGENTS.template.md is not in whitelist, must not be rendered to data/",
        )

    def test_existing_target_is_not_overwritten(self) -> None:
        (self.data_dir / "templates").mkdir(parents=True, exist_ok=True)
        sentinel = "PRESERVED-BY-USER"
        existing = self.data_dir / "templates" / "board_config.json"
        existing.write_text(sentinel, encoding="utf-8")

        render_instance_configs(
            templates_dir=self.templates_dir,
            data_dir=self.data_dir,
            ws_name="Acme",
            verbose=False,
        )

        self.assertEqual(existing.read_text(encoding="utf-8"), sentinel)

    def test_whitelist_excludes_project_scaffolding_templates(self) -> None:
        # 在白名单外多塞项目级模板，确保两个 glob 循环都过滤它们
        (self.templates_dir / "README.template.md").write_text("# README", encoding="utf-8")
        (self.templates_dir / "registry.template.md").write_text("# registry", encoding="utf-8")

        render_instance_configs(
            templates_dir=self.templates_dir,
            data_dir=self.data_dir,
            ws_name="Acme",
            verbose=False,
        )

        # 仅白名单内的两个文件被渲染
        self.assertTrue((self.data_dir / "templates" / "board_config.json").exists())
        self.assertTrue((self.data_dir / "templates" / "workspace-config.md").exists())
        # 不在白名单的全部跳过
        self.assertFalse((self.data_dir / "templates" / "AGENTS.md").exists())
        self.assertFalse((self.data_dir / "templates" / "README.md").exists())
        self.assertFalse((self.data_dir / "registry.md").exists())

    def test_template_dir_missing_is_noop(self) -> None:
        empty_dir = self.tmpdir / "empty_templates"
        empty_dir.mkdir()
        ok = render_instance_configs(
            templates_dir=empty_dir,
            data_dir=self.data_dir,
            ws_name="Acme",
            verbose=False,
        )
        self.assertTrue(ok)
        # data/ 可能被 mkdir 但不应有产物
        self.assertFalse(list((self.data_dir / "templates").glob("*")))