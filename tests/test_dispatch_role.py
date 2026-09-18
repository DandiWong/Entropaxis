"""dispatch_role.py 表驱动测试。

覆盖面（方案 §3.3 / 审计 C-04、M-02、M-05、R2-02 闭合证据）:
  - 决策矩阵全行 × 失败码（strict/graceful × hard/soft × 6 码）
  - graceful 授权契约（有效/过期/越范围含硬角色/缺确认事件 → 一律收紧 strict）
  - manifest 校验（mode 单向收紧、五态无 waived、指纹、CAS 跨字段语义）
  - CAS 并发（互斥、revision 必增、过期锁恢复、journal 追加）
  - run 全链（成功/六类失败/指纹失配回拒）
  - migrate_config 保守迁移（拒迁含引号、幂等不覆盖、dry-run 不写盘）
  - 三样本解析确定性（capsule / workers.yaml / 裸目录）
  - 与 schemas/roles_manifest.schema.json 的样本一致性（jsonschema 可用时）
"""

import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

import yaml

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools import dispatch_role as dr  # noqa: E402

OK_SH = '#!/bin/sh\necho "# 报告" > "$1"\necho body >> "$1"\n'
FAIL_SH = '#!/bin/sh\nexit 1\n'
SLOW_SH = '#!/bin/sh\nsleep 5\n'


def _mkexe(dir_: Path, name: str, body: str) -> str:
    p = dir_ / name
    p.write_text(body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


GOOD_MANIFEST = {
    "assigned_at": "2026-09-18T00:00:00+00:00",
    "assigned_by": "Test",
    "revision": 1,
    "assignments": [{
        "assignment_id": "r1", "role": "Reviewer", "command_profile": "test-ok",
        "target_path": "01.md", "target_sha256": "a" * 64,
        "deliverable": "05.md", "status": "pending",
    }],
}


class DispatchRoleTestBase(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.scripts = self.ws / "bin"
        self.scripts.mkdir()
        self.ok = _mkexe(self.scripts, "ok.sh", OK_SH)
        self.fail = _mkexe(self.scripts, "fail.sh", FAIL_SH)
        self.slow = _mkexe(self.scripts, "slow.sh", SLOW_SH)
        self.config = self.ws / "workspace-config.md"
        self._old_active = dr.ACTIVE_CONFIG
        dr.ACTIVE_CONFIG = self.config

    def tearDown(self) -> None:
        dr.ACTIVE_CONFIG = self._old_active
        self._tmp.cleanup()

    # ---- 脚手架 ----------------------------------------------------------

    def write_config(self, profiles: dict, mode: str | None = "strict", grants: list | None = None) -> None:
        block = {"command_profiles": profiles}
        if mode is not None:
            block["default_dispatch_mode"] = mode
        if grants is not None:
            block["dispatch_authorizations"] = grants
        text = "# test config\n\n```yaml\n" + yaml.safe_dump(block, allow_unicode=True, sort_keys=False) + "```\n"
        self.config.write_text(text, encoding="utf-8")

    def write_workers(self, role: str = "Reviewer", profile: str = "test-ok", target_sha: str | None = None) -> Path:
        target = self.ws / "01.md"
        target.write_text("# 目标方案\n", encoding="utf-8")
        man = {
            "assigned_at": "2026-09-18T00:00:00+00:00", "assigned_by": "Test", "revision": 1,
            "assignments": [{
                "assignment_id": "r1", "role": role, "command_profile": profile,
                "target_path": "01.md", "target_sha256": target_sha or dr._sha256_file(target),
                "deliverable": "05.md", "status": "pending",
            }],
        }
        workers = self.ws / "workers.yaml"
        workers.write_text(yaml.safe_dump({"roles_manifest": man}, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return workers


class DecisionMatrixTests(DispatchRoleTestBase):
    """矩阵唯一裁决（R2-02/M-02/M-06 闭合）。"""

    def test_hard_blocked_in_both_modes(self) -> None:
        for mode in ("strict", "graceful"):
            for code in dr.FAILURE_CODES:
                v = dr.adjudicate(mode, "hard", code)
                self.assertEqual(v["outcome"], "blocked", f"{mode}×hard×{code}")
                self.assertEqual(v["exit"], dr.EXIT_BLOCKED)

    def test_soft_strict_unconfigured_no_review_flag(self) -> None:
        for code in ("UNCONFIGURED", "SUBAGENT_AUTO"):
            v = dr.adjudicate("strict", "soft", code)
            self.assertEqual(v["outcome"], "local_carry")
            self.assertFalse(v["needs_external_review"])

    def test_soft_strict_real_failures_flagged_for_review(self) -> None:
        for code in ("NOT_EXECUTABLE", "TIMEOUT", "NO_VALID_OUTPUT", "CHAIN_EXHAUSTED"):
            v = dr.adjudicate("strict", "soft", code)
            self.assertEqual(v["outcome"], "local_carry")
            self.assertTrue(v["needs_external_review"], code)

    def test_soft_graceful_local_plain(self) -> None:
        for code in dr.FAILURE_CODES:
            v = dr.adjudicate("graceful", "soft", code)
            self.assertEqual(v["outcome"], "local_carry")
            self.assertFalse(v["needs_external_review"])


class GracefulAuthTests(DispatchRoleTestBase):
    """graceful 授权契约（C-01 闭合）：一切无效路径都收紧 strict。"""

    VALID = [{"id": "ga-1", "granted_by": "user", "confirm_event": "会话决策",
              "scope": {"roles": ["Builder"], "until": "2099-01-01"}}]

    def test_valid_grant_soft_role(self) -> None:
        self.write_config({}, mode="graceful", grants=self.VALID)
        cfg = dr.load_dispatch_config()
        ok, why = dr.graceful_grant_valid(cfg, "Builder")
        self.assertTrue(ok, why)

    def test_expired_grant_rejected(self) -> None:
        grants = [{"id": "ga-2", "granted_by": "u", "confirm_event": "e", "scope": {"roles": ["Builder"], "until": "2020-01-01"}}]
        ok, _ = dr.graceful_grant_valid({"dispatch_authorizations": grants}, "Builder")
        self.assertFalse(ok)

    def test_hard_role_in_scope_invalidates_whole_grant(self) -> None:
        grants = [{"id": "ga-3", "granted_by": "u", "confirm_event": "e",
                   "scope": {"roles": ["Builder", "Reviewer"], "until": "2099-01-01"}}]
        ok, _ = dr.graceful_grant_valid({"dispatch_authorizations": grants}, "Builder")
        self.assertFalse(ok)

    def test_missing_confirm_event_rejected(self) -> None:
        grants = [{"id": "ga-4", "granted_by": "u", "scope": {"roles": ["Builder"]}}]
        ok, _ = dr.graceful_grant_valid({"dispatch_authorizations": grants}, "Builder")
        self.assertFalse(ok)

    def test_role_not_in_scope(self) -> None:
        ok, _ = dr.graceful_grant_valid({"dispatch_authorizations": self.VALID}, "Researcher")
        self.assertFalse(ok)

    def test_resolve_mode_tightens_on_invalid_graceful(self) -> None:
        self.write_config({}, mode="graceful", grants=[])  # 声明 graceful 但无授权
        mode, why = dr.resolve_mode(None, dr.load_dispatch_config(), "Builder")
        self.assertEqual(mode, "strict")
        self.assertIn("收紧", why)

    def test_manifest_strict_overrides_tier3_graceful(self) -> None:
        self.write_config({}, mode="graceful", grants=self.VALID)
        mode, _ = dr.resolve_mode({"mode": "strict"}, dr.load_dispatch_config(), "Builder")
        self.assertEqual(mode, "strict")


class ManifestValidationTests(DispatchRoleTestBase):
    """validate_manifest 与 schemas/roles_manifest.schema.json 同构（N-01/R2-01 闭合）。"""

    def test_good_manifest_passes(self) -> None:
        man = json.loads(json.dumps(GOOD_MANIFEST))
        man["assignments"][0]["status"] = "succeeded"
        man["assignments"][0]["deliverable_sha256"] = "b" * 64
        self.assertEqual(dr.validate_manifest(man), [])

    def test_manifest_graceful_rejected(self) -> None:
        man = {**GOOD_MANIFEST, "mode": "graceful"}
        errs = dr.validate_manifest(man)
        self.assertTrue(any("单向收紧" in e for e in errs))

    def test_waived_status_rejected(self) -> None:
        man = json.loads(json.dumps(GOOD_MANIFEST))
        man["assignments"][0]["status"] = "waived"
        errs = dr.validate_manifest(man)
        self.assertTrue(any("waived" in e for e in errs))

    def test_unknown_field_and_dup_id(self) -> None:
        man = json.loads(json.dumps(GOOD_MANIFEST))
        man["complexity_gate"] = True
        man["assignments"].append(dict(man["assignments"][0]))
        errs = dr.validate_manifest(man)
        self.assertTrue(any("未知字段" in e for e in errs))
        self.assertTrue(any("重复" in e for e in errs))

    def test_succeeded_requires_deliverable_sha(self) -> None:
        man = json.loads(json.dumps(GOOD_MANIFEST))
        man["assignments"][0]["status"] = "succeeded"
        errs = dr.validate_manifest(man)
        self.assertTrue(any("deliverable_sha256" in e for e in errs))

    def test_bad_sha_format(self) -> None:
        man = json.loads(json.dumps(GOOD_MANIFEST))
        man["assignments"][0]["target_sha256"] = "xyz"
        self.assertTrue(any("64 位" in e for e in dr.validate_manifest(man)))

    def test_schema_file_agrees_on_samples(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema 不可用，跳过双通道一致性")
        schema = json.loads((dr.SCHEMA_DIR / "roles_manifest.schema.json").read_text(encoding="utf-8"))
        good = json.loads(json.dumps(GOOD_MANIFEST))
        good["assignments"][0].update(status="succeeded", deliverable_sha256="b" * 64)
        jsonschema.validate(good, schema)
        bad_mode = {**GOOD_MANIFEST, "mode": "graceful"}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad_mode, schema)
        jsonschema.validate({**GOOD_MANIFEST, "assignments": []}, schema)  # 空骨架合法（脚手架）
        bad_status = json.loads(json.dumps(GOOD_MANIFEST))
        bad_status["assignments"][0]["status"] = "waived"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad_status, schema)


class CASTests(DispatchRoleTestBase):
    """目录锁 + revision CAS + journal（M-05 闭合，B 收缩：journal 非恢复载体）。"""

    def test_mutual_exclusion(self) -> None:
        w = self.write_workers()
        s1, s2 = dr.ManifestStore(w), dr.ManifestStore(w)
        self.assertTrue(s1.acquire("a"))
        self.assertFalse(s2.acquire("b"))
        s1.release()
        self.assertTrue(s2.acquire("b"))

    def test_stale_lock_recovery(self) -> None:
        w = self.write_workers()
        s = dr.ManifestStore(w)
        self.assertTrue(s.acquire("a"))
        (s.lock_dir / "owner.json").write_text(json.dumps(
            {"owner": "a", "lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}), encoding="utf-8")
        self.assertTrue(s.acquire("b"))  # 过期 → 归档锁目录后接管
        self.assertTrue(s.stale_lock_report())

    def test_cas_requires_revision_bump(self) -> None:
        w = self.write_workers()
        s = dr.ManifestStore(w)
        self.assertTrue(s.acquire("a"))
        with self.assertRaises(RuntimeError):
            s.update(lambda m: None, "a")  # 未 +1 → 拒绝
        s.release()

    def test_update_writes_journal_and_bumps(self) -> None:
        w = self.write_workers()
        s = dr.ManifestStore(w)
        self.assertTrue(s.acquire("a"))

        def mutate(m: dict) -> None:
            m["revision"] = m["revision"] + 1
            m["assignments"][0]["status"] = "running"

        s.update(mutate, "a")
        s.release()
        data = yaml.safe_load(w.read_text(encoding="utf-8"))
        self.assertEqual(data["roles_manifest"]["revision"], 2)
        self.assertEqual(data["roles_manifest"]["assignments"][0]["status"], "running")
        jlines = s.journal.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(jlines), 1)
        self.assertEqual(json.loads(jlines[0])["revision"], 2)


class RunFlowTests(DispatchRoleTestBase):
    """run 全链：成功 + 六类失败 + 指纹失配。"""

    def _run(self, role: str, profile: str, profiles: dict, **kw) -> int:
        self.write_config(profiles, **kw)
        self.write_workers(role=role, profile=profile)
        deliverable = self.ws / "05.md"
        return dr.run_assignment(self.ws, "r1", prompt=str(deliverable), ack=None, owner="test")

    def test_success_hard_role(self) -> None:
        rc = self._run("Reviewer", "p-ok", {"p-ok": {"argv": [self.ok, "{PROMPT}"], "timeout_s": 30}})
        self.assertEqual(rc, dr.EXIT_OK)
        man = yaml.safe_load((self.ws / "workers.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        a = man["assignments"][0]
        self.assertEqual(a["status"], "succeeded")
        self.assertEqual(a["deliverable_sha256"], dr._sha256_file(self.ws / "05.md"))
        self.assertEqual(man["revision"], 2)

    def test_hard_chain_exhausted_blocks(self) -> None:
        rc = self._run("Reviewer", "p-fail", {"p-fail": {"argv": [self.fail, "{PROMPT}"], "timeout_s": 30}})
        self.assertEqual(rc, dr.EXIT_BLOCKED)
        man = yaml.safe_load((self.ws / "workers.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(man["assignments"][0]["status"], "blocked")
        self.assertEqual(man["assignments"][0]["attempts"][-1]["exit_code"], 1)

    def test_soft_chain_exhausted_local_with_review_flag(self) -> None:
        rc = self._run("Builder", "p-fail", {"p-fail": {"argv": [self.fail, "{PROMPT}"], "timeout_s": 30}})
        self.assertEqual(rc, dr.EXIT_LOCAL)
        man = yaml.safe_load((self.ws / "workers.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(man["assignments"][0]["status"], "failed")

    def test_timeout_codes(self) -> None:
        rc = self._run("Reviewer", "p-slow", {"p-slow": {"argv": [self.slow, "{PROMPT}"], "timeout_s": 1}})
        self.assertEqual(rc, dr.EXIT_BLOCKED)
        man = yaml.safe_load((self.ws / "workers.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertTrue(man["assignments"][0]["attempts"][-1].get("timeout"))

    def test_not_executable_missing_binary(self) -> None:
        rc = self._run("Builder", "p-miss", {"p-miss": {"argv": ["no-such-bin-zzz", "{PROMPT}"], "timeout_s": 5}})
        self.assertEqual(rc, dr.EXIT_LOCAL)

    def test_not_executable_missing_prompt_placeholder(self) -> None:
        rc = self._run("Builder", "p-noprompt", {"p-noprompt": {"argv": [self.ok], "timeout_s": 5}})
        self.assertEqual(rc, dr.EXIT_LOCAL)
        man = yaml.safe_load((self.ws / "workers.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(man["assignments"][0]["attempts"][-1]["failure_code"], "NOT_EXECUTABLE")

    def test_unconfigured_unknown_profile_soft_local(self) -> None:
        rc = self._run("Builder", "p-unknown", {"p-other": {"argv": [self.ok, "{PROMPT}"], "timeout_s": 5}})
        self.assertEqual(rc, dr.EXIT_LOCAL)

    def test_unconfigured_hard_blocked(self) -> None:
        rc = self._run("Reviewer", "p-unknown", {"p-other": {"argv": [self.ok, "{PROMPT}"], "timeout_s": 5}})
        self.assertEqual(rc, dr.EXIT_BLOCKED)

    def test_subagent_auto_via_table(self) -> None:
        self.config.write_text(
            "## 角色模态外置 CLI 与模型声明\n\n| 角色模态 | 职责 | 承载 CLI | 启动命令 |\n|---|---|---|---|\n"
            "| Builder | 实施 | subagent | 内置 Subagent 机制 (auto) |\n", encoding="utf-8")
        self.write_workers(role="Builder", profile="p-none")
        rc = dr.run_assignment(self.ws, "r1", prompt="x", ack=None, owner="t")
        self.assertEqual(rc, dr.EXIT_LOCAL)

    def test_no_valid_output_when_zero_exit_but_no_deliverable(self) -> None:
        ok_noop = _mkexe(self.scripts, "noop.sh", "#!/bin/sh\nexit 0\n")
        rc = self._run("Reviewer", "p-noop", {"p-noop": {"argv": [ok_noop, "{PROMPT}"], "timeout_s": 10}})
        self.assertEqual(rc, dr.EXIT_BLOCKED)  # NO_VALID_OUTPUT × hard

    def test_target_fingerprint_mismatch_rejected(self) -> None:
        self.write_config({"p-ok": {"argv": [self.ok, "{PROMPT}"], "timeout_s": 30}})
        self.write_workers(role="Reviewer", profile="p-ok", target_sha="f" * 64)  # 指纹不符
        rc = dr.run_assignment(self.ws, "r1", prompt=str(self.ws / "05.md"), ack=None, owner="t")
        self.assertEqual(rc, dr.EXIT_USAGE)

    def test_fallback_chain_rescues(self) -> None:
        self.write_config({"p-pri": {"argv": [self.fail, "{PROMPT}"], "timeout_s": 10, "fallback_profile": "p-back"},
                           "p-back": {"argv": [self.ok, "{PROMPT}"], "timeout_s": 10}})
        self.write_workers(role="Reviewer", profile="p-pri")
        rc = dr.run_assignment(self.ws, "r1", prompt=str(self.ws / "05.md"), ack=None, owner="t")
        self.assertEqual(rc, dr.EXIT_OK)

    def test_fallback_cycle_guards(self) -> None:
        profs = {"p-a": {"argv": [self.fail, "{PROMPT}"], "timeout_s": 5, "fallback_profile": "p-b"},
                 "p-b": {"argv": [self.fail, "{PROMPT}"], "timeout_s": 5, "fallback_profile": "p-a"}}
        self.write_config(profs)
        self.write_workers(role="Reviewer", profile="p-a")
        rc = dr.run_assignment(self.ws, "r1", prompt="x", ack=None, owner="t")
        self.assertEqual(rc, dr.EXIT_BLOCKED)  # 环被截断，不无限循环


class ResolutionChainTests(DispatchRoleTestBase):
    """三样本确定性（M-03 闭合）。"""

    def test_tier1_capsule_wins(self) -> None:
        cap_dir = self.ws / "caps"
        cap_dir.mkdir()
        (cap_dir / "capsule.yaml").write_text(
            yaml.safe_dump({"topic": "t", "roles_manifest": GOOD_MANIFEST}, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.ws.joinpath("workers.yaml").write_text(
            yaml.safe_dump({"roles_manifest": {**GOOD_MANIFEST, "assigned_by": "Tier2"}}, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tier, archive, man = dr.find_manifest(cap_dir)
        self.assertEqual(tier, 1)
        self.assertEqual(man["assigned_by"], "Test")

    def test_tier2_workers(self) -> None:
        self.write_workers()
        tier, archive, man = dr.find_manifest(self.ws / "sub" if (self.ws / "sub").mkdir() else self.ws)
        self.assertEqual(tier, 2)

    def test_tier3_bare(self) -> None:
        tier, archive, man = dr.find_manifest(self.ws)
        self.assertEqual((tier, archive, man), (3, None, None))

    def test_resolve_reports_gate_and_mode(self) -> None:
        self.write_config({})
        self.write_workers(role="Reviewer", profile="p")
        dr.ACTIVE_CONFIG = self.config
        rc = dr.main(["resolve", "--cwd", str(self.ws)])
        self.assertEqual(rc, dr.EXIT_OK)


class DeliverableValidityTests(DispatchRoleTestBase):
    def test_matrix(self) -> None:
        md = self.ws / "a.md"
        md.write_text("无标题正文", encoding="utf-8")
        self.assertFalse(dr.deliverable_valid(md))
        md.write_text("# 有标题\n", encoding="utf-8")
        self.assertTrue(dr.deliverable_valid(md))
        empty = self.ws / "b.txt"
        empty.write_text("", encoding="utf-8")
        self.assertFalse(dr.deliverable_valid(empty))
        empty.write_text("x", encoding="utf-8")
        self.assertTrue(dr.deliverable_valid(empty))
        self.assertFalse(dr.deliverable_valid(self.ws / "missing.md"))


class MigrateConfigTests(DispatchRoleTestBase):
    """保守迁移（R3-01 B 收缩 + C-05 幂等）。"""

    CFG_HEAD = (
        "## 角色模态外置 CLI 与模型声明\n\n"
        "| 角色模态 | 职责定位 | 承载 CLI | 启动命令 |\n|---|---|---|---|\n"
        "| Reviewer | 审计 | omp | `omp --model openai-codex/gpt-5.6-terra` |\n"
        "| Builder | 实施 | omp / claude | `omp --model zhipu-coding-plan/glm-5.3 \\|\\| claude --model sonnet-5` |\n"
        "| Designer | 设计 | claude | `claude --model sonnet-5 -p \"带引号提示\"` |\n"
        "| Researcher | 调研 | subagent | 内置 Subagent 机制 (auto) |\n"
    )

    def test_dry_run_writes_nothing(self) -> None:
        self.config.write_text(self.CFG_HEAD, encoding="utf-8")
        rc = dr.migrate_config(self.config, apply=False)
        self.assertEqual(rc, dr.EXIT_OK)
        self.assertNotIn("command_profiles", self.config.read_text(encoding="utf-8"))

    def test_apply_generates_profiles_with_prompt(self) -> None:
        self.config.write_text(self.CFG_HEAD, encoding="utf-8")
        dr.migrate_config(self.config, apply=True)
        cfg = dr.load_dispatch_config(self.config)
        self.assertEqual(cfg["default_dispatch_mode"], "strict")
        profs = cfg["command_profiles"]
        self.assertIn("-p", profs["reviewer-primary"]["argv"])
        self.assertIn("{PROMPT}", profs["reviewer-primary"]["argv"])
        self.assertEqual(profs["builder-primary"]["fallback_profile"], "builder-fallback")

    def test_quoted_candidate_refused(self) -> None:
        self.config.write_text(self.CFG_HEAD, encoding="utf-8")
        dr.migrate_config(self.config, apply=True)
        cfg = dr.load_dispatch_config(self.config)
        self.assertNotIn("designer-primary", cfg["command_profiles"])  # 含引号 → 拒迁

    def test_subagent_rows_skipped(self) -> None:
        self.config.write_text(self.CFG_HEAD, encoding="utf-8")
        dr.migrate_config(self.config, apply=True)
        cfg = dr.load_dispatch_config(self.config)
        self.assertNotIn("researcher-primary", cfg["command_profiles"])

    def test_idempotent_no_overwrite(self) -> None:
        self.config.write_text(self.CFG_HEAD, encoding="utf-8")
        dr.migrate_config(self.config, apply=True)
        first = dr.load_dispatch_config(self.config)["command_profiles"]["reviewer-primary"]["argv"]
        # 用户手工改了 timeout，重跑迁移不得覆盖
        text = self.config.read_text(encoding="utf-8").replace("timeout_s: 900", "timeout_s: 600", 1)
        self.config.write_text(text, encoding="utf-8")
        dr.migrate_config(self.config, apply=True)
        cfg = dr.load_dispatch_config(self.config)
        self.assertEqual(cfg["command_profiles"]["reviewer-primary"]["argv"], first)
        self.assertEqual(cfg["command_profiles"]["reviewer-primary"]["timeout_s"], 600)


class RealWorkspaceConfigTests(TestCase):
    """对真实实例的集成断言（§3.5 分发验收的测试面）。"""

    def test_real_config_has_strict_default(self) -> None:
        cfg = dr.load_dispatch_config()  # WS_CONFIG
        self.assertTrue(cfg["present"], "实例 workspace-config.md 缺调度参数 yaml 块（运行 setup_agents.py --migrate-command-profiles）")
        mode = cfg["default_dispatch_mode"]
        self.assertIn(mode, ("strict", None), f"default_dispatch_mode 非法: {mode!r}")

    def test_real_template_seeded(self) -> None:
        tpl = dr.SYSTEM_ROOT / "templates" / "instance" / "workspace-config.template.md"
        self.assertIn("default_dispatch_mode: strict", tpl.read_text(encoding="utf-8"))
