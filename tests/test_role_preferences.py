"""Consumer-visible preference order, authorization and isolated invocation tests."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import subprocess
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
import json, os, re, sys, time
from pathlib import Path
args = sys.argv[1:]
if "--help" in args:
    print("--model -p --mode --no-session --no-tools --no-extensions --no-skills --no-rules --no-title --no-prewalk")
    sys.exit(0)
model = args[args.index("--model") + 1]
prompt = args[args.index("-p") + 1]
with open(os.environ["PROBE_LOG"], "a") as stream:
    stream.write(json.dumps({{"model": model, "cwd": os.getcwd(), "stdin": sys.stdin.read(), "args": args}}) + "\\n")
if model.endswith("fail"):
    sys.exit(4)
if model.endswith("slow"):
    time.sleep(2)
if model.endswith("echo"):
    print(json.dumps({{"type": "message_end", "message": {{"role": "user", "content": [{{"type": "text", "text": prompt}}]}}}}))
else:
    nonce = re.search(r"ENTROPAXIS_[a-f0-9]+", prompt).group()
    print(json.dumps({{"type": "message_end", "message": {{"role": "assistant", "content": [{{"type": "text", "text": nonce}}], "stopReason": "stop"}}}}))
''', encoding="utf-8")
        executable.chmod(0o755)
        env = mock.patch.dict(os.environ, {"PATH": str(self.root), "PROBE_LOG": str(self.log)})
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
        with mock.patch.object(rp.subprocess, "run", side_effect=AssertionError("unauthorized process")):
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
        self.assertEqual([item["model"] for item in self.calls()], ["provider/fail", "provider/echo", "provider/ok"])
        for call in self.calls():
            self.assertNotEqual(call["cwd"], str(self.root))
            self.assertEqual(call["stdin"], "")
            self.assertIn("--no-tools", call["args"])
            self.assertFalse(any("dangerously" in arg for arg in call["args"]))
            self.assertFalse(Path(call["cwd"]).exists())

    def test_model_prefix_and_case_survive_real_subprocess(self):
        data = self.config(["google-antigravity/Gemini-Exact:high"])
        rp.select_preferences(data, authorized=True, authorization_event="explicit permission")
        self.assertEqual(self.calls()[0]["model"], "google-antigravity/Gemini-Exact:high")

    def test_local_phase_never_selects_or_calls_model(self):
        data = self.config(["provider/ok"])
        before = copy.deepcopy(data)
        report = rp.local_candidates(data)[0]
        self.assertEqual(report["preferences"][0]["status"], "local_ready")
        self.assertEqual(data, before)
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
        self.assertEqual([item["model"] for item in self.calls()], ["provider/fail", "provider/ok"])


class PreferenceContractTests(unittest.TestCase):
    def test_migration_preserves_long_order_ids_notes_timeouts_and_verifier(self):
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
        for i in range(5):
            self.assertEqual(profiles[f"p-{i}"], {"agent": "omp", "model": f"provider/model-{i}", "timeout_s": 800 + i, "note": f"user note {i}"})
        migrated = copy.deepcopy(data)
        rp.migrate_config(data)
        self.assertEqual(data, migrated)

    def test_schema_rejects_dual_profiles_unknown_agent_and_timeout_overflow(self):
        schema = vs.load_schema("command_profile")
        for profile in ({"agent": "omp", "model": "provider/name", "argv": ["omp", rp.PROMPT], "timeout_s": 30},
                        {"agent": "unknown", "model": "provider/name", "timeout_s": 30},
                        {"agent": "omp", "model": "provider/name", "timeout_s": 7201},
                        {"agent": "omp", "timeout_s": 30},
                        {"agent": "omp", "model": "provider/name", "fallback_profile": "other", "timeout_s": 30}):
            with self.subTest(profile=profile):
                self.assertTrue(vs.validate({"pair": profile}, schema))
        self.assertEqual(vs.validate({"pair": {"agent": "pi", "model": "provider/name", "timeout_s": 30}}, schema), [])

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
