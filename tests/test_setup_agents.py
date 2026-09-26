import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

import yaml

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools import dispatch_role as dr
from tools.setup_agents import (
    ConfigError,
    command_to_argvs,
    detect_installed_agents,
    load_config,
    parse_role_table,
    role_view,
    save_config,
    set_role,
    verify_roles,
)

LEGACY_TABLE = """## 审计角色外置 CLI 声明

| 角色 | 承载 CLI | 启动命令 |
|---|---|---|
| Reviewer（审计/红队） | omp | `omp --model openai-codex/gpt-5.6-terra` |
"""


class RolesYamlTests(TestCase):
    """roles.yaml 是角色承载唯一真源：命令以结构化 argv 存 command_profiles，角色只引用 profile。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-setup-test-"))
        self.cfg = self.tmpdir / "roles.yaml"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_loads_template_defaults(self) -> None:
        data = load_config(self.cfg)
        self.assertEqual(data["default_dispatch_mode"], "strict")
        self.assertIsNone(data["roles"]["Reviewer"]["profile"])
        self.assertFalse(self.cfg.exists(), "读取不得顺带落盘")

    def test_set_role_builds_fallback_chain(self) -> None:
        data = load_config(self.cfg)
        set_role(data, "Reviewer", "omp", "omp --model a/b || claude -p {prompt}")
        save_config(self.cfg, data)
        saved = yaml.safe_load(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["Reviewer"]["profile"], "reviewer-primary")
        profs = saved["command_profiles"]
        self.assertEqual(profs["reviewer-primary"]["argv"], ["omp", "--model", "a/b", "-p", "{PROMPT}"])
        self.assertEqual(profs["reviewer-primary"]["fallback_profile"], "reviewer-fallback")
        self.assertEqual(profs["reviewer-fallback"]["argv"], ["claude", "-p", "{PROMPT}"])

    def test_back_to_subagent_removes_orphans_but_keeps_shared(self) -> None:
        data = load_config(self.cfg)
        set_role(data, "Reviewer", "omp", "omp --model a || omp --model b")
        data["command_profiles"]["builder-verify"] = {"argv": ["python3", "-m", "pytest"], "timeout_s": 60}
        set_role(data, "Reviewer", "subagent", "")
        self.assertIsNone(data["roles"]["Reviewer"]["profile"])
        self.assertNotIn("reviewer-primary", data["command_profiles"])
        self.assertNotIn("reviewer-fallback", data["command_profiles"])
        self.assertIn("builder-verify", data["command_profiles"], "非本角色链的 profile 不得被清理")

    def test_timeout_preserved_on_rewrite(self) -> None:
        data = load_config(self.cfg)
        set_role(data, "Reviewer", "omp", "omp --model a")
        data["command_profiles"]["reviewer-primary"]["timeout_s"] = 1800
        set_role(data, "Reviewer", "omp", "omp --model b")
        self.assertEqual(data["command_profiles"]["reviewer-primary"]["timeout_s"], 1800)

    def test_unsafe_or_unbindable_commands_refused(self) -> None:
        with self.assertRaises(ConfigError):
            command_to_argvs("gemini --fast")  # 未知 CLI 族且无 {PROMPT}：任务文本无处注入
        with self.assertRaises(ConfigError):
            command_to_argvs("omp -p {PROMPT} > out.md")
        with self.assertRaises(ConfigError):
            command_to_argvs("a {PROMPT} || b {PROMPT} || c {PROMPT} || d {PROMPT}")
        self.assertEqual(command_to_argvs('gemini "{prompt}"'), [["gemini", "{PROMPT}"]])

    def test_role_name_must_be_ascii(self) -> None:
        with self.assertRaises(ConfigError):
            set_role(load_config(self.cfg), "审计员", "omp", "omp")

    def test_schema_violation_not_written(self) -> None:
        data = load_config(self.cfg)
        data["command_profiles"]["bad"] = {"argv": ["a;b"], "timeout_s": 5}
        with self.assertRaises(ConfigError):
            save_config(self.cfg, data)
        self.assertFalse(self.cfg.exists())

    def test_role_view_joins_chain(self) -> None:
        data = load_config(self.cfg)
        set_role(data, "Builder", "omp", "omp --model x || claude")
        view = role_view(data)
        self.assertEqual(view["Builder"]["cli"], "omp")
        self.assertIn(" || ", view["Builder"]["cmd"])
        self.assertEqual(view["Designer"]["cli"], "subagent")

    @mock.patch("tools.setup_agents.shutil.which")
    def test_verify_roles(self, mock_which: mock.MagicMock) -> None:
        mock_which.side_effect = lambda c: "/usr/bin/omp" if c == "omp" else None
        data = load_config(self.cfg)
        set_role(data, "Reviewer", "omp", "omp --model a")
        set_role(data, "Builder", "ghost", "ghost {PROMPT}")
        data["roles"]["Maintainer"]["profile"] = "maintainer-primary"  # 引用未定义 profile
        save_config(self.cfg, data)
        reps = {r["role"]: r["status"] for r in verify_roles(self.cfg)}
        self.assertEqual(reps["Reviewer"], "external_ok")
        self.assertEqual(reps["Builder"], "external_missing")
        self.assertEqual(reps["Maintainer"], "profile_missing")
        self.assertEqual(reps["Designer"], "subagent_default")

    def test_legacy_table_still_readable(self) -> None:
        roles = parse_role_table(LEGACY_TABLE)
        self.assertEqual(roles["Reviewer"]["cli"], "omp")


class Tier3RolesResolutionTests(TestCase):
    """dispatch_role 第 3 级：roles.<角色>.profile 显式声明优先于约定名。"""

    def _cfg(self, roles: dict, profiles: dict) -> dict:
        return {"roles": roles, "command_profiles": profiles, "path": "roles.yaml"}

    def test_declared_profile_wins(self) -> None:
        cfg = self._cfg({"Reviewer": {"duty": "d", "profile": "rv-x"}},
                        {"rv-x": {"argv": ["omp", "{PROMPT}"], "timeout_s": 5},
                         "reviewer-primary": {"argv": ["other", "{PROMPT}"], "timeout_s": 5}})
        name, chain = dr._tier3_default_chain("Reviewer", cfg)
        self.assertEqual((name, chain[0]["name"]), ("rv-x", "rv-x"))

    def test_null_profile_is_subagent(self) -> None:
        cfg = self._cfg({"Reviewer": {"duty": "d", "profile": None}},
                        {"reviewer-primary": {"argv": ["omp", "{PROMPT}"], "timeout_s": 5}})
        self.assertEqual(dr._tier3_default_chain("Reviewer", cfg), ("SUBAGENT_AUTO", []))

    def test_dangling_profile_is_unconfigured(self) -> None:
        cfg = self._cfg({"Reviewer": {"duty": "d", "profile": "nope"}}, {})
        self.assertEqual(dr._tier3_default_chain("Reviewer", cfg), ("UNCONFIGURED", []))

    def test_yaml_config_never_falls_back_to_table(self) -> None:
        self.assertEqual(dr._tier3_default_chain("Builder", self._cfg({}, {})), ("UNCONFIGURED", []))


class DetectAgentsTests(TestCase):
    @mock.patch("shutil.which")
    @mock.patch("subprocess.run")
    def test_detect_installed_agents(self, mock_run: mock.MagicMock, mock_which: mock.MagicMock) -> None:
        mock_which.side_effect = lambda c: {"omp": "/usr/local/bin/omp", "claude": "/opt/bin/claude"}.get(c)
        mock_run.return_value = mock.MagicMock(stdout="omp version 1.0.0\n", stderr="")
        detected = {d["id"]: d for d in detect_installed_agents()}
        self.assertTrue(detected["subagent"]["installed"])
        self.assertEqual(detected["omp"]["path"], "/usr/local/bin/omp")
        self.assertEqual(detected["omp"]["version"], "omp version 1.0.0")
        self.assertFalse(detected["gemini"]["installed"])
