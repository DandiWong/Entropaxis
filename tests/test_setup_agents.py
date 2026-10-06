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
    HARD_GATED_ROLES,
    KNOWN_AGENTS,
    MODELS_LIMIT,
    ConfigError,
    command_to_argvs,
    detect_installed_agents,
    list_roles,
    load_config,
    parse_model_lines,
    patch_model_in_argv,
    probe_models,
    remove_role,
    role_view,
    save_config,
    set_duty,
    set_model,
    set_role,
    set_timeout,
    verify_roles,
)

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


class HardGateParityTests(TestCase):
    """setup_agents 的门禁集合与 dispatch_role 唯一真源锚定，防两处漂移。"""

    def test_hard_gated_roles_matches_dispatch(self) -> None:
        self.assertEqual(HARD_GATED_ROLES, dr.HARD_ROLES)


class PatchModelArgvTests(TestCase):
    """模型置换是 argv 数组内定点手术：占位符与其余 token 不得受扰。"""

    def test_replaces_existing_model_flag(self) -> None:
        argv = ["omp", "--model", "a/b", "-p", "{PROMPT}"]
        self.assertEqual(patch_model_in_argv(argv, "c/d"), ["omp", "--model", "c/d", "-p", "{PROMPT}"])

    def test_replaces_short_flag(self) -> None:
        self.assertEqual(patch_model_in_argv(["x", "-m", "old"], "new"), ["x", "-m", "new"])

    def test_inserts_before_prompt_flag_when_missing(self) -> None:
        argv = ["claude", "-p", "{PROMPT}"]
        self.assertEqual(patch_model_in_argv(argv, "sonnet-5"), ["claude", "--model", "sonnet-5", "-p", "{PROMPT}"])

    def test_inserts_after_executable_when_no_prompt(self) -> None:
        self.assertEqual(patch_model_in_argv(["cli", "run"], "m"), ["cli", "--model", "m", "run"])


class PartialUpdateTests(TestCase):
    """局部更新契约：只动声明的字段，链结构与其余参数原样。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-partial-test-"))
        self.cfg = self.tmpdir / "roles.yaml"
        self.data = load_config(self.cfg)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _with_reviewer_chain(self) -> None:
        set_role(self.data, "Reviewer", "omp", "omp --model a/b || claude")
        self.data["command_profiles"]["reviewer-primary"]["timeout_s"] = 1800

    def test_set_model_touches_primary_only(self) -> None:
        self._with_reviewer_chain()
        set_model(self.data, "Reviewer", "x/y")
        argv = self.data["command_profiles"]["reviewer-primary"]["argv"]
        self.assertIn("x/y", argv)
        self.assertIn("{PROMPT}", argv, "占位符必须保留")
        self.assertNotIn("a/b", self.data["command_profiles"]["reviewer-primary"]["argv"])
        self.assertNotIn("x/y", self.data["command_profiles"]["reviewer-fallback"]["argv"], "备选链不得同换")

    def test_set_model_rejects_subagent_carrier(self) -> None:
        with self.assertRaises(ConfigError):
            set_model(self.data, "Designer", "m")

    def test_set_model_rejects_unknown_role(self) -> None:
        with self.assertRaises(ConfigError):
            set_model(self.data, "Nobody", "m")

    def test_set_timeout_updates_whole_chain(self) -> None:
        self._with_reviewer_chain()
        set_timeout(self.data, "Reviewer", 1200)
        for prof in self.data["command_profiles"].values():
            if prof["argv"][0] == "omp" or prof["argv"][0] == "claude":
                self.assertEqual(prof["timeout_s"], 1200)

    def test_set_timeout_hard_gate_cap_1800(self) -> None:
        self._with_reviewer_chain()
        with self.assertRaises(ConfigError):
            set_timeout(self.data, "Reviewer", 1801)

    def test_set_timeout_schema_cap_7200(self) -> None:
        set_role(self.data, "Builder", "omp", "omp --model a/b")
        with self.assertRaises(ConfigError):
            set_timeout(self.data, "Builder", 7201)

    def test_set_duty_updates_field(self) -> None:
        set_duty(self.data, "Researcher", "前沿查新")
        self.assertEqual(self.data["roles"]["Researcher"]["duty"], "前沿查新")

    def test_set_duty_rejects_blank(self) -> None:
        with self.assertRaises(ConfigError):
            set_duty(self.data, "Researcher", "  ")

    def test_set_duty_rejects_unknown_role(self) -> None:
        with self.assertRaises(ConfigError):
            set_duty(self.data, "Nobody", "x")


class RemoveRoleTests(TestCase):
    """删除契约：标准角色 fail-closed；自定义角色删除后只回收独占 profile。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-remove-test-"))
        self.cfg = self.tmpdir / "roles.yaml"
        self.data = load_config(self.cfg)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_standard_role_refused(self) -> None:
        with self.assertRaises(ConfigError):
            remove_role(self.data, "Reviewer")

    def test_unknown_role_refused(self) -> None:
        with self.assertRaises(ConfigError):
            remove_role(self.data, "Nobody")

    def test_removes_custom_role_and_gcs_its_chain(self) -> None:
        set_role(self.data, "DataSteward", "omp", "omp --model a/b || claude")
        save_config(self.cfg, self.data)
        removed = remove_role(self.data, "DataSteward")
        self.assertNotIn("DataSteward", self.data["roles"])
        self.assertEqual(set(removed), {"datasteward-primary", "datasteward-fallback"})
        self.assertNotIn("datasteward-primary", self.data["command_profiles"])
        for role in ("Architecture", "Researcher", "Designer", "Builder", "Reviewer", "Maintainer", "Reporter"):
            self.assertIn(role, self.data["roles"], "标准角色不受删除影响")

    def test_keeps_profile_referenced_by_other_chain(self) -> None:
        # 共享场景：DataSteward 的链 fallback 指向 Reviewer 的 profile，删除时不得回收它
        set_role(self.data, "Reviewer", "omp", "omp --model a/b")
        set_role(self.data, "DataSteward", "omp", "omp --model a/b")
        self.data["command_profiles"]["datasteward-primary"]["fallback_profile"] = "reviewer-primary"
        removed = remove_role(self.data, "DataSteward")
        self.assertEqual(removed, ["datasteward-primary"])
        self.assertIn("reviewer-primary", self.data["command_profiles"], "被他链引用的 profile 不得回收")


class ListRolesTests(TestCase):
    """清单视图：门禁/承载/模型/超时一字排开，自定义角色显式标 custom。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-list-test-"))
        self.data = load_config(self.tmpdir / "roles.yaml")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch("tools.setup_agents.shutil.which")
    def test_list_gates_and_ordering(self, mock_which: mock.MagicMock) -> None:
        mock_which.return_value = "/usr/bin/omp"
        set_role(self.data, "DataSteward", "omp", "omp --model a/b")
        items = list_roles(self.data)
        by_role = {i["role"]: i for i in items}
        self.assertEqual(by_role["Reviewer"]["gate"], "hard")
        self.assertEqual(by_role["Builder"]["gate"], "soft")
        self.assertEqual(by_role["DataSteward"]["gate"], "custom")
        self.assertEqual(by_role["DataSteward"]["standard"], False)
        self.assertEqual(items[-1]["role"], "DataSteward", "自定义角色排标准角色之后")
        self.assertEqual(by_role["DataSteward"]["model"], "a/b")
        self.assertEqual(by_role["DataSteward"]["timeout_s"], 900)

    def test_list_subagent_carrier(self) -> None:
        items = list_roles(self.data)
        designer = next(i for i in items if i["role"] == "Designer")
        self.assertEqual(designer["carrier"], "subagent")
        self.assertEqual(designer["chain"], [])

    @mock.patch("tools.setup_agents.shutil.which")
    def test_verify_notes_custom_roles(self, mock_which: mock.MagicMock) -> None:
        mock_which.return_value = "/usr/bin/omp"
        cfg = self.tmpdir / "roles.yaml"
        set_role(self.data, "DataSteward", "omp", "omp --model a/b")
        save_config(cfg, self.data)
        notes = [r for r in verify_roles(cfg) if r["status"] == "custom_role_note"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["role"], "DataSteward")
        self.assertIn("仅作承载声明", notes[0]["msg"])


class ParseModelLinesTests(TestCase):
    """列模型 stdout 解析：杂讯行剔除、格式过滤、截断。"""

    def test_tab_parse_skips_noise_lines(self) -> None:
        out = "Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\n\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
        self.assertEqual(parse_model_lines(out, "tab"), ["gemini-3.8-flash-high", "gemini-3.8-flash-low"])

    def test_lines_parse_keeps_only_slash_ids(self) -> None:
        out = "Available models:\nopencode/big-pickle\nlocal model line\nopencode/mimo-v2.6-flash-free\n"
        self.assertEqual(parse_model_lines(out, "lines"), ["opencode/big-pickle", "opencode/mimo-v2.6-flash-free"])

    def test_dedup_and_truncate(self) -> None:
        out = "\n".join(f"p/m{i}" for i in range(30)) + "\np/m0"
        ids = parse_model_lines(out, "lines")
        self.assertEqual(len(ids), MODELS_LIMIT)
        self.assertEqual(len(set(ids)), MODELS_LIMIT, "重复 id 不得占位")


class ProbeModelsTests(TestCase):
    """枚举探测软降级：非零退出/异常不阻断扫描，错误留痕。"""

    def _agent(self, **kw):
        from tools.setup_agents import AgentInfo
        base = dict(id="x", name="X", cli="x", version_cmd=[], description="", presets={})
        return AgentInfo(**(base | kw))

    def test_no_models_cmd_returns_empty(self) -> None:
        self.assertEqual(probe_models(self._agent()), {})

    @mock.patch("tools.setup_agents.subprocess.run")
    def test_success_parses_and_marks_truncation(self, mock_run: mock.MagicMock) -> None:
        mock_run.return_value = mock.MagicMock(returncode=0, stdout="a/b\nc/d\n" + "\n".join(f"p/m{i}" for i in range(30)) + "\n", stderr="")
        out = probe_models(self._agent(models_cmd=("x", "models"), models_parse="lines"))
        self.assertEqual(len(out["available_models"]), MODELS_LIMIT)
        self.assertTrue(out.get("models_truncated"))

    @mock.patch("tools.setup_agents.subprocess.run")
    def test_nonzero_exit_leaves_trace(self, mock_run: mock.MagicMock) -> None:
        mock_run.return_value = mock.MagicMock(returncode=1, stdout="", stderr="boom")
        out = probe_models(self._agent(models_cmd=("x", "models"), models_parse="lines"))
        self.assertEqual(out["available_models"], [])
        self.assertEqual(out["models_error"], "boom")

    @mock.patch("tools.setup_agents.subprocess.run", side_effect=OSError("nope"))
    def test_exception_soft_degrades(self, _: mock.MagicMock) -> None:
        out = probe_models(self._agent(models_cmd=("x", "models"), models_parse="lines"))
        self.assertEqual(out["available_models"], [])
        self.assertEqual(out["models_error"], "OSError")


class NewAgentEntriesTests(TestCase):
    """2026-10-07 调研新增条目：枚举能力声明 + 全部预设可安全转结构化 argv。"""

    def test_enumerable_entries_declared(self) -> None:
        by_id = {a.id: a for a in KNOWN_AGENTS}
        self.assertEqual(by_id["agy"].models_cmd, ("agy", "models"))
        self.assertEqual(by_id["agy"].models_parse, "tab")
        self.assertEqual(by_id["opencode"].models_cmd, ("opencode", "models"))
        self.assertEqual(by_id["opencode"].models_parse, "lines")
        self.assertEqual(by_id["mimo"].models_cmd, ("mimo", "models"))
        self.assertEqual(by_id["mimo"].models_parse, "lines")
        self.assertEqual(by_id["pi"].models_cmd, (), "pi 需 pattern 搜索，不自动枚举")

    def test_new_agent_presets_convert_to_argv(self) -> None:
        # opencode/mimo 的 headless 是 run <位置参数>，预设必须显式含 {prompt}；
        # 任何一条预设转不出含 {PROMPT} 的 argv，初始化向导就会静默保持旧配置
        for agent in KNOWN_AGENTS:
            if agent.id not in ("pi", "agy", "opencode", "mimo"):
                continue
            for role, preset in agent.presets.items():
                with self.subTest(agent=agent.id, role=role):
                    argvs = command_to_argvs(preset)
                    self.assertTrue(any("{PROMPT}" in argv for argv in argvs))

    @mock.patch("tools.setup_agents.shutil.which")
    @mock.patch("tools.setup_agents.subprocess.run")
    def test_scan_wires_model_enumeration(self, mock_run: mock.MagicMock, mock_which: mock.MagicMock) -> None:
        mock_which.side_effect = lambda c: "/usr/bin/agy" if c == "agy" else None
        mock_run.side_effect = [
            mock.MagicMock(returncode=0, stdout="agy 1.2.7\n", stderr=""),   # --version
            mock.MagicMock(returncode=0, stdout="gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n", stderr=""),  # models
        ]
        detected = {i["id"]: i for i in detect_installed_agents()}
        self.assertEqual(detected["agy"]["version"], "agy 1.2.7")
        self.assertEqual(detected["agy"]["available_models"], ["gemini-3.8-flash-high"])
        # 未安装的 CLI 不得出现枚举字段
        self.assertNotIn("available_models", detected["gemini"])
