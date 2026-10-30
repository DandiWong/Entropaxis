import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import lint_workspace as lw
from tools import update_audit_state as uas
from tools import update_capsule as uc
from tests.test_update_audit_state import REPORT_TEMPLATE

TOOL = Path(uc.__file__)

MANIFEST = """# 事务胶囊清单
id: Tech-1
lifecycle: {lifecycle}
delivered_at: null        # 交付日
review_due_at: null       # +30 天
closure_deadline: null    # +60 天
relations: []
"""

ACCEPTANCE = """# 验收报告
- **数据回收日期**：{data}
- **未度量兜底说明**：{unmeasured}
"""


class UpdateCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261006_主题"
        self.cap.mkdir()
        self.manifest(lifecycle="draft")

    def tearDown(self) -> None:
        self._td.cleanup()

    def manifest(self, **kw) -> None:
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(**kw), encoding="utf-8")

    def report(self, issues: str, name: str = "03_方案审计报告.md") -> Path:
        target = self.cap / "02_方案.md"
        target.write_text("方案\n", encoding="utf-8")
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        text = REPORT_TEMPLATE.format(mode="external", target=target.name, sha=sha, extra="")
        start, end = text.index('"issues": [') + len('"issues": ['), text.index("],\n  \"critical_acks\"")
        path = self.cap / name
        path.write_text(text[:start] + issues + text[end:], encoding="utf-8")
        return path

    def lifecycle(self) -> str:
        return uc._read_key((self.cap / "capsule.yaml").read_text(encoding="utf-8"), "lifecycle")

    def test_open_major_keeps_draft_then_close_via_audit_state_advances(self) -> None:
        report = self.report('{"id": "M-1", "level": "Major", "status": "open"}')
        self.assertEqual(uc.update_capsule(str(self.cap))["to"], "draft")
        text = report.read_text(encoding="utf-8")
        uas.commit(report, uas.dump_state(text, uas.set_field(uas.load_state(text), "M-1", "status", "closed")))
        self.assertEqual(self.lifecycle(), "active", "关闭最后一个阻断问题应由 update_audit_state 顺带推进胶囊")

    def test_legacy_combined_report_name_still_gates(self) -> None:
        self.report('{"id": "M-1", "level": "Major", "status": "open"}', name="05_审计报告.md")
        self.assertEqual(uc.update_capsule(str(self.cap))["to"], "draft")

    def test_open_minor_does_not_block(self) -> None:
        self.report('{"id": "m-1", "level": "Minor", "status": "open"}')
        self.assertEqual(uc.update_capsule(str(self.cap))["to"], "active")

    def test_spec_advances_and_comments_preserved(self) -> None:
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        uc.update_capsule(str(self.cap))
        text = (self.cap / "capsule.yaml").read_text(encoding="utf-8")
        self.assertIn("lifecycle: active\n", text)
        self.assertIn("delivered_at: null        # 交付日", text)

    def test_delivered_derives_dates_and_is_write_once(self) -> None:
        self.manifest(lifecycle="active")
        res = uc.update_capsule(str(self.cap), delivered="2026-10-06")
        self.assertEqual(res["changes"], {"delivered_at": "2026-10-06", "review_due_at": "2026-11-05",
                                          "closure_deadline": "2026-12-05", "lifecycle": "delivered"})
        self.assertIn("review_due_at: 2026-11-05       # +30 天",
                      (self.cap / "capsule.yaml").read_text(encoding="utf-8"))
        with self.assertRaises(uc.ToolError):
            uc.update_capsule(str(self.cap), delivered="2026-10-07")

    def test_bad_date_rejected(self) -> None:
        with self.assertRaises(uc.ToolError):
            uc.update_capsule(str(self.cap), delivered="10/06")

    def test_archive_from_acceptance(self) -> None:
        for data, unmeasured, expected in (("2026-11-05", "待回填", "archived"),
                                           ("待回填", "业务方未提供数据", "archived-unmeasured"),
                                           ("待回填", "待回填", "delivered")):
            self.manifest(lifecycle="delivered")
            (self.cap / "07_验收报告.md").write_text(ACCEPTANCE.format(data=data, unmeasured=unmeasured), encoding="utf-8")
            self.assertEqual(uc.update_capsule(str(self.cap))["to"], expected)

    def test_never_moves_backwards_or_out_of_terminal(self) -> None:
        self.manifest(lifecycle="archived")
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        self.assertEqual(uc.update_capsule(str(self.cap))["changes"], {})

    def test_dry_run_writes_nothing(self) -> None:
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        self.assertEqual(uc.update_capsule(str(self.cap), dry_run=True)["to"], "active")
        self.assertEqual(self.lifecycle(), "draft")

    def test_sync_for_outside_capsule_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(uc.sync_for(Path(td) / "x.md"))

    def test_lint_reports_drift(self) -> None:
        root = Path(self._td.name)
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        tools = root / ".entropaxis" / "tools"
        tools.mkdir(parents=True)
        (tools / "update_capsule.py").write_text(TOOL.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertTrue(any("[胶囊状态落后]" in i for i in lw.check_capsule_lifecycle(root)))
        uc.update_capsule(str(self.cap))
        self.assertEqual(lw.check_capsule_lifecycle(root), [])

    def test_lint_reports_overdue_value_review(self) -> None:
        root = Path(self._td.name)
        self.manifest(lifecycle="delivered")
        text = (self.cap / "capsule.yaml").read_text(encoding="utf-8").replace(
            "review_due_at: null", "review_due_at: 2000-01-01")
        (self.cap / "capsule.yaml").write_text(text, encoding="utf-8")
        tools = root / ".entropaxis" / "tools"
        tools.mkdir(parents=True)
        (tools / "update_capsule.py").write_text(TOOL.read_text(encoding="utf-8"), encoding="utf-8")
        issues = lw.check_capsule_lifecycle(root)
        self.assertTrue(any("[价值回收到期]" in item for item in issues), issues)

    def test_cli(self) -> None:
        ok = subprocess.run([sys.executable, str(TOOL), str(self.cap)], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("draft", ok.stdout)
        bad = subprocess.run([sys.executable, str(TOOL), str(self.cap / "nope")], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("👉", bad.stderr)

    def test_task_segment_lifecycle(self) -> None:
        # 1. 秒级派生（Tech-176 T2）：q_minutes 已删除，spent 由整数秒派生
        self.assertFalse(hasattr(uc, "q_minutes"))
        self.assertEqual(uc._seconds_to_spent(4500), 1.25)

        # 2. 开段 --start-task
        res_start = uc.start_task(self.cap, name="功能开发", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        self.assertEqual(res_start["status"], "started")
        seg = res_start["segment"]
        self.assertEqual(seg["seq"], 1)
        self.assertEqual(seg["seg_id"], "Tech-1#1")
        self.assertEqual(seg["state"], "open")

        # 阻止并发开段
        with self.assertRaises(uc.ToolError):
            uc.start_task(self.cap, name="冲突段")

        # 刷新活跃时间
        uc.touch_task(self.cap, at="2026-10-15T09:30:00+08:00")
        segs = uc.read_segments(self.cap)
        self.assertIn("09:30:00", segs[0]["lastActivityAt"])

        # 3. 收尾入账 (<= 8h, 75分钟 -> 1.25h)
        res_end = uc.end_task(self.cap, at="2026-10-15T10:15:00+08:00", trigger="deliver")
        self.assertEqual(res_end["status"], "ended")
        self.assertEqual(res_end["segment"]["state"], "logged")
        self.assertEqual(res_end["entry"]["spent"], 1.25)
        self.assertEqual(res_end["worklog_refs"], ["Tech-1#1"])
        self.assertEqual(uc.worklog_total(self.cap), 1.25)

        # 4. 人工覆盖 --override（T5 后 --reason 必填；spent 仍兼容小时浮点直调）
        res_ov = uc.override_worklog(self.cap, ref="Tech-1#1", spent=2.0, summary="修正工时",
                                     reason="人工核对后修正")
        self.assertEqual(res_ov["status"], "overridden")
        self.assertEqual(res_ov["entry"]["spent"], 2.0)
        self.assertEqual(res_ov["entry"]["seconds"], 7200)
        self.assertEqual(res_ov["entry"]["original_seconds"], 4500)
        self.assertEqual(res_ov["entry"]["original_spent"], 1.25)
        self.assertEqual(res_ov["entry"]["override_reason"], "人工核对后修正")
        self.assertTrue(res_ov["entry"]["overridden"])
        self.assertEqual(uc.worklog_total(self.cap), 2.0)

    def test_segment_over_8h_requires_approval(self) -> None:
        # 开段并经过 9 小时 (540 分钟)
        uc.start_task(self.cap, name="长任务", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        res_end = uc.end_task(self.cap, at="2026-10-15T18:00:00+08:00")
        self.assertEqual(res_end["segment"]["state"], "pending_approval")
        self.assertEqual(res_end["worklog_refs"], [])
        self.assertEqual(uc.worklog_total(self.cap), 0.0)

        # 批准入账
        res_app = uc.approve_segment(self.cap, seg_id="Tech-1#1")
        self.assertEqual(res_app["status"], "approved")
        self.assertEqual(res_app["entry"]["spent"], 9.0)
        self.assertEqual(uc.worklog_total(self.cap), 9.0)

    def test_archive_gate_blocks_open_or_pending_segments(self) -> None:
        self.manifest(lifecycle="delivered")
        (self.cap / "07_验收报告.md").write_text(ACCEPTANCE.format(data="2026-11-05", unmeasured="待回填"), encoding="utf-8")

        # 存在 open 段拦截归档
        uc.start_task(self.cap, name="遗留段", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        with self.assertRaises(uc.ToolError):
            uc.update_capsule(str(self.cap))

        # 收尾成为 pending_approval 仍然拦截 (9h, >8h)
        uc.end_task(self.cap, at="2026-10-15T18:00:00+08:00")
        with self.assertRaises(uc.ToolError):
            uc.update_capsule(str(self.cap))

        # 标记为 unmeasured 后放行归档
        uc.unmeasure_segment(self.cap, seg_id="Tech-1#1", reason="测试忽略")
        res = uc.update_capsule(str(self.cap))
        self.assertEqual(res["to"], "archived")



class SegmentSecondsSettleTests(unittest.TestCase):
    """Tech-176 T3a/T3c（方案 4.4.2、判定 1/3）：结算改整数秒；缺省 stage 不再按 mtime 推断。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_秒级结算"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_threshold_constants_in_seconds(self) -> None:
        self.assertEqual(uc.AUTO_LIMIT_S, 28800)
        self.assertEqual(uc.APPROVAL_LIMIT_S, 86400)
        self.assertFalse(hasattr(uc, "AUTO_LIMIT_MIN"), "分钟制门槛常量应删除")
        self.assertFalse(hasattr(uc, "APPROVAL_LIMIT_MIN"), "分钟制门槛常量应删除")

    def test_old_segment_settles_in_seconds(self) -> None:
        uc.start_task(self.cap, name="调研", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        res = uc.end_task(self.cap, at="2026-10-15T09:07:00+08:00")
        seg = res["segment"]
        self.assertEqual(seg["state"], "logged")
        self.assertEqual(seg["machine_seconds"], 420)
        self.assertNotIn("effective_seconds", seg, "旧段（无 actor）不写有效秒")
        entry = res["entry"]
        self.assertEqual(entry["seconds"], 420)
        self.assertEqual(entry["spent"], 0.1167)
        self.assertNotIn("actor", entry, "旧段结算不推断执行主体")
        self.assertEqual(uc.worklog_total(self.cap), 0.1167)

    def test_8h01m_still_pending_approval(self) -> None:
        uc.start_task(self.cap, name="长任务", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        res = uc.end_task(self.cap, at="2026-10-15T17:01:00+08:00")
        self.assertEqual(res["segment"]["state"], "pending_approval")
        self.assertEqual(res["segment"]["machine_seconds"], 28860)
        self.assertEqual(res["worklog_refs"], [])
        self.assertEqual(uc.worklog_total(self.cap), 0.0)

    def test_p1_default_stage_is_misc_not_mtime(self) -> None:
        # P-1 复现：07_验收报告.md mtime 最新时，不传 stage 不得再被推断成 07_验收
        (self.cap / "02_方案.md").write_text("方案\n", encoding="utf-8")
        newest = self.cap / "07_验收报告.md"
        newest.write_text("验收\n", encoding="utf-8")
        stale = time.time() - 3600
        os.utime(self.cap / "02_方案.md", (stale, stale))
        os.utime(newest, None)
        self.assertFalse(hasattr(uc, "_default_stage"), "mtime 推断入口应删除")
        res = uc.start_task(self.cap, name="事务", task_id="Tech-1")
        self.assertEqual(res["segment"]["stage"], "misc")


class SegmentInvariantTests(unittest.TestCase):
    """Tech-176 T3b（方案 4.4.1 兜底）：I-10 / I-7 写前不变量，违规拒绝写入且磁盘字节不变。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_写前不变量"
        self.cap.mkdir()

    def tearDown(self) -> None:
        self._td.cleanup()

    def _write_manifest(self, data: dict) -> bytes:
        import yaml
        payload = {"id": "Tech-1", "lifecycle": "active", **data}
        p = self.cap / "capsule.yaml"
        p.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return p.read_bytes()

    def test_i10_manager_logged_without_evidence_blocks_write(self) -> None:
        seg = {"seq": 1, "seg_id": "Tech-1#1", "name": "事务段",
               "startAt": "2026-10-15T09:00:00+08:00", "endAt": "2026-10-15T09:10:00+08:00",
               "state": "logged", "actor": "manager", "effective_seconds": 600}
        entry = {"date": "2026-10-15", "stage": "misc", "spent": 0.1667, "seconds": 600,
                 "actor": "manager", "summary": "事务段（自动计量入账）",
                 "operator": "tester", "ref": "Tech-1#1"}
        before = self._write_manifest({"task_segments": [seg], "worklogs": [entry], "seq_next": 2})
        with self.assertRaises(uc.ToolError) as ctx:
            uc.log_time(self.cap, spent="1h", stage="misc", summary="补登", operator="tester")
        self.assertIn("I-10", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before, "写入被拒后文件字节必须不变")

    def test_i7_spent_seconds_mismatch_blocks_write(self) -> None:
        entry = {"date": "2026-10-15", "stage": "misc", "spent": 0.5, "seconds": 420,
                 "summary": "被手改的旧条目", "operator": "tester"}
        before = self._write_manifest({"worklogs": [entry], "seq_next": 2})
        with self.assertRaises(uc.ToolError) as ctx:
            uc.log_time(self.cap, spent="1h", stage="misc", summary="补登", operator="tester")
        self.assertIn("I-7", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before, "写入被拒后文件字节必须不变")


class OverrideAuditTests(unittest.TestCase):
    """Tech-176 T5（P-5 复现）：--reason 必填、支持 --stage、保存 original_* 与 override_reason。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_覆盖审计"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _logged_7m_segment(self) -> str:
        uc.start_task(self.cap, name="设计", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        uc.end_task(self.cap, at="2026-10-15T09:07:00+08:00")
        return "Tech-1#1"

    def test_override_with_stage_reason_and_seconds(self) -> None:
        ref = self._logged_7m_segment()
        res = uc.override_worklog(self.cap, ref=ref, spent="30m", stage="03_设计",
                                  reason="P-1 mtime 推断错位")
        e = res["entry"]
        self.assertEqual(e["stage"], "03_设计")
        self.assertEqual(e["original_stage"], "misc")
        self.assertEqual(e["override_reason"], "P-1 mtime 推断错位")
        self.assertEqual(e["seconds"], 1800)
        self.assertEqual(e["spent"], 0.5)
        self.assertEqual(e["original_seconds"], 420)
        self.assertEqual(e["original_spent"], 0.1167)

    def test_override_old_entry_backfills_original_seconds(self) -> None:
        entry = {"date": "2026-10-15", "stage": "02_方案", "spent": 0.25,
                 "summary": "旧条目", "operator": "tester", "ref": "Tech-1#1"}
        seg = {"seq": 1, "seg_id": "Tech-1#1", "name": "旧段",
               "startAt": "2026-10-15T09:00:00+08:00", "endAt": "2026-10-15T09:15:00+08:00",
               "state": "logged"}
        self._write_manifest({"task_segments": [seg], "worklogs": [entry], "seq_next": 2})
        res = uc.override_worklog(self.cap, ref="Tech-1#1", spent="1h", reason="补录秒数")
        e = res["entry"]
        self.assertEqual(e["original_seconds"], 900, "无 seconds 的旧条目按 round(spent×3600) 补算")
        self.assertEqual(e["original_spent"], 0.25)
        self.assertEqual(e["seconds"], 3600)
        self.assertEqual(e["spent"], 1.0)

    def test_second_override_keeps_originals(self) -> None:
        ref = self._logged_7m_segment()
        uc.override_worklog(self.cap, ref=ref, spent="30m", reason="第一次")
        res = uc.override_worklog(self.cap, ref=ref, spent="45m", reason="第二次")
        e = res["entry"]
        self.assertEqual(e["seconds"], 2700)
        self.assertEqual(e["original_seconds"], 420, "二次覆盖不得改写 original_*")
        self.assertEqual(e["original_spent"], 0.1167)
        self.assertEqual(e["override_reason"], "第二次", "override_reason 记最近一次原因")

    def test_cli_override_without_reason_rejected(self) -> None:
        ref = self._logged_7m_segment()
        before = (self.cap / "capsule.yaml").read_bytes()
        proc = subprocess.run([sys.executable, str(TOOL), str(self.cap),
                               "--override", ref, "--spent", "30m"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--reason", proc.stderr)
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before, "拒绝后文件字节不变")

    def _write_manifest(self, data: dict) -> None:
        import yaml
        payload = {"id": "Tech-1", "lifecycle": "active", **data}
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
