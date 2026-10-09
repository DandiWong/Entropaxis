import contextlib
import json
import io
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

import yaml

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools import role_preferences
from tools import setup_agents as sa


class SetupAgentsTests(TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-setup-test-"))
        self.config = self.tmpdir / "roles.yaml"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def data(self) -> dict:
        data = sa.load_config(self.config)
        role_preferences.migrate_config(data)
        return data

    def invoke(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["setup_agents.py", *args]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = sa.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_preference_crud_has_unbounded_order_and_preserves_position_metadata(self) -> None:
        data = self.data()
        for number in range(1, 6):
            sa.set_preference(data, "Builder", number, "omp", f"provider/model-{number}")
        second = data["roles"]["Builder"]["preferences"][1]
        data["command_profiles"][second]["timeout_s"] = 123

        sa.set_preference(data, "Builder", 2, "codex", "provider/replaced")
        sa.move_preference(data, "Builder", 5, 1)
        removed = sa.remove_preference(data, "Builder", 4)

        preferences = data["roles"]["Builder"]["preferences"]
        self.assertEqual(len(preferences), 4)
        self.assertNotIn(removed, preferences)
        self.assertEqual(data["command_profiles"][second]["agent"], "codex")
        self.assertEqual(data["command_profiles"][second]["model"], "provider/replaced")
        self.assertEqual(data["command_profiles"][second]["timeout_s"], 123)

    def test_cli_set_preference_appends_agent_model_record_with_json_only_output(self) -> None:
        data = self.data()
        sa.save_config(self.config, data)

        code, stdout, err = self.invoke(
            "--config", str(self.config), "--set-preference", "Builder", "3", "pi", "provider/model", "--json"
        )

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(stdout)["changed"], ["Builder"])
        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        name = saved["roles"]["Builder"]["preferences"][2]
        self.assertEqual(saved["command_profiles"][name]["agent"], "pi")
        self.assertEqual(saved["command_profiles"][name]["model"], "provider/model")

    def test_cli_bad_preference_number_is_actionable_without_traceback(self) -> None:
        data = self.data()
        sa.save_config(self.config, data)

        code, _, err = self.invoke("--config", str(self.config), "--set-preference", "Builder", "bad", "omp", "model")

        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("invalid literal", err)

    def test_cli_set_timeout_defaults_to_role_level_and_preference_overrides(self) -> None:
        """角色级超时对该角色全部偏好生效；--preference 才落到单条 profile。"""
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/one")
        sa.set_preference(data, "Builder", 2, "codex", "provider/two")
        first, second = data["roles"]["Builder"]["preferences"]
        data["roles"]["Builder"]["profile"] = None
        sa.save_config(self.config, data)

        code, _, err = self.invoke("--config", str(self.config), "--set-timeout", "Builder", "1200")
        self.assertEqual(code, 0, err)
        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["Builder"]["timeout_s"], 1200)
        self.assertNotIn("timeout_s", saved["command_profiles"][first])
        self.assertNotIn("timeout_s", saved["command_profiles"][second])
        self.assertEqual(role_preferences.effective_timeout(saved, saved["command_profiles"][second], "Builder"), 1200)

        code, _, err = self.invoke("--config", str(self.config), "--set-timeout", "Builder", "300", "--preference", "1")
        self.assertEqual(code, 0, err)
        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["command_profiles"][first]["timeout_s"], 300)
        self.assertEqual(saved["roles"]["Builder"]["timeout_s"], 1200)
        self.assertEqual(role_preferences.effective_timeout(saved, saved["command_profiles"][second], "Builder"), 1200)

    def test_cli_rejects_hard_gate_timeout_above_limit_without_writing(self) -> None:
        data = self.data()
        sa.set_preference(data, "Reviewer", 1, "omp", "provider/one")
        sa.save_config(self.config, data)
        before = self.config.read_bytes()

        code, _, err = self.invoke("--config", str(self.config), "--set-timeout", "Reviewer", "1801")

        self.assertEqual(code, 1)
        self.assertIn("1800", err)
        self.assertEqual(self.config.read_bytes(), before)

    def test_cli_targets_requested_preference_without_rebuilding_others(self) -> None:
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/one")
        sa.set_preference(data, "Builder", 2, "codex", "provider/two")
        first, second = data["roles"]["Builder"]["preferences"]
        data["roles"]["Builder"]["profile"] = None
        sa.save_config(self.config, data)

        code, _, err = self.invoke("--config", str(self.config), "--set-model", "Builder", "provider/two-new", "--preference", "2")
        self.assertEqual(code, 0, err)
        code, _, err = self.invoke("--config", str(self.config), "--set-timeout", "Builder", "77", "--preference", "2")
        self.assertEqual(code, 0, err)

        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["command_profiles"][first]["model"], "provider/one")
        self.assertEqual(saved["command_profiles"][second]["model"], "provider/two-new")
        self.assertEqual(saved["command_profiles"][second]["timeout_s"], 77)
        self.assertEqual(saved["roles"]["Builder"]["profile"], None)

    def test_editing_shared_profile_copies_before_mutating(self) -> None:
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/original")
        shared = data["roles"]["Builder"]["preferences"][0]
        data["roles"]["Reviewer"]["preferences"] = [shared]
        data["roles"]["Reviewer"]["profile"] = shared

        sa.set_model(data, "Reviewer", "provider/reviewer", preference=1)

        reviewer = data["roles"]["Reviewer"]["preferences"][0]
        self.assertNotEqual(reviewer, shared)
        self.assertEqual(data["command_profiles"][shared]["model"], "provider/original")
        self.assertEqual(data["command_profiles"][reviewer]["model"], "provider/reviewer")

    def test_list_exposes_full_sequence_without_choosing_unselected_preference(self) -> None:
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/one")
        sa.set_preference(data, "Builder", 2, "codex", "provider/two")
        data["roles"]["Builder"]["profile"] = None

        item = next(item for item in sa.list_roles(data) if item["role"] == "Builder")

        self.assertEqual([preference["number"] for preference in item["preferences"]], [1, 2])
        self.assertIsNone(item["selected_preference"])
        self.assertNotIn("profile", item)

    def test_save_rejects_invalid_preference_reference_without_writing(self) -> None:
        data = self.data()
        data["roles"]["Builder"]["preferences"] = ["missing"]
        data["roles"]["Builder"]["profile"] = "missing"

        with self.assertRaises(sa.ConfigError):
            sa.save_config(self.config, data)

        self.assertFalse(self.config.exists())

    def test_set_role_keeps_raw_custom_argv_but_uses_preferences_not_fallbacks(self) -> None:
        data = self.data()
        sa.set_role(data, "Builder", "custom", "custom-cli --task {PROMPT} || other-cli {PROMPT}")

        names = data["roles"]["Builder"]["preferences"]
        self.assertEqual(len(names), 2)
        self.assertEqual(data["roles"]["Builder"]["profile"], names[0])
        self.assertEqual(data["command_profiles"][names[0]]["argv"], ["custom-cli", "--task", "{PROMPT}"])
        self.assertNotIn("fallback_profile", data["command_profiles"][names[0]])

    def test_legacy_load_is_read_only_until_explicit_migration(self) -> None:
        legacy = {
            "default_dispatch_mode": "strict",
            "roles": {"Builder": {"duty": "build", "profile": "builder-primary"}},
            "command_profiles": {
                "builder-primary": {"argv": ["custom", "{PROMPT}"], "timeout_s": 9, "fallback_profile": "builder-fallback"},
                "builder-fallback": {"argv": ["other", "{PROMPT}"], "timeout_s": 9},
            },
            "dispatch_authorizations": [],
        }
        self.config.write_text(yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8")
        before = self.config.read_bytes()

        code, _, err = self.invoke("--config", str(self.config), "--list")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config.read_bytes(), before)

        code, _, err = self.invoke("--config", str(self.config), "--migrate-preferences")
        self.assertEqual(code, 0, err)
        migrated = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(migrated["roles"]["Builder"]["preferences"], ["builder-primary", "builder-fallback"])
        self.assertNotIn("fallback_profile", migrated["command_profiles"]["builder-primary"])

    def test_remove_preference_is_dry_run_until_confirmed(self) -> None:
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/one")
        sa.set_preference(data, "Builder", 2, "codex", "provider/two")
        sa.save_config(self.config, data)
        before = self.config.read_bytes()

        code, _, err = self.invoke("--config", str(self.config), "--remove-preference", "Builder", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config.read_bytes(), before)

        code, _, err = self.invoke("--config", str(self.config), "--remove-preference", "Builder", "2", "--yes")
        self.assertEqual(code, 0, err)
        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["roles"]["Builder"]["preferences"]), 1)

    def test_unauthorized_check_never_probes_or_rewrites_file(self) -> None:
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/one")
        sa.save_config(self.config, data)
        before = self.config.read_bytes()

        with mock.patch("tools.role_preferences.subprocess.run") as model_run:
            code, stdout, err = self.invoke("--config", str(self.config), "--check-preferences", "--json")

        self.assertEqual(code, 1, err)
        self.assertIn("authorization_required", stdout)
        self.assertEqual(model_run.call_count, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_authorized_check_reports_successful_ordinal_and_persists_selection(self) -> None:
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/one")
        sa.set_preference(data, "Builder", 2, "codex", "provider/two")
        data["roles"]["Builder"]["profile"] = None
        sa.save_config(self.config, data)
        names = data["roles"]["Builder"]["preferences"]
        local = [{
            "role": "Builder", "selected_profile": None, "selected_preference": None,
            "preferences": [
                {"number": 1, "profile": names[0], "status": "local_ready"},
                {"number": 2, "profile": names[1], "status": "local_ready"},
            ],
        }]

        with mock.patch("tools.role_preferences.local_candidates", return_value=local), mock.patch(
            "tools.role_preferences._live_probe", side_effect=["call_failed", "available"]
        ):
            code, stdout, err = self.invoke(
                "--config", str(self.config), "--check-preferences", "--authorize-model-check",
                "--authorization-event", "user-confirmed-123", "--json",
            )

        self.assertEqual(code, 0, err)
        self.assertIn('"selected_preference":2', stdout)
        saved = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["Builder"]["profile"], names[1])

    def test_verify_reports_only_local_executable_not_model_ready(self) -> None:
        data = self.data()
        sa.set_preference(data, "Builder", 1, "omp", "provider/one")
        data["roles"]["Builder"]["profile"] = data["roles"]["Builder"]["preferences"][0]
        sa.save_config(self.config, data)

        with mock.patch("tools.setup_agents.shutil.which", return_value="/usr/bin/omp"):
            reports = sa.verify_roles(self.config)

        builder = next(report for report in reports if report["role"] == "Builder")
        self.assertEqual(builder["status"], "local_executable")

    def test_scan_never_enumerates_model_catalogs(self) -> None:
        with mock.patch("tools.setup_agents.subprocess.run", side_effect=AssertionError("no installed CLI means no subprocess")), mock.patch(
            "tools.setup_agents.shutil.which", return_value=None
        ):
            detected = sa.detect_installed_agents()

        self.assertTrue(any(item["id"] == "subagent" for item in detected))

    def test_cli_rejects_malformed_and_non_object_config_without_writing(self) -> None:
        for content in ("roles: []\ncommand_profiles: {}\n", "roles: {}\ncommand_profiles: []\n", "roles: [\n"):
            self.config.write_text(content, encoding="utf-8")
            before = self.config.read_bytes()

            code, _, err = self.invoke("--config", str(self.config), "--verify")

            self.assertNotEqual(code, 0)
            self.assertNotIn("Traceback", err)
            self.assertEqual(self.config.read_bytes(), before)
