"""Tech-175 M1-9 / M1-10：`stage_bar()`、`pending_gates()` 纯函数与周边界。

依据：`02_方案.md` 11.2（段位状态）、11.3（六类待拍板卡片）、Q-11（本周口径）；
Spec 1.6 与 4.2 的 M1-9 / M1-10 行；AC-11、AC-13、AC-19。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import workbench_view as wv  # noqa: E402


def attempt(start, end, ok=True, dispatch_id=None):
    a = {"preference_number": 1, "started_at": start, "ended_at": end,
         "exit_code": 0 if ok else 1}
    if dispatch_id:
        a["dispatch_id"] = dispatch_id
    return a


def assignment(role, deliverable, status="succeeded", attempts=None, aid=None):
    return {"assignment_id": aid or f"{role}-1", "role": role, "status": status,
            "deliverable": deliverable, "attempts": attempts or []}


def capsule(assignments=(), segments=(), lifecycle="active",
            stages=("01_调研", "02_方案", "03_设计", "04_Spec", "07_验收")):
    return {"id": "T-1", "lifecycle": lifecycle, "instantiated_stages": list(stages),
            "roles_manifest": {"assignments": list(assignments)},
            "task_segments": list(segments)}


def open_seg(dispatch_id, role="Builder", stage="04_实施", actor="role"):
    seg = {"seg_id": "T-1#1", "state": "open", "startAt": "2026-10-30T10:00:00+08:00"}
    if actor:
        seg.update(actor=actor, role=role, dispatch_id=dispatch_id, stage=stage)
    else:
        seg["stage"] = stage
    return seg


def states(bar):
    return {s["stage"]: s["state"] for s in bar}


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class StageBarTests(unittest.TestCase):
    def test_seven_segments_in_order(self):
        bar = wv.stage_bar(capsule())
        self.assertEqual([s["stage"] for s in bar],
                         ["01_调研", "02_方案", "03_设计", "04_Spec", "05_审计", "07_验收", "08_汇报"])
        self.assertEqual(set(bar[0]), {"stage", "label", "sub", "state", "tone", "assignment"})

    def test_ac11_1_reporter_succeeded_is_done_else_uninstantiated(self):
        done = capsule([assignment("Maintainer", "07_验收.md",
                                   attempts=[attempt("2026-10-30T01:00:00+00:00", "2026-10-30T01:10:00+00:00")]),
                        assignment("Reporter", "08_汇报.md",
                                   attempts=[attempt("2026-10-30T02:00:00+00:00", "2026-10-30T02:10:00+00:00")])])
        self.assertEqual(states(wv.stage_bar(done))["08_汇报"], "done")
        self.assertEqual(states(wv.stage_bar(capsule()))["08_汇报"], "uninstantiated")

    def test_ac11_2_plan_review_marks_plan_segment_light_audit(self):
        cap = capsule([assignment("Reviewer", "03_方案审计报告.md",
                                  attempts=[attempt("2026-10-30T01:00:00+00:00", "2026-10-30T01:10:00+00:00")])])
        bar = {s["stage"]: s for s in wv.stage_bar(cap)}
        self.assertEqual(bar["02_方案"]["sub"], "轻审")
        self.assertEqual(bar["05_审计"]["state"], "uninstantiated")

    def test_impl_review_maps_to_audit_segment(self):
        cap = capsule([assignment("Reviewer", "05_实施审计报告.md",
                                  attempts=[attempt("2026-10-30T01:00:00+00:00", "2026-10-30T01:10:00+00:00")])])
        bar = {s["stage"]: s for s in wv.stage_bar(cap)}
        self.assertIsNone(bar["02_方案"]["sub"])
        self.assertEqual(bar["05_审计"]["state"], "parked")

    def test_ac11_3_running_vs_residual(self):
        cap = capsule([assignment("Builder", "04_Spec.md", status="failed")],
                      [open_seg("abc123")])
        alive = [{"dispatch_id": "abc123.2", "pid": os.getpid()}]
        bar = wv.stage_bar(cap, runs=alive)
        self.assertEqual(states(bar)["04_Spec"], "running")
        gone = [{"dispatch_id": "abc123.2", "pid": dead_pid()}]
        self.assertEqual(states(wv.stage_bar(cap, runs=gone))["04_Spec"], "residual")
        self.assertEqual(wv.stage_bar(cap, runs=gone)[3]["tone"], "amber")
        # 前缀不匹配的存活 run 不算本段运行中
        other = [{"dispatch_id": "zzz.1", "pid": os.getpid()}]
        self.assertEqual(states(wv.stage_bar(cap, runs=other))["04_Spec"], "residual")
        self.assertEqual(states(wv.stage_bar(cap))["04_Spec"], "residual")

    def test_ac11_4_p1_legacy_stage_ignored_latest_assignment_wins(self):
        cap = capsule(
            [assignment("Architecture", "02_方案.md",
                        attempts=[attempt("2026-10-30T07:00:00+00:00", "2026-10-30T08:00:00+00:00")]),
             assignment("Researcher", "01_调研.md",
                        attempts=[attempt("2026-10-29T01:00:00+00:00", "2026-10-29T02:00:00+00:00")])],
            [open_seg("legacy", stage="07_验收", actor=None) | {"state": "logged"}])
        bar = states(wv.stage_bar(cap))
        self.assertEqual(bar["02_方案"], "parked")
        self.assertEqual(bar["01_调研"], "done")

    def test_open_legacy_segment_without_actor_is_not_read(self):
        cap = capsule(segments=[open_seg("x", stage="07_验收", actor=None)])
        self.assertEqual(states(wv.stage_bar(cap))["07_验收"], "todo")

    def test_parked_tone_follows_status(self):
        ok = capsule([assignment("Designer", "03_设计.md",
                                 attempts=[attempt("2026-10-30T01:00:00+00:00", "2026-10-30T01:10:00+00:00")])])
        bad = capsule([assignment("Designer", "03_设计.md", status="failed",
                                  attempts=[attempt("2026-10-30T01:00:00+00:00", "2026-10-30T01:10:00+00:00", ok=False)])])
        self.assertEqual(wv.stage_bar(ok)[2]["tone"], "brand")
        self.assertEqual(wv.stage_bar(bad)[2]["tone"], "rose")

    def test_todo_and_does_not_mutate_input(self):
        cap = capsule([assignment("Researcher", "01_调研.md",
                                  attempts=[attempt("2026-10-29T01:00:00+00:00", "2026-10-29T02:00:00+00:00")]),
                       assignment("Architecture", "02_方案.md",
                                  attempts=[attempt("2026-10-30T01:00:00+00:00", "2026-10-30T02:00:00+00:00")])])
        import copy
        before = copy.deepcopy(cap)
        bar = states(wv.stage_bar(cap))
        self.assertEqual(bar["03_设计"], "todo")
        self.assertEqual(bar["07_验收"], "todo")
        self.assertEqual(cap, before)

    def test_does_not_read_front_matter_status(self):
        cap = capsule()
        cap["status"] = "completed"
        self.assertEqual(states(wv.stage_bar(cap))["01_调研"], "todo")


T0 = "2026-10-30T01:00:00+00:00"
T1 = "2026-10-30T02:00:00+00:00"
T2 = "2026-10-30T03:00:00+00:00"
T3 = "2026-10-30T04:00:00+00:00"


def kinds(cards):
    return sorted(c["kind"] for c in cards)


def blocking_issue():
    return {"id": "A-1", "status": "open", "level": "Major", "gate": "plan"}


class PendingGatesTests(unittest.TestCase):
    def test_prototype_review_appears_and_disappears(self):
        a = assignment("Designer", "03_设计.md", attempts=[attempt(T0, T1)])
        self.assertEqual(kinds(wv.pending_gates(capsule([a]))), ["prototype_review"])
        nxt = assignment("Builder", "04_Spec.md", status="failed", attempts=[attempt(T2, T3, ok=False)])
        self.assertNotIn("prototype_review", kinds(wv.pending_gates(capsule([a, nxt]))))

    def test_designer_rerun_brings_card_back(self):
        a = assignment("Designer", "03_设计.md", attempts=[attempt(T0, T1), attempt(T2, T3)])
        self.assertEqual(kinds(wv.pending_gates(capsule([a]))), ["prototype_review"])

    def test_preview_review(self):
        a = assignment("Builder", "04_Spec.md", attempts=[attempt(T0, T1)])
        self.assertEqual(kinds(wv.pending_gates(capsule([a]))), ["preview_review"])
        nxt = assignment("Maintainer", "07_验收.md", attempts=[attempt(T2, T3)])
        self.assertEqual(kinds(wv.pending_gates(capsule([a, nxt]))), [])

    def test_proposal_review_needs_clear_blocking_and_no_later_builder(self):
        rev = assignment("Reviewer", "03_方案审计报告.md", attempts=[attempt(T0, T1)])
        clear = {"phase": "plan", "issues": [{"id": "A-1", "status": "closed", "level": "Major", "gate": "plan"}]}
        self.assertEqual(kinds(wv.pending_gates(capsule([rev]), audit_gate=clear)), ["proposal_review"])
        # 无审计门禁数据：不推断
        self.assertEqual(wv.pending_gates(capsule([rev])), [])
        # 仍有阻断：不出现方案待确认
        blocked = {"phase": "plan", "issues": [blocking_issue()]}
        self.assertNotIn("proposal_review", kinds(wv.pending_gates(capsule([rev]), audit_gate=blocked)))
        # Builder 已开始
        bld = assignment("Builder", "04_Spec.md", status="failed", attempts=[attempt(T2, T3, ok=False)])
        self.assertNotIn("proposal_review", kinds(wv.pending_gates(capsule([rev, bld]), audit_gate=clear)))

    def test_audit_fuse_at_limit_with_blocking(self):
        rev = assignment("Reviewer", "03_方案审计报告.md",
                         attempts=[attempt(T0, T1) for _ in range(5)])
        gate = {"phase": "plan", "issues": [blocking_issue()]}
        self.assertEqual(kinds(wv.pending_gates(capsule([rev]), audit_gate=gate)), ["audit_fuse"])
        # 4 轮未到上限
        rev4 = assignment("Reviewer", "03_方案审计报告.md", attempts=[attempt(T0, T1) for _ in range(4)])
        self.assertEqual(wv.pending_gates(capsule([rev4]), audit_gate=gate), [])
        # audit_max_rounds 覆盖
        self.assertEqual(kinds(wv.pending_gates(capsule([rev4]), audit_gate=gate | {"max_rounds": 4})),
                         ["audit_fuse"])
        # 阻断清零 → 熔断卡消失
        clear = {"phase": "plan", "issues": []}
        self.assertNotIn("audit_fuse", kinds(wv.pending_gates(capsule([rev]), audit_gate=clear)))

    def test_impl_audit_limit_is_seven(self):
        def rev(n):
            return assignment("Reviewer", "05_实施审计报告.md", attempts=[attempt(T0, T1) for _ in range(n)])
        gate = {"phase": "impl", "issues": [blocking_issue() | {"gate": "impl"}]}
        self.assertEqual(wv.pending_gates(capsule([rev(6)]), audit_gate=gate), [])
        self.assertEqual(kinds(wv.pending_gates(capsule([rev(7)]), audit_gate=gate)), ["audit_fuse"])

    def test_worktime_review_three_sources(self):
        pend = capsule(segments=[{"seg_id": "T-1#1", "state": "pending_approval", "actor": "role",
                                  "dispatch_id": "d1"}])
        self.assertEqual(kinds(wv.pending_gates(pend)), ["worktime_review"])
        residual = capsule(segments=[open_seg("d2")])
        self.assertEqual(kinds(wv.pending_gates(residual, runs=[])), ["worktime_review"])
        running = [{"dispatch_id": "d2.1", "pid": os.getpid()}]
        self.assertEqual(wv.pending_gates(residual, runs=running), [])
        for status in ("unknown", "needs_manual_fix"):
            self.assertEqual(kinds(wv.pending_gates(capsule(), ledger=[{"status": status}])), ["worktime_review"])
        self.assertEqual(wv.pending_gates(capsule(), ledger=[{"status": "pushed"}]), [])

    def test_dispatch_failed_until_success_or_reassigned(self):
        bad = assignment("Builder", "04_Spec.md", status="blocked", aid="B-1",
                         attempts=[attempt(T0, T1, ok=False)])
        self.assertEqual(kinds(wv.pending_gates(capsule([bad]))), ["dispatch_failed"])
        fixed = assignment("Builder", "04_Spec.md", status="succeeded", aid="B-1",
                           attempts=[attempt(T0, T1, ok=False), attempt(T2, T3)])
        self.assertNotIn("dispatch_failed", kinds(wv.pending_gates(capsule([fixed]))))
        again = assignment("Builder", "04_Spec.md", status="succeeded", aid="B-2",
                           attempts=[attempt(T2, T3)])
        self.assertNotIn("dispatch_failed", kinds(wv.pending_gates(capsule([bad, again]))))

    def test_ac13_delivered_has_no_cards(self):
        a = assignment("Designer", "03_设计.md", attempts=[attempt(T0, T1)])
        bad = assignment("Builder", "04_Spec.md", status="failed", aid="B", attempts=[attempt(T0, T1, ok=False)])
        for life in ("delivered", "archived"):
            cap = capsule([a, bad], [open_seg("d")], lifecycle=life)
            self.assertEqual(wv.pending_gates(cap, runs=[], ledger=[{"status": "unknown"}]), [])

    def test_naive_and_utc_timestamps_compare_in_cst(self):
        # 2026-10-30T09:00:00+08:00 == 01:00Z；无时区视作 +08:00
        a = assignment("Designer", "03_设计.md", attempts=[attempt("2026-10-30T08:00:00", "2026-10-30T09:00:00")])
        nxt = assignment("Builder", "04_Spec.md", status="failed",
                         attempts=[attempt("2026-10-30T01:30:00+00:00", "2026-10-30T02:00:00+00:00", ok=False)])
        self.assertNotIn("prototype_review", kinds(wv.pending_gates(capsule([a, nxt]))))

    def test_writes_no_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            before = list(Path(tmp).iterdir())
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch("builtins.open", side_effect=AssertionError("pure function opened a file")):
                    wv.pending_gates(capsule([assignment("Designer", "x.md", attempts=[attempt(T0, T1)])],
                                             [open_seg("d")]), runs=[])
                    wv.stage_bar(capsule([assignment("Designer", "x.md", attempts=[attempt(T0, T1)])]))
            finally:
                os.chdir(cwd)
            self.assertEqual(list(Path(tmp).iterdir()), before)


if __name__ == "__main__":
    unittest.main()
