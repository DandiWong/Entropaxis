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

OK_BODY_NAME = "ok_body.md"  # 替身 CLI 把这份合规产出拷到交付物位置
FAIL_SH = '#!/bin/sh\nexit 1\n'
SLOW_SH = '#!/bin/sh\nsleep 5\n'


def _mkexe(dir_: Path, name: str, body: str) -> str:
    p = dir_ / name
    p.write_text(body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# 判据升级后，夹具必须是真正合规的产出——它同时充当「合规长什么样」的可执行文档。
RESEARCH_BODY = """# 某主题调研

## 一、执行摘要
结论先行。

## 二、多方案对比矩阵
| 维度 | 方案A | 方案B |
|---|---|---|
| 证据等级 | A | B |
| 时效性 | 高 | 中 |
| 风险 | 低 | 高 |

## 三、主推方案与落地路径
信源 https://example.org/a 与 https://example.org/b

## 四、风险与 Plan B
风险一。
"""

PROPOSAL_BODY = """# 某主题方案

## 一、业务目标
目标。

## 二、系统边界与非目标
非目标。

## 三、架构设计
架构。

## 四、核心决策与替代方案对比
| 决策 | 选定 | 替代 |
|---|---|---|
| A | 甲 | 乙 |
| B | 丙 | 丁 |
"""


def research_doc(topic: str = "某主题") -> str:
    return (f"---\ntype: Research\ntopic: {topic}\ndate: 2026-09-19\nauthor: Researcher\n"
            "status: draft\ncarrier: session-local\n---\n\n") + RESEARCH_BODY


def proposal_doc(author: str = "Architecture") -> str:
    return (f"---\ntype: Proposal\ntopic: 某主题\ndate: 2026-09-19\nauthor: {author}\n"
            "status: draft\ncarrier: session-local\n---\n\n") + PROPOSAL_BODY


def audit_doc(target_sha: str) -> str:
    """Reviewer 的成功须过完整审计契约，不只是围栏能解析（审计 C-02）。"""
    return ("---\ntype: Audit\ntopic: 某主题\ndate: 2026-09-19\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 3\nreviewer_mode: session\nreviewer_ref: current-session\n"
            "fallback_reason: not_configured\ncarrier: session-local\n"
            f"target_path: 01.md\ntarget_sha256: {target_sha}\n---\n\n"
            '# 审计报告\n\n```audit-state\n{"issues": [], "critical_acks": []}\n```\n')


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
        self.ok_body = self.ws / OK_BODY_NAME
        self.ok_body.write_text(research_doc(), encoding="utf-8")
        self.ok = _mkexe(self.scripts, "ok.sh", f'#!/bin/sh\ncat "{self.ok_body}" > "$1"\n')
        self.fail = _mkexe(self.scripts, "fail.sh", FAIL_SH)
        self.slow = _mkexe(self.scripts, "slow.sh", SLOW_SH)
        self.config = self.ws / "workspace-config.md"
        self._old_active = dr.ACTIVE_CONFIG
        dr.ACTIVE_CONFIG = self.config
        # 轨迹重定向到临时目录：测试跑一次就往生产轨迹里灌几十条噪声，
        # 会把真实调度证据埋掉——留痕文件是给人查的，不该被测试污染。
        self._old_trace = dr.TRACE_LOG
        dr.TRACE_LOG = self.ws / "trace.jsonl"

    def tearDown(self) -> None:
        dr.ACTIVE_CONFIG = self._old_active
        dr.TRACE_LOG = self._old_trace
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
        # 判据按角色取，夹具产出也要按角色给：Reviewer 须过完整审计契约，其余走文档判据
        self.ok_body.write_text(
            audit_doc(dr._sha256_file(self.ws / "01.md")) if role == "Reviewer" else proposal_doc(),
            encoding="utf-8")
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
        self.ok_body.write_text(audit_doc(dr._sha256_file(self.ws / "01.md")), encoding="utf-8")
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
        cfg = dr.load_dispatch_config()  # roles.yaml
        if not cfg["present"]:
            self.skipTest("本机尚未初始化 roles.yaml（空态即初始态）")
        mode = cfg["default_dispatch_mode"]
        self.assertIn(mode, ("strict", None), f"default_dispatch_mode 非法: {mode!r}")

    def test_real_template_seeded(self) -> None:
        tpl = dr.SYSTEM_ROOT / "templates" / "instance" / "roles.template.yaml"
        self.assertIn("default_dispatch_mode: strict", tpl.read_text(encoding="utf-8"))

    def test_real_config_passes_schema(self) -> None:
        from tools import validate_schema as vs
        if not dr.ROLES_CONFIG.exists():
            self.skipTest("本机尚未初始化 roles.yaml")
        data = yaml.safe_load(dr.ROLES_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(vs.validate(data, vs.load_schema("roles_config")), [])


class DirectRunTests(DispatchRoleTestBase):
    """直跑模式：无手写 assignment 的第 3 级调度 + carrier 回执盖章。

    真源：rules/角色协作.md「角色指派三级解析与调度门禁」建档条款。此前调度一次必须
    先手写 YAML 并算 target_sha256，调研类角色还没有前序对象可指纹——外置 CLI 因此
    从不被调用。本组证明：指派由工具落笔、无档案不造容器、承载如实盖章。
    """

    SKELETON = "---\ntype: Research\ntopic: 某主题\ndate: 2026-09-19\nauthor: Researcher\nstatus: draft\ncarrier: session-local\n---\n\n# 某主题\n"

    def setUp(self) -> None:
        super().setUp()
        self.append = _mkexe(self.scripts, "append.sh", f'#!/bin/sh\ncat "{self.ok_body}" > "$2"\n')
        self.out = self.ws / "01_调研.md"
        self.out.write_text(self.SKELETON, encoding="utf-8")

    def append_profile(self) -> dict:
        """替身 CLI：把落盘路径当独立 argv 传入，模拟外置 Agent 按提示词写出交付物。"""
        return {"researcher-primary": {"argv": [self.append, "{PROMPT}", str(self.out)], "timeout_s": 30}}

    def write_capsule(self, revision: int = 0) -> Path:
        cap = self.ws / "capsule.yaml"
        cap.write_text(yaml.safe_dump({"id": "T-1", "roles_manifest": {
            "assigned_at": "2026-09-19T00:00:00+08:00", "assigned_by": "init_capsule.py",
            "revision": revision, "assignments": []}}, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return cap

    def test_empty_capsule_is_archivable_but_not_a_manifest(self) -> None:
        """find_manifest 只认已有指派，find_archive_any 要能看见空指派的新胶囊。"""
        self.write_capsule()
        self.assertEqual(dr.find_manifest(self.ws)[0], 3)
        self.assertEqual(dr.find_archive_any(self.ws)[0], 1)

    def test_success_writes_assignment_and_stamps_carrier(self) -> None:
        self.write_config(self.append_profile())
        self.write_capsule()
        rc = dr.run_direct(self.ws, "Researcher", "调研任务", Path("01_调研.md"), owner="test")
        self.assertEqual(rc, dr.EXIT_OK)
        self.assertIn("carrier: researcher-primary", self.out.read_text(encoding="utf-8"))
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(man["revision"], 1)
        a = man["assignments"][0]
        self.assertEqual((a["role"], a["status"], a["deliverable"]), ("Researcher", "succeeded", "01_调研.md"))
        self.assertNotIn("target_path", a, "无前序受审对象的角色不得被迫编造 target")
        self.assertEqual(a["deliverable_sha256"], dr._sha256_file(self.out), "指纹须覆盖盖章后的文件")
        self.assertEqual(dr.validate_manifest(man), [])

    def test_rerun_reuses_assignment_id(self) -> None:
        self.write_config(self.append_profile())
        self.write_capsule()
        for _ in range(2):
            dr.run_direct(self.ws, "Researcher", "调研任务", Path("01_调研.md"), owner="test")
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(len(man["assignments"]), 1, "同一交付物重跑禁重编号")
        self.assertEqual(man["revision"], 2)

    def test_no_archive_yields_receipt_only(self) -> None:
        self.write_config(self.append_profile())
        rc = dr.run_direct(self.ws, "Researcher", "调研任务", Path("01_调研.md"), owner="test")
        self.assertEqual(rc, dr.EXIT_OK)
        self.assertFalse((self.ws / "capsule.yaml").exists(), "档案缺失时不得擅自造容器")

    def test_unconfigured_soft_role_local_carry_keeps_session_local(self) -> None:
        self.write_config({})
        self.write_capsule()
        rc = dr.run_direct(self.ws, "Researcher", "任务", Path("01_调研.md"), owner="test")
        self.assertEqual(rc, dr.EXIT_LOCAL)
        self.assertIn("carrier: session-local", self.out.read_text(encoding="utf-8"))
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(man["assignments"][0]["status"], "failed")

    def test_hard_role_still_blocked(self) -> None:
        """降摩擦只作用于建档成本，不得顺手放宽硬门禁。"""
        self.write_config({})
        self.write_capsule()
        rc = dr.run_direct(self.ws, "Reviewer", "任务", Path("05_审计报告.md"), owner="test")
        self.assertEqual(rc, dr.EXIT_BLOCKED)

    def test_stamp_carrier_skips_files_without_front_matter(self) -> None:
        plain = self.ws / "plain.md"
        plain.write_text("# 无档头\n", encoding="utf-8")
        self.assertFalse(dr.stamp_carrier(plain, "researcher-primary"))
        self.assertEqual(plain.read_text(encoding="utf-8"), "# 无档头\n")

    def test_manifest_without_target_is_valid(self) -> None:
        man = {"assigned_at": "2026-09-19T00:00:00+00:00", "assigned_by": "T", "revision": 1,
               "assignments": [{"assignment_id": "a1", "role": "Architecture", "command_profile": "architecture-primary",
                                "deliverable": "02_方案.md", "status": "pending"}]}
        self.assertEqual(dr.validate_manifest(man), [])

    def test_prompt_binds_deliverable_path(self) -> None:
        """外置 CLI 默认只打 stdout；不绑路径则每次调度都以 NO_VALID_OUTPUT 收场。"""
        echo = _mkexe(self.scripts, "echo_prompt.sh", '#!/bin/sh\nprintf "%s" "$1" > "$2"\n')
        self.write_config({"researcher-primary": {"argv": [echo, "{PROMPT}", str(self.ws / "seen.txt")], "timeout_s": 30}})
        dr.run_direct(self.ws, "Researcher", "调研某主题", Path("01_调研.md"), owner="test")
        seen = (self.ws / "seen.txt").read_text(encoding="utf-8")
        self.assertIn("调研某主题", seen)
        self.assertIn(str((self.ws / "01_调研.md").resolve()), seen)


class FailureStampTests(DispatchRoleTestBase):
    """失败盖章不得破坏既有有效承载证据（复核 M-07）。

    一次 TIMEOUT 曾把上一轮成功报告的 carrier/receipt_id/reviewer_mode 就地抹成
    session-local + timeout，正文未变而证据没了——失败尝试反过来破坏历史事实。
    """

    def setUp(self) -> None:
        super().setUp()
        # 必须与 dispatch_role 内部 _receipt_module() 拿到的是同一个模块对象，
        # 否则打的补丁落在另一份全局变量上，测试会"通过"得毫无意义。
        rc = dr._receipt_module()
        self.rc = rc
        self._old = (rc.RECEIPT_DIR, rc.KEY_FILE)
        rc.RECEIPT_DIR = self.ws / "receipts"
        rc.KEY_FILE = self.ws / "credentials" / "k.key"
        self.report = self.ws / "05_审计报告.md"
        self.report.write_text("---\ntype: Audit\ntopic: t\ndate: 2026-09-19\nauthor: Reviewer\n"
                               "status: active\n---\n\n# 复核\n结论\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.rc.RECEIPT_DIR, self.rc.KEY_FILE = self._old
        super().tearDown()

    def _stamp_success(self) -> str:
        r = self.rc.issue("Reviewer", "a" * 64, self.report, None, "reviewer-primary", "omp -p {PROMPT}")
        dr.stamp_carrier(self.report, "reviewer-primary", "omp -p {PROMPT}", "",
                         audit=True, receipt_id=r["receipt_id"])
        return r["receipt_id"]

    def test_failed_dispatch_keeps_carrier_evidence(self) -> None:
        """失败只写 fallback_reason，承载字段一律不动（复核 M-07 / B）。"""
        rid = self._stamp_success()
        self.assertTrue(dr.stamp_carrier(self.report, None, None, "timeout", audit=True, receipt_id=None))
        after = self.report.read_text(encoding="utf-8")
        self.assertIn("carrier: reviewer-primary", after, "失败不得改写既有承载")
        self.assertIn(f"receipt_id: {rid}", after, "失败不得抹掉上一轮的有效回执")
        self.assertIn("reviewer_mode: external", after)
        self.assertIn("fallback_reason: timeout", after, "降级原因仍须留痕")
        self.assertTrue(self.rc.verify(rid, text=after)[0], "只动 Front Matter 不影响正文绑定")

    def test_failure_never_asserts_a_carrier_it_cannot_prove(self) -> None:
        """一份真由外置 CLI 产出的报告，曾被失败路径盖成 session-local（复核 B）。"""
        self.report.write_text("---\ntype: Audit\ntopic: t\ndate: 2026-09-20\nauthor: Reviewer\n"
                               "status: active\ncarrier: reviewer-primary\n---\n\n# 报告\n正文\n",
                               encoding="utf-8")
        dr.stamp_carrier(self.report, None, None, "no_valid_output", audit=True, receipt_id=None)
        text = self.report.read_text(encoding="utf-8")
        self.assertIn("carrier: reviewer-primary", text)
        self.assertNotIn("session-local", text)
        self.assertNotIn("reviewer_mode: session", text)


class VerifyTamperTests(DispatchRoleTestBase):
    """校验期间改写被审代码即判失败（M-04 的可机械部分）。

    完整的 M-04——测试是否充分——没有机械解，仍 open；这里只关掉"先把产品代码改坏
    再 exit 0 仍判通过"这一条实测反例。
    """

    def test_rewriting_product_code_during_verify_fails(self) -> None:
        product = self.ws / "product.py"
        product.write_text("def answer():\n    return 42\n", encoding="utf-8")
        cheat = _mkexe(self.scripts, "cheat.sh", f'#!/bin/sh\necho "def answer(: broken" > "{product}"\nexit 0\n')
        self.write_config({"builder-verify": {"argv": [cheat], "timeout_s": 30}})
        r = dr.run_verify("builder-verify", dr.load_dispatch_config(), self.ws, "Builder")
        self.assertFalse(r["passed"], "改坏产品代码后退出 0 不得判通过")
        self.assertEqual(r["failure_code"], "VERIFY_FAILED")
        self.assertIn("product.py", r["detail"])

    def test_new_cache_files_are_not_tampering(self) -> None:
        (self.ws / "product.py").write_text("x = 1\n", encoding="utf-8")
        honest = _mkexe(self.scripts, "honest.sh",
                        f'#!/bin/sh\nmkdir -p "{self.ws}/__pycache__"\n'
                        f'echo cache > "{self.ws}/__pycache__/x.pyc"\necho report > "{self.ws}/coverage.xml"\nexit 0\n')
        self.write_config({"builder-verify": {"argv": [honest], "timeout_s": 30}})
        r = dr.run_verify("builder-verify", dr.load_dispatch_config(), self.ws, "Builder")
        self.assertTrue(r["passed"], f"新建产物不算改写：{r.get('detail')}")


class StampBeforeCriteriaTests(DispatchRoleTestBase):
    """承载字段由工具盖，判据就不能反过来要求模型先写出它们（复核 A）。

    实测第 5 轮：外置 Reviewer 按要求不自写 carrier/reviewer_mode，判据即报
    NO_VALID_OUTPUT；上一轮之所以通过，是因为模型照抄了上一轮的旧承载字段。
    """

    def test_output_without_carrier_fields_still_passes(self) -> None:
        target = self.ws / "01.md"
        target.write_text("# 目标\n", encoding="utf-8")
        clean = ("---\ntype: Audit\ntopic: 某主题\ndate: 2026-09-19\nauthor: Reviewer\nstatus: active\n"
                 "schema_version: 3\n"
                 f"target_path: 01.md\ntarget_sha256: {dr._sha256_file(target)}\n---\n\n"
                 '# 审计报告\n\n```audit-state\n{"issues": [], "critical_acks": []}\n```\n')
        body = self.ws / "clean_report.md"
        body.write_text(clean, encoding="utf-8")
        out = self.ws / "05_审计报告.md"
        out.write_text("---\ntype: Audit\ntopic: 某主题\ndate: 2026-09-19\nauthor: Reviewer\n"
                       "status: draft\ncarrier: session-local\n---\n\n# 骨架\n", encoding="utf-8")
        writer = _mkexe(self.scripts, "review.sh", f'#!/bin/sh\ncat "{body}" > "$2"\n')
        self.write_config({"reviewer-primary": {"argv": [writer, "{PROMPT}", str(out)], "timeout_s": 30}})
        rc = dr.run_direct(self.ws, "Reviewer", "复核", Path("05_审计报告.md"), owner="test", target=Path("01.md"))
        self.assertEqual(rc, dr.EXIT_OK, dr._deliverable_issues(out, "Reviewer"))
        stamped = out.read_text(encoding="utf-8")
        self.assertIn("carrier: reviewer-primary", stamped)
        self.assertIn("reviewer_mode: external", stamped, "承载由回执盖章，不由模型自述")


class StdinIsolationTests(DispatchRoleTestBase):
    """被调度 CLI 的 stdin 必须显式给 DEVNULL。

    继承调用者的 stdin 时，CLI 见 stdin 非 tty 会当成"有管道输入"并读到 EOF 为止；
    调用者（后台任务、编排脚本）不关闭那一端，它就永远读不到——实测外置 Reviewer
    卡在 `phase: readPipedInput` 22 分钟零产出，最后以 TIMEOUT 收场，看起来像模型
    不行，其实一个字都没开始生成。
    """

    def test_child_does_not_inherit_a_never_closing_stdin(self) -> None:
        reader = _mkexe(self.scripts, "reader.sh",
                        f'#!/bin/sh\ncat > /dev/null\ncat "{self.ok_body}" > "$2"\n')
        out = self.ws / "01_调研.md"
        self.write_config({"researcher-primary": {"argv": [reader, "{PROMPT}", str(out)], "timeout_s": 5}})
        r, w = os.pipe()  # 只建不写：模拟"调用者持着写端不关"的真实场景
        saved = os.dup(0)
        try:
            os.dup2(r, 0)
            rc = dr.run_direct(self.ws, "Researcher", "调研任务", Path("01_调研.md"), owner="test")
        finally:
            os.dup2(saved, 0)
            for fd in (saved, r, w):
                os.close(fd)
        self.assertEqual(rc, dr.EXIT_OK, "子进程不得因继承永不关闭的 stdin 而挂到超时")


class RealConfigResolutionTests(TestCase):
    """对真实 roles.yaml 的解析断言（门禁四律「执行态可见」的可负担部分）。"""

    def test_researcher_chain_resolves_to_declared_cli(self) -> None:
        cfg = dr.load_dispatch_config()
        name, chain = dr._tier3_default_chain("Researcher", cfg)
        if name in ("UNCONFIGURED", "SUBAGENT_AUTO"):
            # 收件方尚未配置角色承载是合法初始态，不是契约破损（治理准则「空态即初始态」）
            self.skipTest(f"本机未配置 Researcher 外置承载（{name}），跳过实例断言")
        self.assertTrue(chain, f"Researcher 未解析出可执行链: {name}")
        self.assertIn(dr.PROMPT_PLACEHOLDER, chain[0]["argv"], "argv 缺 {PROMPT} 占位符 → 任务文本无法注入")
        self.assertLessEqual(len(chain), 3, "备选链深须 ≤3")


class ArgvBuildTests(DispatchRoleTestBase):
    """argv 组装：模板守 shell 元字符，提示词按数据放行。"""

    def test_multiline_prompt_survives(self) -> None:
        argv = dr.build_argv({"argv": ["cli", "-p", "{PROMPT}"]}, "第一行\n第二行 | 带管道符")
        self.assertEqual(argv, ["cli", "-p", "第一行\n第二行 | 带管道符"])

    def test_template_metachars_rejected(self) -> None:
        self.assertIsNone(dr.build_argv({"argv": ["cli -p {PROMPT} || other"]}, "x"))
        self.assertIsNone(dr.build_argv({"argv": ["cli", "-p", "{PROMPT}", ">", "out.md"]}, "x"))

    def test_missing_placeholder_rejected(self) -> None:
        self.assertIsNone(dr.build_argv({"argv": ["cli", "-p"]}, "x"))


class DeliverableFormTests(DispatchRoleTestBase):
    """成功判据按交付物形态取——"文件存在"对目录型和代码型角色是假门禁。"""

    def test_empty_dir_is_not_a_deliverable(self) -> None:
        d = self.ws / "prototypes"
        d.mkdir()
        self.assertFalse(dr.deliverable_valid(d), "空目录此前一律判 succeeded，是最大的假门禁")
        (d / "index.html").write_text("<html></html>", encoding="utf-8")
        self.assertTrue(dr.deliverable_valid(d))

    def test_fence_required_for_directory_roles(self) -> None:
        d = self.ws / "prototypes"
        d.mkdir()
        (d / "index.html").write_text("<html></html>", encoding="utf-8")
        self.assertFalse(dr.deliverable_valid(d, "Designer"), "目录型角色缺状态围栏应判不通过")
        (d / "README.md").write_text(
            "---\ntype: Proposal\ntopic: 某主题\ndate: 2026-09-19\nauthor: Designer\n"
            "status: draft\ncarrier: session-local\n---\n\n"
            "# 原型\n\n## 一、页面拓扑\n\n## 二、字段映射\n\n## 三、交互状态\n\n## 四、原型索引\n\n"
            '```deliverable-state\n{"role": "Designer", "outputs": ["index.html"]}\n```\n', encoding="utf-8")
        self.assertTrue(dr.deliverable_valid(d, "Designer"), dr._deliverable_issues(d, "Designer"))

    def test_fence_must_be_parseable_json_with_outputs(self) -> None:
        f = self.ws / "07.md"
        f.write_text('# 报告\n\n```deliverable-state\n{不是JSON}\n```\n', encoding="utf-8")
        self.assertIsNone(dr.parse_state_fence(f))
        self.assertTrue(any("JSON" in e for e in dr.validate_state_fence(f, "Reporter")))
        f.write_text('# 报告\n\n```deliverable-state\n{"role": "Reporter", "outputs": []}\n```\n', encoding="utf-8")
        self.assertTrue(dr.validate_state_fence(f, "Reporter"), "outputs 为空等于没产出")

    def test_fence_outputs_must_exist_on_disk(self) -> None:
        """声明与事实不绑定时，围栏只是一段自述（审计 M-02）。"""
        d = self.ws / "08_汇报"
        d.mkdir()
        (d / "real.html").write_text("<html></html>", encoding="utf-8")
        (d / "README.md").write_text(
            '# 汇报\n\n```deliverable-state\n{"role": "Reporter", "outputs": ["missing.html"]}\n```\n', encoding="utf-8")
        self.assertTrue(any("不存在" in e for e in dr.validate_state_fence(d, "Reporter")))
        (d / "README.md").write_text(
            '# 汇报\n\n```deliverable-state\n{"role": "Reporter", "outputs": ["real.html"]}\n```\n', encoding="utf-8")
        self.assertEqual(dr.validate_state_fence(d, "Reporter"), [])

    def test_fence_role_must_match_and_be_unique(self) -> None:
        d = self.ws / "out2"
        d.mkdir()
        (d / "a.txt").write_text("x", encoding="utf-8")
        body = '```deliverable-state\n{"role": "%s", "outputs": ["a.txt"]}\n```'
        (d / "README.md").write_text("# x\n\n" + body % "Designer" + "\n", encoding="utf-8")
        self.assertTrue(any("不符" in e for e in dr.validate_state_fence(d, "Reporter")))
        (d / "README.md").write_text("# x\n\n" + body % "Reporter" + "\n\n" + body % "Reporter" + "\n", encoding="utf-8")
        self.assertTrue(any("恰好一个" in e for e in dr.validate_state_fence(d, "Reporter")))

    def test_unmapped_role_fails_closed(self) -> None:
        """缺判据映射一律 fail-closed，不得按缺省文档分支静默放宽（审计 M-03）。"""
        f = self.ws / "x.md"
        f.write_text("# 标题\n", encoding="utf-8")
        self.assertFalse(dr.deliverable_valid(f, "Unknown"))
        self.assertTrue(any("fail-closed" in r for r in dr._deliverable_issues(f, "Unknown")))
        self.assertEqual(set(dr.ROLE_CRITERIA), set(dr.ALL_ROLES), "七角色映射必须穷尽")

    def test_doc_type_applies_to_dirs_without_readme(self) -> None:
        """无 README.md 的目录此前直接跳过 doc_type：一份 type: Proposal 的 note.md
        配有效围栏即通过 Reporter 判据（复核 M-03 残留旁路）。"""
        d = self.ws / "09_汇报"
        d.mkdir()
        (d / "deck.html").write_text("<html></html>", encoding="utf-8")
        note = d / "note.md"
        fm = ("---\ntype: %s\ntopic: 某主题\ndate: 2026-09-19\nauthor: Reporter\n"
              "status: draft\ncarrier: session-local\n---\n\n# 汇报\n\n"
              '```deliverable-state\n{"role": "Reporter", "outputs": ["deck.html"]}\n```\n')
        note.write_text(fm % "Proposal", encoding="utf-8")
        self.assertTrue(any("产出类型须为 Report" in e for e in dr._deliverable_issues(d, "Reporter")))
        note.write_text(fm % "Report", encoding="utf-8")
        self.assertFalse([e for e in dr._deliverable_issues(d, "Reporter") if "产出类型" in e])

    def test_dir_fingerprint_is_content_addressed(self) -> None:
        d = self.ws / "out"
        d.mkdir()
        (d / "a.txt").write_text("x", encoding="utf-8")
        first = dr._sha256_path(d)
        self.assertEqual(first, dr._sha256_path(d), "同内容须同指纹")
        (d / "a.txt").write_text("y", encoding="utf-8")
        self.assertNotEqual(first, dr._sha256_path(d))


class TargetChainTests(DispatchRoleTestBase):
    """输入依据指纹：上一棒改了，这一棒的 succeeded 必须能被看出已失效。"""

    def setUp(self) -> None:
        super().setUp()
        self.ok_body.write_text(proposal_doc(), encoding="utf-8")
        self.append = _mkexe(self.scripts, "append.sh", f'#!/bin/sh\ncat "{self.ok_body}" > "$2"\n')
        self.target = self.ws / "01_调研.md"
        self.target.write_text("# 调研\n", encoding="utf-8")
        self.out = self.ws / "02_方案.md"
        self.out.write_text("---\ntype: Proposal\ntopic: 某主题\ndate: 2026-09-19\nauthor: Architecture\nstatus: draft\ncarrier: session-local\n---\n\n# 方案\n", encoding="utf-8")
        cap = self.ws / "capsule.yaml"
        cap.write_text(yaml.safe_dump({"roles_manifest": {
            "assigned_at": "2026-09-19T00:00:00+08:00", "assigned_by": "t", "revision": 0, "assignments": []}},
            allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _run(self) -> int:
        self.write_config({"architecture-primary": {"argv": [self.append, "{PROMPT}", str(self.out)], "timeout_s": 30}})
        return dr.run_direct(self.ws, "Architecture", "出方案", Path("02_方案.md"), owner="t", target=Path("01_调研.md"))

    def test_target_fingerprint_recorded(self) -> None:
        self.assertEqual(self._run(), dr.EXIT_OK)
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        a = man["assignments"][0]
        self.assertEqual(a["target_path"], "01_调研.md")
        self.assertEqual(a["target_sha256"], dr._sha256_file(self.target))
        self.assertEqual(dr.validate_manifest(man), [])

    def test_changed_target_marks_stale(self) -> None:
        self._run()
        self.target.write_text("# 调研（修订）\n", encoding="utf-8")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dr.cmd_resolve(self.ws)
        row = json.loads(buf.getvalue())["assignments"][0]
        self.assertTrue(row["stale"], "依据改了，上一轮 succeeded 必须显示为 stale")

    def test_attempts_carry_the_input_fingerprint(self) -> None:
        """依赖历史随执行记录走：assignment 的 target_* 只留最新一跳。"""
        self._run()
        first = dr._sha256_file(self.target)
        self.target.write_text("# 调研（修订）\n", encoding="utf-8")
        self._run()
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        a = man["assignments"][0]
        shas = [at.get("target_sha256") for at in a["attempts"]]
        self.assertEqual(shas[0], first, "第一次执行所依据的旧指纹必须留得住")
        self.assertEqual(shas[-1], dr._sha256_file(self.target))
        self.assertNotEqual(shas[0], shas[-1], "两次依据不同，记录不得被最新一跳抹平")
        self.assertEqual(dr.validate_manifest(man), [])

    def test_missing_target_rejected(self) -> None:
        self.write_config({})
        rc = dr.run_direct(self.ws, "Architecture", "x", Path("02_方案.md"), owner="t", target=Path("nope.md"))
        self.assertEqual(rc, dr.EXIT_USAGE)


class VerifyProfileTests(DispatchRoleTestBase):
    """代码型角色：成功判据是校验命令退出码，不是文件是否存在。"""

    def setUp(self) -> None:
        super().setUp()
        self.touch = _mkexe(self.scripts, "touch.sh", '#!/bin/sh\n:\n')
        self.pass_sh = _mkexe(self.scripts, "pass.sh", '#!/bin/sh\nexit 0\n')
        self.failing = _mkexe(self.scripts, "failing.sh", '#!/bin/sh\necho "2 failed" >&2\nexit 1\n')
        cap = self.ws / "capsule.yaml"
        cap.write_text(yaml.safe_dump({"roles_manifest": {
            "assigned_at": "2026-09-19T00:00:00+08:00", "assigned_by": "t", "revision": 0, "assignments": []}},
            allow_unicode=True, sort_keys=False), encoding="utf-8")

    def test_verify_pass_is_the_success_criterion(self) -> None:
        self.write_config({"builder-primary": {"argv": [self.touch, "{PROMPT}"], "timeout_s": 30},
                           "builder-verify": {"argv": [self.pass_sh], "timeout_s": 30}})
        rc = dr.run_direct(self.ws, "Builder", "实施", None, owner="t", verify="builder-verify")
        self.assertEqual(rc, dr.EXIT_OK, "CLI 没写任何文件，但校验通过即成功")
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        a = man["assignments"][0]
        self.assertEqual(a["status"], "succeeded")
        self.assertEqual(a["verify_profile"], "builder-verify")
        self.assertTrue(a["deliverable_ref"].startswith(("git:", "path:")))
        self.assertNotIn("deliverable", a, "代码型角色不应被迫指一个交付物文件")
        self.assertEqual(dr.validate_manifest(man), [])

    def test_out_and_verify_both_record_revision_ref(self) -> None:
        """Builder 既产出 Spec 文档又改代码时，修订标识不得因为有 --out 就整个丢失。"""
        out = self.ws / "04_Spec_Tech-1.md"
        out.write_text("# Spec\n", encoding="utf-8")
        writer = _mkexe(self.scripts, "w2.sh", '#!/bin/sh\necho "## x" >> "$2"\n')
        self.write_config({"builder-primary": {"argv": [writer, "{PROMPT}", str(out)], "timeout_s": 30},
                           "builder-verify": {"argv": [self.pass_sh], "timeout_s": 30}})
        dr.run_direct(self.ws, "Builder", "实施", Path("04_Spec_Tech-1.md"), owner="t", verify="builder-verify")
        a = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]["assignments"][0]
        self.assertTrue(a["deliverable"])
        self.assertTrue(a["deliverable_ref"].startswith(("git:", "path:")), "文档与代码修订标识须并存")

    def test_verify_fail_overrides_a_written_file(self) -> None:
        """外置 CLI 写出了文件也不算数——测试不过就是没做完。"""
        out = self.ws / "04_Spec_Tech-1.md"
        out.write_text("# Spec\n", encoding="utf-8")
        writer = _mkexe(self.scripts, "w.sh", '#!/bin/sh\necho "## x" >> "$2"\n')
        self.write_config({"builder-primary": {"argv": [writer, "{PROMPT}", str(out)], "timeout_s": 30},
                           "builder-verify": {"argv": [self.failing], "timeout_s": 30}})
        rc = dr.run_direct(self.ws, "Builder", "实施", Path("04_Spec_Tech-1.md"), owner="t", verify="builder-verify")
        self.assertEqual(rc, dr.EXIT_LOCAL)
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(man["assignments"][0]["status"], "failed")
        self.assertEqual(man["assignments"][0]["attempts"][-1]["failure_code"], "VERIFY_FAILED")

    def test_verify_is_builder_only(self) -> None:
        """C-01：--verify 放开给所有角色时，硬门禁可被一条 exit 0 整条绕过。"""
        self.write_config({"proof": {"argv": [self.pass_sh], "timeout_s": 10}})
        for role in ("Reviewer", "Maintainer", "Researcher"):
            self.assertEqual(dr.run_direct(self.ws, role, "x", None, owner="t", verify="proof"), dr.EXIT_USAGE, role)
        man = yaml.safe_load((self.ws / "capsule.yaml").read_text(encoding="utf-8"))["roles_manifest"]
        self.assertEqual(man["assignments"], [], "被拒的调度不得留下任何 succeeded 记录")

    def test_hard_role_requires_a_deliverable(self) -> None:
        """独立性以产出为证：没有审计报告的 Reviewer 不存在"成功"这回事。"""
        self.write_config({"proof": {"argv": [self.pass_sh], "timeout_s": 10}})
        self.assertEqual(dr.run_direct(self.ws, "Reviewer", "x", None, owner="t"), dr.EXIT_USAGE)

    def test_unconfigured_verify_profile_fails_closed(self) -> None:
        self.write_config({"builder-primary": {"argv": [self.touch, "{PROMPT}"], "timeout_s": 30}})
        rc = dr.run_direct(self.ws, "Builder", "实施", None, owner="t", verify="nope-verify")
        self.assertEqual(rc, dr.EXIT_LOCAL)


class CarrierTripleTests(DispatchRoleTestBase):
    """承载三元组：与 audit_report 的 reviewer_* 同构，值来自回执。"""

    SKELETON = "---\ntype: Research\ntopic: 某主题\ndate: 2026-09-19\nauthor: Researcher\nstatus: draft\ncarrier: session-local\n---\n\n# 某主题\n"

    def test_stamp_writes_all_three_and_clears_on_success(self) -> None:
        f = self.ws / "01.md"
        f.write_text(self.SKELETON, encoding="utf-8")
        dr.stamp_carrier(f, "researcher-primary", "omp --model x -p {PROMPT}", "")
        text = f.read_text(encoding="utf-8")
        self.assertIn("carrier: researcher-primary", text)
        self.assertIn("carrier_ref:", text)
        self.assertNotIn("fallback_reason:", text, "成功时不留失败原因")

    def test_stamp_records_structured_fallback_reason(self) -> None:
        f = self.ws / "01.md"
        f.write_text(self.SKELETON, encoding="utf-8")
        dr.stamp_carrier(f, "session-local", "当前会话 Agent", "not_configured")
        self.assertIn("fallback_reason: not_configured", f.read_text(encoding="utf-8"))

    def test_command_text_with_colon_is_quoted(self) -> None:
        """命令原文含冒号裸写会让 Front Matter 解析错位。"""
        f = self.ws / "01.md"
        f.write_text(self.SKELETON, encoding="utf-8")
        dr.stamp_carrier(f, "p", "cli --url http://x:8080 -p {PROMPT}", "")
        line = next(ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.startswith("carrier_ref:"))
        self.assertTrue(line.split(":", 1)[1].strip().startswith('"'))

    def test_audit_report_references_the_triple_instead_of_copying(self) -> None:
        """承载三元组必须由 audit_report 引用而非各抄一份（元规则 #1：能引用就不复制）。"""
        raw = json.loads((dr.SCHEMA_DIR / "audit_report.schema.json").read_text(encoding="utf-8"))
        props = raw["properties"]["front_matter"]["properties"]
        for key in ("reviewer_ref", "fallback_reason", "carrier", "carrier_ref"):
            self.assertIn("$ref", props[key], f"{key} 应引用通用定义，不得内联复制")
            self.assertTrue(props[key]["$ref"].startswith("front_matter.schema.json#/"))

    def test_ref_resolves_to_the_same_contract(self) -> None:
        """引用展开后与被引用方逐字段相等；展开失败要报错而不是静默放行。"""
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import validate_schema as vs  # noqa: PLC0415
        fm = vs.load_schema("front_matter")["properties"]
        ar = vs.load_schema("audit_report")["properties"]["front_matter"]["properties"]
        self.assertEqual(ar["fallback_reason"]["pattern"], fm["fallback_reason"]["pattern"])
        self.assertEqual(ar["reviewer_ref"]["type"], fm["carrier_ref"]["type"])
        with self.assertRaises(ValueError):
            vs._resolve_refs({"$ref": "front_matter.schema.json#/properties/不存在的键"})


class TraceTests(DispatchRoleTestBase):
    """执行轨迹：档案只存 argv_sha256（可证伪不可还原），trace 存命令原文供人核对。"""

    def test_trace_records_full_argv_and_exit(self) -> None:
        log = self.ws / "trace.jsonl"
        out = self.ws / "01.md"
        out.write_text("---\ntype: Research\ntopic: t\ndate: 2026-09-19\nauthor: Researcher\nstatus: draft\ncarrier: session-local\n---\n\n# t\n", encoding="utf-8")
        appender = _mkexe(self.scripts, "a.sh", f'#!/bin/sh\ncat "{self.ok_body}" > "$2"\n')
        old, dr.TRACE_LOG = dr.TRACE_LOG, log
        try:
            self.write_config({"researcher-primary": {"argv": [appender, "{PROMPT}", str(out)], "timeout_s": 30}})
            dr.run_direct(self.ws, "Researcher", "调研某事", Path("01.md"), owner="t")
        finally:
            dr.TRACE_LOG = old
        rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["role"], r["exit_code"], r["executed"]), ("Researcher", 0, True))
        self.assertIn("调研某事", " ".join(r["argv"]), "轨迹须含提示词原文，否则无法核对跑的是什么")
        self.assertTrue(r["deliverable_valid"])

    def test_trace_never_blocks_dispatch(self) -> None:
        """留痕失败不得反过来阻断被留痕的动作。"""
        old, dr.TRACE_LOG = dr.TRACE_LOG, Path("/proc/nonexistent/x/trace.jsonl")
        try:
            dr.trace({"role": "X"})
        finally:
            dr.TRACE_LOG = old

    def test_directory_deliverable_stamps_its_readme(self) -> None:
        """目录型交付物本身没有 Front Matter，承载须盖在其 README.md 上。"""
        d = self.ws / "08_汇报"
        d.mkdir()
        self.assertFalse(dr.stamp_carrier(d, "reporter-primary", "omp -p {PROMPT}"), "无 README 时不硬盖")
        (d / "README.md").write_text(
            "---\ntype: Report\ntopic: t\ndate: 2026-09-19\nauthor: Reporter\nstatus: draft\ncarrier: session-local\n---\n\n# 汇报\n",
            encoding="utf-8")
        self.assertTrue(dr.stamp_carrier(d, "reporter-primary", "omp -p {PROMPT}"))
        self.assertIn("carrier: reporter-primary", (d / "README.md").read_text(encoding="utf-8"))

    def test_trace_rotates_instead_of_growing_unbounded(self) -> None:
        """一行约 2KB，无上限会无声长成几十 MB。"""
        log = self.ws / "t.jsonl"
        old_log, old_max = dr.TRACE_LOG, dr.TRACE_MAX_BYTES
        dr.TRACE_LOG, dr.TRACE_MAX_BYTES = log, 200
        try:
            for i in range(20):
                dr.trace({"role": "X", "detail": "y" * 60})
        finally:
            dr.TRACE_LOG, dr.TRACE_MAX_BYTES = old_log, old_max
        self.assertTrue(log.with_suffix(".jsonl.1").is_file(), "超限须转存为 .1")
        self.assertLessEqual(log.stat().st_size, 400, "当前文件须已从头写")



class ReviewerFenceTests(DispatchRoleTestBase):
    """Reviewer 的 audit-state 围栏纳入调度判据（只验可解析，语义仍归 check_audit_gate）。"""

    def test_report_without_audit_state_is_not_valid_output(self) -> None:
        f = self.ws / "05_审计报告.md"
        f.write_text("# 审计报告\n\n看起来很像一份报告，但没有机器状态。\n", encoding="utf-8")
        self.assertTrue(dr.deliverable_valid(f), "对其他角色仍是有效文档")
        self.assertFalse(dr.deliverable_valid(f, "Reviewer"), "Reviewer 缺围栏即非有效产出")

    def test_unparseable_fence_rejected(self) -> None:
        f = self.ws / "05_审计报告.md"
        f.write_text("# 审计报告\n\n```audit-state\n{坏 JSON}\n```\n", encoding="utf-8")
        self.assertFalse(dr.deliverable_valid(f, "Reviewer"))

    def test_valid_fence_accepted_even_when_empty(self) -> None:
        """空 issues 是合法状态（没查出问题），与「没写围栏」必须区分开。"""
        target = self.ws / "01.md"
        target.write_text("# 目标\n", encoding="utf-8")
        f = self.ws / "05_审计报告.md"
        f.write_text(audit_doc(dr._sha256_file(target)), encoding="utf-8")
        self.assertTrue(dr.deliverable_valid(f, "Reviewer"), dr._deliverable_issues(f, "Reviewer"))

    def test_fence_alone_is_not_enough_for_reviewer(self) -> None:
        """围栏能解析 ≠ 审计契约成立：`{"arbitrary":true}` 曾被判有效（审计 C-02）。"""
        f = self.ws / "05_审计报告.md"
        f.write_text('# 审计\n\n```audit-state\n{"arbitrary": true}\n```\n', encoding="utf-8")
        self.assertFalse(dr.deliverable_valid(f, "Reviewer"))
        self.assertTrue(any("审计契约" in e or "Front Matter" in e for e in dr._deliverable_issues(f, "Reviewer")))

    def test_fence_output_outside_capsule_still_rejected(self) -> None:
        """三基准解析是为了容纳真实书写习惯，不是放弃越界防护。"""
        (self.ws / "capsule.yaml").write_text(
            yaml.safe_dump({"roles_manifest": {"assigned_at": "2026-09-19T00:00:00+08:00",
                                               "assigned_by": "t", "revision": 0, "assignments": []}}), encoding="utf-8")
        d = self.ws / "out3"
        d.mkdir()
        (d / "a.txt").write_text("x", encoding="utf-8")
        (d / "README.md").write_text(
            '# x\n\n```deliverable-state\n{"role": "Reporter", "outputs": ["/etc/hosts"]}\n```\n', encoding="utf-8")
        errs = dr.validate_state_fence(d, "Reporter")
        self.assertTrue(errs, "绝对路径须被拦")

    def test_role_must_match_the_deliverable_type(self) -> None:
        """只穷尽角色键还不够：Maintainer 曾交一份结构合规的 Proposal 即判通过（复核 M-03）。"""
        f = self.ws / "07_验收报告.md"
        f.write_text("---\ntype: Proposal\ntopic: t\ndate: 2026-09-19\nauthor: Maintainer\n"
                     "status: draft\ncarrier: session-local\n---\n\n"
                     "# 目标\n\n## 边界与非目标\n\n## 架构设计\n\n## 决策与替代对比\n"
                     "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n", encoding="utf-8")
        self.assertFalse(dr.deliverable_valid(f, "Maintainer"))
        self.assertTrue(any("产出类型须为 Report" in e for e in dr._deliverable_issues(f, "Maintainer")))

    def test_every_role_binds_a_doc_type(self) -> None:
        self.assertEqual({r for r, c in dr.ROLE_CRITERIA.items() if not c.get("doc_type")}, set(),
                         "每个角色都须绑定产出类型，否则该类判据仍可被绕过")
