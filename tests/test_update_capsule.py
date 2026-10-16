import hashlib
import json
import subprocess
import sys
import tempfile
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

    def report(self, issues: str) -> Path:
        target = self.cap / "02_方案.md"
        target.write_text("方案\n", encoding="utf-8")
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        text = REPORT_TEMPLATE.format(mode="external", target=target.name, sha=sha, extra="")
        start, end = text.index('"issues": [') + len('"issues": ['), text.index("],\n  \"critical_acks\"")
        path = self.cap / "05_审计报告.md"
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

    def test_cli(self) -> None:
        ok = subprocess.run([sys.executable, str(TOOL), str(self.cap), "--json"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["to"], "draft")
        bad = subprocess.run([sys.executable, str(TOOL), str(self.cap / "nope")], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("👉", bad.stderr)

    def test_task_segment_lifecycle_and_rounding(self) -> None:
        # 1. 验证取整算法 q_minutes
        self.assertEqual(uc.q_minutes(7.0), 0.25)
        self.assertEqual(uc.q_minutes(7.5), 0.25)
        self.assertEqual(uc.q_minutes(8.0), 0.25)
        self.assertEqual(uc.q_minutes(22.5), 0.5)
        self.assertEqual(uc.q_minutes(479.0), 8.0)
        self.assertEqual(uc.q_minutes(480.0), 8.0)
        self.assertEqual(uc.q_minutes(481.0), 8.0)

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

        # 4. 人工覆盖 --override
        res_ov = uc.override_worklog(self.cap, ref="Tech-1#1", spent=2.0, summary="修正工时")
        self.assertEqual(res_ov["status"], "overridden")
        self.assertEqual(res_ov["entry"]["spent"], 2.0)
        self.assertEqual(res_ov["entry"]["original_spent"], 1.25)
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


if __name__ == "__main__":
    unittest.main()
