import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.setup_agents import (
    KNOWN_AGENTS,
    STANDARD_ROLES,
    detect_installed_agents,
    parse_role_table,
    render_role_section,
    update_workspace_config,
    verify_roles,
)

SAMPLE_CONFIG_WITH_FULL_ROLES = """---
source: .entropaxis/templates/instance/workspace-config.template.md
managed_by: bootstrap.py
policy: merge-only
---

# 工作区配置

## 共享资料层
| 目录 | 用途 |
|---|---|
| `01公司资料/` | 公司政策 |

## 组织名称默认口径
| 参数 | 值 |
|---|---|
| 默认名称 | 「示例组织」 |

## 角色模态外置 CLI 与模型声明

| 角色模态 | 职责定位 | 承载 CLI | 启动命令 |
|---|---|---|---|
| Reviewer | 审计/红队/架构合规 | omp | `omp --model openai-codex/gpt-5.6-terra` |
| Researcher | 深度调研/文献综述 | subagent | 内置 Subagent 机制 (auto) |
| Builder | 核心编码/重构实施 | subagent | 内置 Subagent 机制 (auto) |
| Designer | 架构设计/方案规划 | subagent | 内置 Subagent 机制 (auto) |
| Maintainer | 守门验收/证据核验 | subagent | 内置 Subagent 机制 (auto) |
"""

SAMPLE_CONFIG_OLD_REVIEWER_ONLY = """---
source: .entropaxis/templates/instance/workspace-config.template.md
---

# 工作区配置

## 审计角色外置 CLI 声明

| 角色 | 承载 CLI | 启动命令 |
|---|---|---|
| Reviewer（审计/红队） | omp | `omp --model openai-codex/gpt-5.6-terra` |
"""


class SetupAgentsTests(TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-setup-test-"))
        self.config_file = self.tmpdir / "workspace-config.md"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_role_table_full(self) -> None:
        roles = parse_role_table(SAMPLE_CONFIG_WITH_FULL_ROLES)
        self.assertEqual(len(roles), 5)
        self.assertIn("Reviewer", roles)
        self.assertEqual(roles["Reviewer"]["cli"], "omp")
        self.assertEqual(roles["Reviewer"]["cmd"], "omp --model openai-codex/gpt-5.6-terra")
        self.assertEqual(roles["Researcher"]["cli"], "subagent")

    def test_parse_role_table_old_format(self) -> None:
        roles = parse_role_table(SAMPLE_CONFIG_OLD_REVIEWER_ONLY)
        self.assertEqual(len(roles), 1)
        self.assertIn("Reviewer", roles)
        self.assertEqual(roles["Reviewer"]["cli"], "omp")

    def test_render_role_section(self) -> None:
        roles = {
            "Reviewer": {"duty": "审计/红队/架构合规", "cli": "omp", "cmd": "omp --model gpt-5"},
            "Researcher": {"duty": "深度调研", "cli": "subagent", "cmd": "内置 Subagent 机制 (auto)"},
        }
        text = render_role_section(roles)
        self.assertIn("## 角色模态外置 CLI 与模型声明", text)
        self.assertIn("| Reviewer |", text)
        self.assertIn("`omp --model gpt-5`", text)
        self.assertIn("| Researcher |", text)

    def test_update_workspace_config_preserves_other_sections(self) -> None:
        self.config_file.write_text(SAMPLE_CONFIG_WITH_FULL_ROLES, encoding="utf-8")
        new_roles = {
            "Reviewer": {"duty": "审计", "cli": "claude", "cmd": "claude -p \"{prompt}\""},
            "Researcher": {"duty": "调研", "cli": "subagent", "cmd": "内置 Subagent 机制 (auto)"},
            "Builder": {"duty": "编码", "cli": "omp", "cmd": "omp --model gpt-5"},
            "Designer": {"duty": "设计", "cli": "subagent", "cmd": "内置 Subagent 机制 (auto)"},
            "Maintainer": {"duty": "守门", "cli": "subagent", "cmd": "内置 Subagent 机制 (auto)"},
        }
        ok = update_workspace_config(self.config_file, new_roles)
        self.assertTrue(ok)

        content = self.config_file.read_text(encoding="utf-8")
        # 验证保留了原有的 Front Matter 和组织名称
        self.assertIn("source: .entropaxis/templates/instance/workspace-config.template.md", content)
        self.assertIn("「示例组织」", content)
        # 验证更新了角色
        self.assertIn("claude", content)
        self.assertIn("omp --model gpt-5", content)

    @mock.patch("shutil.which")
    @mock.patch("subprocess.run")
    def test_detect_installed_agents(self, mock_run: mock.MagicMock, mock_which: mock.MagicMock) -> None:
        def side_effect_which(cmd: str):
            if cmd == "omp":
                return "/usr/local/bin/omp"
            elif cmd == "claude":
                return "/opt/bin/claude"
            return None

        mock_which.side_effect = side_effect_which
        mock_proc = mock.MagicMock()
        mock_proc.stdout = "omp version 1.0.0\n"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        detected = detect_installed_agents()
        self.assertTrue(any(d["id"] == "subagent" and d["installed"] for d in detected))
        omp_item = next(d for d in detected if d["id"] == "omp")
        self.assertTrue(omp_item["installed"])
        self.assertEqual(omp_item["path"], "/usr/local/bin/omp")
        self.assertEqual(omp_item["version"], "omp version 1.0.0")

        gemini_item = next(d for d in detected if d["id"] == "gemini")
        self.assertFalse(gemini_item["installed"])
        self.assertIsNone(gemini_item["path"])

    @mock.patch("shutil.which")
    def test_verify_roles_ok_and_missing(self, mock_which: mock.MagicMock) -> None:
        def side_effect_which(cmd: str):
            if cmd == "omp":
                return "/usr/local/bin/omp"
            return None

        mock_which.side_effect = side_effect_which

        config_text = """# Config
## 角色模态外置 CLI 与模型声明
| 角色模态 | 职责定位 | 承载 CLI | 启动命令 |
|---|---|---|---|
| Reviewer | 审计 | omp | `omp --model gpt` |
| Builder | 编码 | non_existent_cli | `non_existent_cli run` |
| Researcher | 调研 | subagent | 内置 Subagent 机制 (auto) |
"""
        self.config_file.write_text(config_text, encoding="utf-8")
        reports = verify_roles(self.config_file)
        self.assertEqual(len(reports), 3)

        rep_map = {r["role"]: r for r in reports}
        self.assertEqual(rep_map["Reviewer"]["status"], "external_ok")
        self.assertEqual(rep_map["Builder"]["status"], "external_missing")
        self.assertEqual(rep_map["Researcher"]["status"], "subagent_default")
