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

    def config(self, models, selected=None):
        names = [f"choice-{i}" for i in range(1, len(models) + 1)]
        return {"roles": {"Researcher": {"duty": "research", "preferences": names, "profile": selected}},
                "command_profiles": {n: {"agent": "omp", "model": m, "timeout_s": 30} for n, m in zip(names, models)}}

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_missing_authorization_never_launches_and_preserves_selection(self):
        data = self.config(["provider/ok"], "choice-1")
        before = copy.deepcopy(data)
        with mock.patch.object(rp.subprocess, "Popen", side_effect=AssertionError("unauthorized process")):
            for kwargs in ({}, {"authorized": True}, {"authorization_event": "user consent"}):
                result = rp.select_preferences(data, **kwargs)
                self.assertEqual(result[0]["outcome"], "authorization_required")
        self.assertEqual(data, before)

    def test_first_actual_response_wins_not_exit_code_or_prompt_echo(self):
        data = self.config(["provider/fail", "provider/echo", "provider/ok", "provider/unused"])
        report = rp.select_preferences(data, authorized=True, authorization_event="user said yes")[0]
        self.assertEqual((report["outcome"], report["selected_preference"], data["roles"]["Researcher"]["profile"]),
                         ("selected", 3, "choice-3"))
        self.assertEqual([item["status"] for item in report["attempts"]], ["call_failed", "invalid_response", "available"])
        self.assertEqual(len(self.calls()), 3)
        for call in self.calls():
            self.assertNotEqual(call["cwd"], str(self.root))
            self.assertEqual(call["stdin"], "")
            self.assertFalse(Path(call["cwd"]).exists())

    def test_protocol_identity_requires_exact_provider_and_case(self):
        data = self.config(["google-antigravity/Gemini-Exact:high"])
        report = rp.select_preferences(data, authorized=True, authorization_event="explicit permission")[0]
        self.assertEqual((report["outcome"], report["attempts"][0]["status"]), ("selected", "available"))

        data = self.config(
            [
                "provider/missing",
                "provider/mismatch",
                "provider/provider-mismatch",
                "google-antigravity/Gemini-Exact:case-mismatch",
            ],
            "choice-1",
        )
        report = rp.select_preferences(data, authorized=True, authorization_event="explicit permission")[0]
        self.assertEqual(
            [attempt["status"] for attempt in report["attempts"]],
            ["identity_missing", "identity_mismatch", "identity_mismatch", "identity_mismatch"],
        )
        self.assertEqual((report["outcome"], data["roles"]["Researcher"]["profile"]), ("no_available_preference", "choice-1"))

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
        data = self.config(["requested-alias"], "choice-1")
        data["command_profiles"]["choice-1"]["agent"] = "claude"
        report = rp.select_preferences(data, authorized=True, authorization_event="fixture consent")[0]
        self.assertEqual(report["attempts"][0]["status"], "identity_mismatch")
        self.assertEqual(data["roles"]["Researcher"]["profile"], "choice-1")
        self.assertEqual(report["outcome"], "no_available_preference")

    def test_unisolated_adapters_do_not_launch_or_change_selection(self):
        for agent in ("agy", "opencode", "mimo"):
            executable = self.root / agent
            executable.write_bytes((self.root / "omp").read_bytes())
            executable.chmod(0o755)
            data = self.config(["provider/ok"], "choice-1")
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
        self.assertEqual((result["attempts"][0]["status"], result["selected_preference"]), ("timeout", 2))
        for selected in (None, "choice-1"):
            data = self.config(["provider/fail"], selected)
            result = rp.select_preferences(data, authorized=True, authorization_event="yes")[0]
            self.assertEqual(result["outcome"], "no_available_preference")
            self.assertEqual(data["roles"]["Researcher"]["profile"], selected)


    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_reaps_descendant_process_group(self):
        child_pid = self.root / "child.pid"
        for model in ("provider/descendant", "provider/closed-descendant"):
            child_pid.unlink(missing_ok=True)
            data = self.config([model])
            with mock.patch.dict(os.environ, {"PROBE_CHILD_PID": str(child_pid)}):
                report = rp.select_preferences(data, authorized=True, authorization_event="yes", timeout=1)[0]
            self.assertEqual(report["attempts"][0]["status"], "timeout")
            self.assertTrue(child_pid.exists())
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
        data["roles"]["Reporter"] = {"duty": "report", "preferences": ["choice-1"], "profile": None}
        result = rp.select_preferences(data, authorized=True, authorization_event="yes")
        self.assertEqual([row["selected_preference"] for row in result], [1, 1])
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
        config = {"roles": {"Designer": {"preferences": names, "profile": names[0]}}, "command_profiles": profiles}
        source, chain = dr._tier3_default_chain("Designer", config)
        with mock.patch.object(dr, "TRACE_LOG", self.root / "trace.jsonl"):
            failure, attempts, ok = dr.execute_chain(chain, "multiline\nquoted ; prompt", self.root, out)
        self.assertTrue(ok)
        self.assertEqual(failure, "")
        self.assertEqual([item["preference_number"] for item in attempts], [1, 2, 3, 4])
        self.assertEqual(attempts[-1]["profile"], names[3])
        self.assertEqual(out.read_text(), "# Changed output\n")
        config["roles"]["Designer"]["profile"] = names[2]
        self.assertEqual([p["preference_number"] for p in dr._tier3_default_chain("Designer", config)[1]], [3, 4])
        config["roles"]["Designer"]["profile"] = None
        self.assertEqual(dr._tier3_default_chain("Designer", config), ("SUBAGENT_AUTO", []))

    def test_cli_authorization_and_dry_run_preserve_disk_then_select_actual_fallback(self):
        config = self.root / "roles.yaml"
        config.write_text(yaml.safe_dump(self.config(["provider/fail", "provider/ok"])))
        before = config.read_bytes()
        command = [sys.executable, str(SYSTEM / "tools" / "setup_agents.py"), "--config", str(config), "--json"]
        unauthorized = subprocess.run(command + ["--check-preferences"], capture_output=True, text=True, timeout=15)
        self.assertEqual(unauthorized.returncode, 1, unauthorized.stderr)
        self.assertEqual(json.loads(unauthorized.stdout)[0]["outcome"], "authorization_required")
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
        self.assertEqual(json.loads(authorized.stdout)[0]["selected_preference"], 2)
        saved = yaml.safe_load(config.read_text())
        self.assertEqual(saved["roles"]["Researcher"]["profile"], "choice-2")
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
        self.assertEqual(data["roles"]["Builder"]["profile"], "p-0")
        self.assertEqual(profiles["verify"], original_verify)
        # note 已从契约删除（零读者字段）：迁移时剥掉，否则旧实例一跑写入命令就把
        # 已删字段带回 schema 校验。timeout_s 与 profile ID 一律保留。
        for i in range(5):
            self.assertEqual(profiles[f"p-{i}"], {"agent": "omp", "model": f"provider/model-{i}", "timeout_s": 800 + i})
        migrated = copy.deepcopy(data)
        rp.migrate_config(data)
        self.assertEqual(data, migrated)

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
                "roles": {"Builder": {"duty": "b", "preferences": ["shared"], "profile": "shared", "timeout_s": 3600},
                          "Reviewer": {"duty": "r", "preferences": ["shared"], "profile": "shared", "timeout_s": 1800}},
                "command_profiles": {"shared": {"agent": "omp", "model": "provider/name"}}}
        profile = data["command_profiles"]["shared"]
        self.assertEqual(rp.effective_timeout(data, profile, "Builder"), 3600)
        self.assertEqual(rp.effective_timeout(data, profile, "Reviewer"), 1800)
        # profile 覆盖优先于角色级
        profile["timeout_s"] = 60
        self.assertEqual(rp.effective_timeout(data, profile, "Builder"), 60)
        del profile["timeout_s"]
        # 未声明角色级时落到 default_timeout_s
        data["roles"]["Reporter"] = {"duty": "x", "preferences": ["shared"], "profile": "shared"}
        self.assertEqual(rp.effective_timeout(data, profile, "Reporter"), 900)
        del data["default_timeout_s"]
        self.assertEqual(rp.effective_timeout(data, profile, "Reporter"), rp.FALLBACK_TIMEOUT_S)

    def test_hard_gate_timeout_rejected_at_role_and_profile_level(self):
        def build(role_timeout=None, profile_timeout=None):
            profile = {"agent": "omp", "model": "provider/name"}
            if profile_timeout is not None:
                profile["timeout_s"] = profile_timeout
            role = {"duty": "review", "preferences": ["one"], "profile": "one"}
            if role_timeout is not None:
                role["timeout_s"] = role_timeout
            return {"roles": {"Reviewer": role}, "command_profiles": {"one": profile}}

        for label, data in (("role-level", build(role_timeout=1801)),
                            ("profile override", build(profile_timeout=1801)),
                            ("default overflow", {"default_timeout_s": 7201, "roles": {"Reviewer": {"duty": "r", "preferences": ["one"], "profile": "one"}},
                                                  "command_profiles": {"one": {"agent": "omp", "model": "provider/name"}}})):
            with self.subTest(level=label):
                self.assertTrue(rp.semantic_errors(data))
        # 硬门禁上限本身合法；非硬门禁角色不受 1800 限制
        self.assertEqual(rp.semantic_errors(build(role_timeout=1800)), [])
        self.assertEqual(rp.semantic_errors(build(profile_timeout=1800)), [])
        self.assertEqual(rp.semantic_errors(build(role_timeout=3600) | {"roles": {"Builder": build(role_timeout=3600)["roles"]["Reviewer"]}}), [])

    def test_invalid_selection_duplicate_reference_and_hard_timeout_rejected(self):
        data = {"roles": {"Reviewer": {"duty": "review", "preferences": ["one"], "profile": "one"}},
                "command_profiles": {"one": {"agent": "omp", "model": "provider/name", "timeout_s": 1801}}}
        self.assertTrue(rp.semantic_errors(data))
        data["command_profiles"]["one"]["timeout_s"] = 1800
        self.assertEqual(rp.semantic_errors(data), [])
        data["roles"]["Reviewer"]["profile"] = "missing"
        self.assertTrue(rp.semantic_errors(data))
        data["roles"]["Reviewer"].update(profile="one", preferences=["one", "one"])
        self.assertTrue(rp.semantic_errors(data))
