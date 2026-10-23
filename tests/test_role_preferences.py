"""Consumer-visible preference order, authorization and isolated invocation tests."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

SYSTEM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM))
from tools import role_preferences as rp, dispatch_role as dr, validate_schema as vs


class PreferenceExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "calls.jsonl"
        executable = self.root / "omp"
        executable.write_text(f'''#!{sys.executable}
import json, os, re, subprocess, sys, time
from pathlib import Path
marker = os.environ.get("PROBE_STARTUP_MARKER")
if marker:
    Path(marker).write_text("started")
args = sys.argv[1:]
model = args[args.index("--model") + 1]
prompt = args[args.index("-p") + 1]
system_prompt = args[args.index("--system-prompt") + 1] if "--system-prompt" in args else ""
global_system = Path(os.environ["HOME"]) / ".omp" / "SYSTEM.md"
with open(os.environ["PROBE_LOG"], "a") as stream:
    stream.write(json.dumps({{"cwd": os.getcwd(), "stdin": sys.stdin.read()}}) + "\\n")
if model.endswith("fail"):
    sys.exit(4)
if model.endswith("descendant"):
    if model.endswith("closed-descendant"):
        child_code = "import os,signal,sys,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);os.close(1);os.close(2);Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
        subprocess.Popen([sys.executable, "-c", child_code, os.environ["PROBE_CHILD_PID"]])
        time.sleep(30)
    child_code = "import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
    subprocess.Popen([sys.executable, "-c", child_code, os.environ["PROBE_CHILD_PID"]])
    time.sleep(30)
if model.endswith("slow"):
    time.sleep(2)
if model.endswith("echo"):
    print(json.dumps({{"type": "message_end", "message": {{"role": "user", "content": [{{"type": "text", "text": prompt}}]}}}}))
    sys.exit(0)
nonce = re.search(r"ENTROPAXIS_[a-f0-9]+", prompt).group()
provider, separator, response_model = model.partition("/")
if model.endswith("provider-mismatch"):
    provider = "different-provider"
elif model.endswith("case-mismatch"):
    response_model = response_model.swapcase()
elif model.endswith("mismatch"):
    response_model = "different-model"
response = nonce if not global_system.exists() or system_prompt else global_system.read_text()
message = {{"role": "assistant", "provider": provider, "model": response_model, "content": [{{"type": "text", "text": response}}], "stopReason": "stop"}}
if model.endswith("missing"):
    message.pop("model")
print(json.dumps({{"type": "message_end", "message": message}}))
''', encoding="utf-8")
        executable.chmod(0o755)
        env = mock.patch.dict(
            os.environ,
            {"PATH": str(self.root), "PROBE_LOG": str(self.log), "PROBE_STARTUP_MARKER": str(self.root / "started")},
        )
        env.start()
        self.addCleanup(env.stop)

    def config(self, models):
        names = [f"choice-{i}" for i in range(1, len(models) + 1)]
        return {"roles": {"Researcher": {"duty": "research", "preferences": names}},
                "command_profiles": {n: {"agent": "omp", "model": m, "timeout_s": 30} for n, m in zip(names, models)}}

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_missing_authorization_never_launches_and_preserves_config(self):
        data = self.config(["provider/ok"])
        before = copy.deepcopy(data)
        with mock.patch.object(rp.subprocess, "Popen", side_effect=AssertionError("unauthorized process")):
            for kwargs in ({}, {"authorized": True}, {"authorization_event": "user consent"}):
                result = rp.select_preferences(data, **kwargs)
                self.assertEqual(result[0]["outcome"], "authorization_required")
        self.assertEqual(data, before)

    def test_first_actual_response_wins_not_exit_code_or_prompt_echo(self):
        data = self.config(["provider/fail", "provider/echo", "provider/ok", "provider/unused"])
        before = copy.deepcopy(data)
        report = rp.select_preferences(data, authorized=True, authorization_event="user said yes")[0]
        self.assertEqual((report["outcome"], report["first_available_preference"]), ("reported", 3))
        self.assertEqual([item["status"] for item in report["attempts"]][:3], ["call_failed", "invalid_response", "available"])
        self.assertEqual(len(report["attempts"]), 4, "逐个偏好都实测并报告")
        self.assertEqual(data, before)
        self.assertEqual(len(self.calls()), 4)
        for call in self.calls():
            self.assertNotEqual(call["cwd"], str(self.root))
            self.assertEqual(call["stdin"], "")
            self.assertFalse(Path(call["cwd"]).exists())

    def test_protocol_identity_requires_exact_provider_and_case(self):
        data = self.config(["google-antigravity/Gemini-Exact:high"])
        report = rp.select_preferences(data, authorized=True, authorization_event="explicit permission")[0]
        self.assertEqual((report["outcome"], report["attempts"][0]["status"]), ("reported", "available"))

        data = self.config(
            [
                "provider/missing",
                "provider/mismatch",
                "provider/provider-mismatch",
                "google-antigravity/Gemini-Exact:case-mismatch",
            ],
        )
        before = copy.deepcopy(data)
        report = rp.select_preferences(data, authorized=True, authorization_event="explicit permission")[0]
        self.assertEqual(
            [attempt["status"] for attempt in report["attempts"]],
            ["identity_missing", "identity_mismatch", "identity_mismatch", "identity_mismatch"],
        )
        self.assertEqual((report["outcome"], report["first_available_preference"]), ("reported", None))
        self.assertEqual(data, before)

    def test_claude_requested_alias_cannot_override_billed_model_identity(self):
        executable = self.root / "claude"
        executable.write_text(f'''#!{sys.executable}
import json, re, sys
prompt = sys.argv[sys.argv.index("-p") + 1]
nonce = re.search(r"ENTROPAXIS_[a-f0-9]+", prompt).group()
print(json.dumps({{"type": "result", "result": nonce, "model": "requested-alias",
                  "modelUsage": {{"actual-model": {{"inputTokens": 1}}}}}}))
''', encoding="utf-8")
        executable.chmod(0o755)
        data = self.config(["requested-alias"])
        data["command_profiles"]["choice-1"]["agent"] = "claude"
        report = rp.select_preferences(data, authorized=True, authorization_event="fixture consent")[0]
        self.assertEqual(report["attempts"][0]["status"], "identity_mismatch")
        self.assertIsNone(report["first_available_preference"])

    def test_unisolated_adapters_do_not_launch_or_change_selection(self):
        for agent in ("agy", "opencode", "mimo"):
            executable = self.root / agent
            executable.write_bytes((self.root / "omp").read_bytes())
            executable.chmod(0o755)
            data = self.config(["provider/ok"])
            data["command_profiles"]["choice-1"]["agent"] = agent
            before = copy.deepcopy(data)
            report = rp.select_preferences(data, authorized=True, authorization_event="fixture consent")[0]
            self.assertEqual(report["attempts"][0]["status"], "context_unisolated")
            self.assertEqual(data, before)
            self.assertFalse((self.root / "started").exists())

    def test_local_phase_only_discovers_binary_without_launching(self):
        data = self.config(["provider/ok"])
        before = copy.deepcopy(data)
        report = rp.local_candidates(data)[0]
        self.assertEqual(report["preferences"][0]["status"], "local_ready")
        self.assertEqual(data, before)
        self.assertFalse((self.root / "started").exists())
        self.assertEqual(self.calls(), [])


    def test_timeout_advances_and_all_fail_preserves_existing_or_null(self):
        data = self.config(["provider/slow", "provider/ok"])
        result = rp.select_preferences(data, authorized=True, authorization_event="yes", timeout=1)[0]
        self.assertEqual((result["attempts"][0]["status"], result["first_available_preference"]), ("timeout", 2))
        data = self.config(["provider/fail"])
        before = copy.deepcopy(data)
        result = rp.select_preferences(data, authorized=True, authorization_event="yes")[0]
        self.assertIsNone(result["first_available_preference"])
        self.assertEqual(data, before)


    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_reaps_descendant_process_group(self):
        child_pid = self.root / "child.pid"
        for model in ("provider/descendant", "provider/closed-descendant"):
            data = self.config([model])
            # 探针最小超时 1s 是启动预算：高负载下 SIGKILL 可能先于后代写出 pid，此时没有可验证的后代，
            # 重试至后代真正起来（最多 5 次）；回收窗口 3s 只量 SIGKILL 后内核清理，与负载无关。
            for _ in range(5):
                child_pid.unlink(missing_ok=True)
                with mock.patch.dict(os.environ, {"PROBE_CHILD_PID": str(child_pid)}):
                    report = rp.select_preferences(data, authorized=True, authorization_event="yes", timeout=1)[0]
                self.assertEqual(report["attempts"][0]["status"], "timeout")
                if child_pid.exists() and child_pid.read_text().strip():
                    break
            else:
                self.fail("descendant never started within the probe timeout; cannot verify reaping")
            pid = int(child_pid.read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("timed-out probe left its descendant running")

    def test_same_pair_called_once_per_authorized_operation(self):
        data = self.config(["provider/ok"])
        data["roles"]["Reporter"] = {"duty": "report", "preferences": ["choice-1"]}
        result = rp.select_preferences(data, authorized=True, authorization_event="yes")
        self.assertEqual([row["first_available_preference"] for row in result], [1, 1])
        self.assertEqual(len(self.calls()), 1)

    def test_dispatch_walks_past_three_and_records_actual_number(self):
        bad = self.root / "bad"
        bad.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
        bad.chmod(0o755)
        good = self.root / "good"
        good.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nPath(sys.argv[2]).write_text('# Changed output\\n')\n")
        good.chmod(0o755)
        out = self.root / "result.md"
        names = [f"candidate-{i}" for i in range(1, 5)]
        profiles = {name: {"argv": [str(good if i == 4 else bad), rp.PROMPT, str(out)], "timeout_s": 5}
                    for i, name in enumerate(names, 1)}
        config = {"roles": {"Designer": {"preferences": names}}, "command_profiles": profiles}
        source, chain = dr._tier3_default_chain("Designer", config)
        with mock.patch.object(dr, "TRACE_LOG", self.root / "trace.jsonl"):
            failure, attempts, ok = dr.execute_chain(chain, "multiline\nquoted ; prompt", self.root, out)
        self.assertTrue(ok)
        self.assertEqual(failure, "")
        self.assertEqual([item["preference_number"] for item in attempts], [1, 2, 3, 4])
        self.assertEqual(attempts[-1]["profile"], names[3])
        self.assertEqual(out.read_text(), "# Changed output\n")
        self.assertEqual([p["preference_number"] for p in chain], [1, 2, 3, 4], "总从第 1 个偏好起建链")
        config["roles"]["Designer"]["preferences"] = []
        self.assertEqual(dr._tier3_default_chain("Designer", config), ("SUBAGENT_AUTO", []))

    def test_cli_authorization_and_dry_run_preserve_disk_and_report_first_available(self):
        config = self.root / "roles.yaml"
        config.write_text(yaml.safe_dump(self.config(["provider/fail", "provider/ok"])))
        before = config.read_bytes()
        command = [sys.executable, str(SYSTEM / "tools" / "setup_agents.py"), "--config", str(config)]
        unauthorized = subprocess.run(command + ["--check-preferences"], capture_output=True, text=True, timeout=15)
        self.assertEqual(unauthorized.returncode, 1, unauthorized.stderr)
        self.assertEqual(json.loads(unauthorized.stdout.splitlines()[0])["outcome"], "authorization_required")
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.calls(), [])
        for options in (
            ["--check-preferences", "--authorize-model-check", "--authorization-event", "fixture consent", "--dry-run"],
            ["--set-model", "Researcher", "changed/model", "--preference", "2", "--dry-run"],
            ["--remove-preference", "Researcher", "2", "--yes", "--dry-run"],
            ["--migrate-preferences", "--dry-run"],
        ):
            result = subprocess.run(command + options, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(self.calls(), [])
        authorized = subprocess.run(command + ["--check-preferences", "--authorize-model-check", "--authorization-event", "fixture consent"],
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(authorized.returncode, 0, authorized.stderr)
        self.assertEqual(json.loads(authorized.stdout.splitlines()[0])["first_available_preference"], 2)
        self.assertEqual(config.read_bytes(), before, "已授权检查前后 roles.yaml 字节不变")
        self.assertEqual(len(self.calls()), 2)


class PreferenceContractTests(unittest.TestCase):
    def test_migration_preserves_long_order_ids_timeouts_and_verifier(self):
        profiles = {f"p-{i}": {"argv": ["omp", "--model", f"provider/model-{i}", "-p", rp.PROMPT],
                               "timeout_s": 800 + i, "note": f"user note {i}"} for i in range(5)}
        for i in range(4):
            profiles[f"p-{i}"]["fallback_profile"] = f"p-{i + 1}"
        profiles["verify"] = {"argv": ["python3", "-m", "pytest"], "timeout_s": 600}
        data = {"roles": {"Builder": {"duty": "build", "profile": "p-0"}}, "command_profiles": profiles}
        original_verify = copy.deepcopy(profiles["verify"])
        rp.migrate_config(data)
        self.assertEqual(data["roles"]["Builder"]["preferences"], [f"p-{i}" for i in range(5)])
        self.assertNotIn("profile", data["roles"]["Builder"])
        self.assertEqual(profiles["verify"], original_verify)
        # note 已从契约删除（零读者字段）：迁移时剥掉，否则旧实例一跑写入命令就把
        # 已删字段带回 schema 校验。timeout_s 与 profile ID 一律保留。
        for i in range(5):
            self.assertEqual(profiles[f"p-{i}"], {"agent": "omp", "model": f"provider/model-{i}", "timeout_s": 800 + i})
        migrated = copy.deepcopy(data)
        rp.migrate_config(data)
        self.assertEqual(data, migrated)

    def test_migration_drops_profile_and_moves_old_selection_to_front(self):
        profiles = {n: {"agent": "omp", "model": f"provider/{n}"} for n in ("a", "b", "c")}
        data = {"roles": {"Builder": {"duty": "b", "preferences": ["a", "b", "c"], "profile": "c"},
                          "Reporter": {"duty": "r", "preferences": ["a", "b"], "profile": "a"},
                          "Designer": {"duty": "d", "preferences": ["b"], "profile": None}},
                "command_profiles": profiles}
        notes = rp.migrate_config(data)
        self.assertTrue(all("profile" not in entry for entry in data["roles"].values()))
        self.assertEqual(data["roles"]["Builder"]["preferences"], ["c", "a", "b"])
        self.assertEqual(data["roles"]["Reporter"]["preferences"], ["a", "b"])
        self.assertEqual(len([n for n in notes if n.startswith("Builder")]), 1)
        self.assertTrue(any(n.startswith("Designer") and "null" in n for n in notes))
        self.assertEqual(rp.migrate_config(data), [])

    def test_schema_rejects_dual_profiles_unknown_agent_and_timeout_overflow(self):
        schema = vs.load_schema("command_profile")
        for profile in ({"agent": "omp", "model": "provider/name", "argv": ["omp", rp.PROMPT], "timeout_s": 30},
                        {"agent": "unknown", "model": "provider/name", "timeout_s": 30},
                        {"agent": "omp", "model": "provider/name", "timeout_s": 7201},
                        {"agent": "omp", "timeout_s": 30},
                        {"agent": "omp", "model": "provider/name", "fallback_profile": "other", "timeout_s": 30},
                        # note 已从契约删除：残留字段一律按未声明拒绝，否则旧实例会把它带回来
                        {"agent": "omp", "model": "provider/name", "timeout_s": 30, "note": "x"},
                        {"argv": ["omp", "-p", rp.PROMPT], "timeout_s": 0}):
            with self.subTest(profile=profile):
                self.assertTrue(vs.validate({"pair": profile}, schema))
        # timeout_s 可选：缺省由角色级/default_timeout_s 解析（见 effective_timeout）
        for profile in ({"agent": "pi", "model": "provider/name", "timeout_s": 30},
                        {"agent": "pi", "model": "provider/name"},
                        {"argv": ["python3", "-m", "pytest"]}):
            with self.subTest(profile=profile):
                self.assertEqual(vs.validate({"pair": profile}, schema), [])

    def test_effective_timeout_resolves_role_level_before_default(self):
        """同一 profile 跨角色共享时，超时按角色各自解析（Builder 3600 / Reviewer 1800 共用同一配对）。"""
        data = {"default_timeout_s": 900,
                "roles": {"Builder": {"duty": "b", "preferences": ["shared"], "timeout_s": 3600},
                          "Reviewer": {"duty": "r", "preferences": ["shared"], "timeout_s": 1800}},
                "command_profiles": {"shared": {"agent": "omp", "model": "provider/name"}}}
        profile = data["command_profiles"]["shared"]
        self.assertEqual(rp.effective_timeout(data, profile, "Builder"), 3600)
        self.assertEqual(rp.effective_timeout(data, profile, "Reviewer"), 1800)
        # profile 覆盖优先于角色级
        profile["timeout_s"] = 60
        self.assertEqual(rp.effective_timeout(data, profile, "Builder"), 60)
        del profile["timeout_s"]
        # 未声明角色级时落到 default_timeout_s
        data["roles"]["Reporter"] = {"duty": "x", "preferences": ["shared"]}
        self.assertEqual(rp.effective_timeout(data, profile, "Reporter"), 900)
        del data["default_timeout_s"]
        self.assertEqual(rp.effective_timeout(data, profile, "Reporter"), rp.FALLBACK_TIMEOUT_S)

    def test_hard_gate_timeout_rejected_at_role_and_profile_level(self):
        def build(role_timeout=None, profile_timeout=None):
            profile = {"agent": "omp", "model": "provider/name"}
            if profile_timeout is not None:
                profile["timeout_s"] = profile_timeout
            role = {"duty": "review", "preferences": ["one"]}
            if role_timeout is not None:
                role["timeout_s"] = role_timeout
            return {"roles": {"Reviewer": role}, "command_profiles": {"one": profile}}

        cap = rp.hard_role_max_timeout("Reviewer")
        for label, data in (("role-level", build(role_timeout=cap + 1)),
                            ("profile override", build(profile_timeout=cap + 1)),
                            ("default overflow", {"default_timeout_s": rp.MAX_TIMEOUT_S + 1, "roles": {"Reviewer": {"duty": "r", "preferences": ["one"]}},
                                                  "command_profiles": {"one": {"agent": "omp", "model": "provider/name"}}})):
            with self.subTest(level=label):
                self.assertTrue(rp.semantic_errors(data))
        # 硬门禁上限本身合法；非硬门禁角色不受硬门禁上限约束，只受 schema 上限约束
        self.assertEqual(rp.semantic_errors(build(role_timeout=cap)), [])
        self.assertEqual(rp.semantic_errors(build(profile_timeout=cap)), [])
        top = rp.MAX_TIMEOUT_S
        self.assertEqual(rp.semantic_errors(build(role_timeout=top) | {"roles": {"Builder": build(role_timeout=top)["roles"]["Reviewer"]}}), [])

    def test_invalid_selection_duplicate_reference_and_hard_timeout_rejected(self):
        cap = rp.hard_role_max_timeout("Reviewer")
        data = {"roles": {"Reviewer": {"duty": "review", "preferences": ["one"]}},
                "command_profiles": {"one": {"agent": "omp", "model": "provider/name", "timeout_s": cap + 1}}}
        self.assertTrue(rp.semantic_errors(data))
        data["command_profiles"]["one"]["timeout_s"] = cap
        self.assertEqual(rp.semantic_errors(data), [])
        data["roles"]["Reviewer"]["preferences"] = ["missing"]
        self.assertTrue(rp.semantic_errors(data))
        data["roles"]["Reviewer"]["preferences"] = ["one", "one"]
        self.assertTrue(rp.semantic_errors(data))

    def test_hard_gate_cap_is_split_per_role_not_shared(self) -> None:
        """Reviewer 要审整批产物可放宽到 3600s；Maintainer 只跑快速集成验收，长挂只拖住交付。"""
        self.assertEqual(rp.hard_role_max_timeout("Reviewer"), 3600)
        self.assertEqual(rp.hard_role_max_timeout("Maintainer"), 1800)
        self.assertEqual(rp.hard_role_max_timeout("Builder"), rp.MAX_TIMEOUT_S)

        def build(role):
            return {"roles": {role: {"duty": role, "preferences": ["one"], "timeout_s": 3600}},
                    "command_profiles": {"one": {"agent": "omp", "model": "provider/name"}}}

        self.assertEqual(rp.semantic_errors(build("Reviewer")), [])
        self.assertTrue(rp.semantic_errors(build("Maintainer")))

    def test_usage_flags_shared_with_probe(self) -> None:
        """USAGE_FLAGS 包含四个支持 CLI 且与 _probe_argv 共享旗标前缀。"""
        self.assertEqual(
            rp.USAGE_FLAGS,
            {
                "claude": ["--output-format", "json"],
                "codex": ["--json"],
                "omp": ["--mode", "json"],
                "pi": ["--mode", "json"],
            },
        )
        for agent, flags in rp.USAGE_FLAGS.items():
            probe_cmd = rp._probe_argv({"agent": agent, "model": "test-model"}, "hello")
            # probe_cmd 必须包含 USAGE_FLAGS[agent]
            flag_len = len(flags)
            found = any(probe_cmd[i:i + flag_len] == flags for i in range(len(probe_cmd) - flag_len + 1))
            self.assertTrue(found, f"{agent} probe command does not contain USAGE_FLAGS")

    def test_parse_usage_claude_fixture(self) -> None:
        content = (SYSTEM / "tests/fixtures/agent_cli_usage/usage_claude.txt").read_text(encoding="utf-8")
        usage = rp.parse_usage(content, "claude")
        self.assertIsNotNone(usage)
        self.assertEqual(usage["session_id"], "claude-sess-1234")
        self.assertAlmostEqual(usage["cost_usd"], 0.015)
        self.assertIn("claude-3-7-sonnet-20250219", usage["models"])
        m = usage["models"]["claude-3-7-sonnet-20250219"]
        self.assertEqual(m["in"], 100)
        self.assertEqual(m["out"], 50)
        self.assertEqual(m["cache_read"], 200)
        self.assertEqual(m["cache_write"], 30)

    def test_parse_usage_codex_fixture(self) -> None:
        content = (SYSTEM / "tests/fixtures/agent_cli_usage/usage_codex.txt").read_text(encoding="utf-8")
        usage = rp.parse_usage(content, "codex")
        self.assertIsNotNone(usage)
        self.assertEqual(usage["session_id"], "codex-thread-5678")
        self.assertIsNone(usage["cost_usd"])
        # codex 累加: turn1 (in=120-100=20, out=40, cr=100, cw=0), turn2 (in=80-50=30, out=25, cr=50, cw=0)
        # 总计: in=50, out=65, cache_read=150, cache_write=0
        self.assertIn("", usage["models"])
        m = usage["models"][""]
        self.assertEqual(m["in"], 50)
        self.assertEqual(m["out"], 65)
        self.assertEqual(m["cache_read"], 150)
        self.assertEqual(m["cache_write"], 0)

    def test_parse_usage_omp_fixture(self) -> None:
        content = (SYSTEM / "tests/fixtures/agent_cli_usage/usage_omp.txt").read_text(encoding="utf-8")
        usage = rp.parse_usage(content, "omp")
        self.assertIsNotNone(usage)
        self.assertEqual(usage["session_id"], "omp-sess-9999")
        self.assertAlmostEqual(usage["cost_usd"], 0.023)
        self.assertIn("claude-3-7-sonnet", usage["models"])
        self.assertIn("gpt-4o", usage["models"])
        m_claude = usage["models"]["claude-3-7-sonnet"]
        self.assertEqual(m_claude["in"], 150)
        self.assertEqual(m_claude["out"], 75)
        self.assertEqual(m_claude["cache_read"], 400)
        self.assertEqual(m_claude["cache_write"], 30)
        m_gpt = usage["models"]["gpt-4o"]
        self.assertEqual(m_gpt["in"], 80)
        self.assertEqual(m_gpt["out"], 40)
        self.assertEqual(m_gpt["cache_read"], 0)
        self.assertEqual(m_gpt["cache_write"], 0)

    def test_parse_usage_garbage_empty_is_error_none(self) -> None:
        self.assertIsNone(rp.parse_usage("", "claude"))
        self.assertIsNone(rp.parse_usage("   \n\t  ", "claude"))
        self.assertIsNone(rp.parse_usage("not valid json at all", "claude"))
        self.assertIsNone(rp.parse_usage("not valid json at all", "codex"))
        self.assertIsNone(rp.parse_usage("not valid json at all", "omp"))
        self.assertIsNone(rp.parse_usage('{"is_error": true}', "claude"))
        self.assertIsNone(rp.parse_usage('{"type": "error", "error": "rate limit"}', "claude"))
        self.assertIsNone(rp.parse_usage('{"type": "turn.failed", "error": "bad request"}', "codex"))
        self.assertIsNone(rp.parse_usage('{"unsupported": 1}', "unsupported_agent"))


class ProcessTerminationPlatformTests(unittest.TestCase):
    """探针进程终止按 os.name 分支：posix（darwin/linux）杀整个进程组防管道孤儿；
    nt（win32）无进程组，走 taskkill /T /F 连子进程一起杀，taskkill 缺失再退 kill()。"""

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_posix_terminate_kills_process_group(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        try:
            rp._terminate_probe_process(proc)
            self.assertIsNotNone(proc.poll(), "进程组应被 SIGKILL 整组回收")
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_windows_terminate_uses_taskkill_tree(self):
        argv_seen = []

        def fake_run(argv, **kwargs):
            argv_seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)

        process = mock.Mock(pid=4321)
        with mock.patch("os.name", "nt"), mock.patch.object(rp.subprocess, "run", fake_run):
            rp._terminate_probe_process(process)
        self.assertEqual(argv_seen, [["taskkill", "/PID", "4321", "/T", "/F"]])
        process.kill.assert_not_called()
        process.communicate.assert_called_once()

    def test_windows_taskkill_missing_falls_back_to_kill(self):
        def missing_run(argv, **kwargs):
            raise FileNotFoundError("taskkill 不存在")

        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        with mock.patch("os.name", "nt"), mock.patch.object(rp.subprocess, "run", missing_run):
            rp._terminate_probe_process(process)
        process.kill.assert_called_once()
        process.communicate.assert_called_once()
