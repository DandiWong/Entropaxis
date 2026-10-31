import contextlib
import datetime as dt
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

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


def write_legacy_open_segment(cap: Path, *, name: str = "旧规则开的段",
                              at: str = "2026-10-15T09:00:00+08:00",
                              stage: str | None = None, seq: int = 1,
                              seg_id: str = "Tech-1#1", lifecycle: str = "active") -> None:
    """直接落一个无 actor 的 open 旧段（Tech-176 T7 后的夹具入口）。

    T7 起 `start_task` 只开 `actor: manager` 段，Manager 结段的证据门槛属 T8；
    测「旧段按旧语义结算」与「调度结算 open 段」的用例改为直接构造无 actor 段，
    不再经过 `start_task`。
    """
    import yaml
    seg: dict = {"seq": seq, "seg_id": seg_id, "name": name,
                 "startAt": at, "lastActivityAt": at, "state": "open"}
    if stage:
        seg["stage"] = stage
    payload = {"id": "Tech-1", "lifecycle": lifecycle,
               "task_segments": [seg], "worklogs": [], "seq_next": seq + 1}
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


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

        # 2. 开段 --start-task（T7 起只开 actor=manager 段，并记 baseline）
        res_start = uc.start_task(self.cap, name="功能开发", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        self.assertEqual(res_start["status"], "started")
        seg = res_start["segment"]
        self.assertEqual(seg["seq"], 1)
        self.assertEqual(seg["seg_id"], "Tech-1#1")
        self.assertEqual(seg["state"], "open")
        self.assertEqual(seg["actor"], "manager")
        self.assertIn("baseline", seg)

        # 阻止并发开段（已有 open Manager 段）
        with self.assertRaises(uc.ToolError):
            uc.start_task(self.cap, name="冲突段")

        # 刷新活跃时间
        uc.touch_task(self.cap, at="2026-10-15T09:30:00+08:00")
        segs = uc.read_segments(self.cap)
        self.assertIn("09:30:00", segs[0]["lastActivityAt"])

        # 3. 收尾入账 (<= 8h, 75分钟 -> 1.25h)。Manager 段的证据门槛与有效秒口径属 T8，
        #    本条钉旧语义结算路径，改用无 actor 旧段夹具。
        write_legacy_open_segment(self.cap, name="功能开发", stage="04_实施", seq=2, seg_id="Tech-1#2")
        res_end = uc.end_task(self.cap, at="2026-10-15T10:15:00+08:00", trigger="deliver")
        self.assertEqual(res_end["status"], "ended")
        self.assertEqual(res_end["segment"]["state"], "logged")
        self.assertEqual(res_end["segment"]["machine_seconds"], 4500)
        self.assertEqual(res_end["entry"]["spent"], 1.25)
        self.assertEqual(res_end["entry"]["seconds"], 4500)
        self.assertEqual(res_end["worklog_refs"], ["Tech-1#2"])
        self.assertEqual(uc.worklog_total(self.cap), 1.25)

        # 4. 人工覆盖 --override（T5 后 --reason 必填；spent 仍兼容小时浮点直调）
        res_ov = uc.override_worklog(self.cap, ref="Tech-1#2", spent=2.0, summary="修正工时",
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
        # 开段并经过 9 小时 (540 分钟)；旧段夹具（Manager 段 8h 分支属 T8）
        write_legacy_open_segment(self.cap, name="长任务")
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
        (self.cap / "07_验收报告.md").write_text(ACCEPTANCE.format(data="2026-11-05", unmeasured="待回填"), encoding="utf-8")

        # 存在 open 段拦截归档（旧段夹具：Manager 段结段门槛属 T8）
        write_legacy_open_segment(self.cap, name="遗留段", lifecycle="delivered")
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
        write_legacy_open_segment(self.cap, name="调研", stage="01_调研")
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
        write_legacy_open_segment(self.cap, name="长任务")
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
        # 旧段夹具：T7 后 start_task 只开 Manager 段（其结算门槛属 T8）
        write_legacy_open_segment(self.cap, name="设计")
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


class CapsuleWriteLockTests(unittest.TestCase):
    """Tech-176 T6（AC-10，方案 4.4 判定 6）：所有写函数读→写全程持胶囊目录锁。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_写锁"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_concurrent_touch_and_log_time_lose_nothing(self) -> None:
        """8 线程并发 --touch 与 log_time：worklogs 条数等于成功调用次数，seq 不重复。"""
        uc.start_task(self.cap, name="并发段", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        self.assertEqual(uc.read_segments(self.cap)[0]["actor"], "manager",
                         "T7 后 --start-task 只开 Manager 段")
        errors: list[str] = []
        ok: list[str] = []
        rejected: list[str] = []

        def worker(i: int) -> None:
            try:
                if i % 2 == 0:
                    uc.touch_task(self.cap, at=f"2026-10-15T09:{i:02d}:00+08:00")
                else:
                    uc.log_time(self.cap, spent="1h", stage="misc",
                                summary=f"并发登记 {i}", operator=f"t{i}")
                ok.append(f"call-{i}")
            except uc.ToolError as err:
                # 业务拒绝（如五元组重复）是合法结果，不算丢数据
                rejected.append(f"call-{i}: {err}")
            except Exception as err:  # noqa: BLE001
                errors.append(f"call-{i}: {err!r}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], "并发写不得抛异常")
        self.assertEqual(len(ok) + len(rejected), 8, "每次调用都须有确定结果")
        logs = uc.read_worklogs(self.cap)
        added = [w for w in logs if w["summary"].startswith("并发登记")]
        self.assertEqual(len(added), len(ok) - 4, "touch 不写 worklog；log_time 成功数须等于新增条数")
        seqs = [w["ref"] for w in logs]
        self.assertEqual(len(seqs), len(set(seqs)), "ref 不得重复")

    def test_lock_held_during_write_blocks_concurrent_update(self) -> None:
        """持锁期间 ManifestStore.update 写 assignments 与 log_time 等待后写入：roles_manifest 不丢。"""
        store = uc._manifest_store(self.cap / "capsule.yaml")
        # 夹具无 roles_manifest 块，先种一条 revision=0 供 update 的 CAS 递增
        (self.cap / "capsule.yaml").write_text(
            MANIFEST.format(lifecycle="active").rstrip() + "\nroles_manifest:\n  revision: 0\n",
            encoding="utf-8")
        self.assertTrue(store.acquire("holder"), "测试前置：先持有胶囊目录锁")
        try:
            outcome: dict = {}

            def run_log() -> None:
                try:
                    uc.log_time(self.cap, spent="1h", stage="misc", summary="持锁等待", operator="t")
                    outcome["log"] = "written"
                except uc.ToolError as err:
                    outcome["log"] = f"toolerror:{err}"

            t = threading.Thread(target=run_log)
            t.start()
            time.sleep(0.3)
            self.assertFalse(outcome, "持锁期间 log_time 必须等待，不得写入")

            # 并发方走 ManifestStore.update 写 assignments，与持锁方互斥
            def bump(man: dict) -> None:
                man["revision"] = int(man.get("revision", 0)) + 1
                man.setdefault("assignments", []).append(
                    {"assignment_id": "a-2", "role": "Builder", "status": "logged"})

            store.update(bump, "holder")
            store.release()
            t.join(timeout=60)
            self.assertEqual(outcome.get("log"), "written", "锁释放后等待中的写入必须成功")
        finally:
            store.release()

        import yaml
        data = yaml.safe_load((self.cap / "capsule.yaml").read_text(encoding="utf-8"))
        man = data.get("roles_manifest") or {}
        self.assertTrue(any(a.get("assignment_id") == "a-2" for a in man.get("assignments", [])),
                        "ManifestStore 写入的 assignments 不得被 capsule.yaml 整份回写丢掉")
        logs = uc.read_worklogs(self.cap)
        self.assertEqual([w["summary"] for w in logs], ["持锁等待"])

    def test_lock_timeout_raises_tool_error_and_leaves_bytes_untouched(self) -> None:
        """锁被占满 10 秒：抛 ToolError 且 capsule.yaml 字节不变。"""
        before = (self.cap / "capsule.yaml").read_bytes()
        store = uc._manifest_store(self.cap / "capsule.yaml")
        self.assertTrue(store.acquire("squatter"))
        try:
            with self.assertRaises(uc.ToolError) as ctx:
                uc.log_time(self.cap, spent="1h", stage="misc", summary="超时应拒", operator="t")
            self.assertIn("写锁等待超时", str(ctx.exception))
        finally:
            store.release()
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before, "超时拒绝不得落盘")

    def test_public_write_functions_hold_the_lock(self) -> None:
        """每个公开写函数都在锁内执行：执行期间锁目录被本调用持有。"""
        seen: list[str] = []
        real = uc._capsule_lock

        @contextlib.contextmanager
        def spy(capsule):
            with real(capsule):
                seen.append(str(capsule))
                yield

        uc._capsule_lock = spy
        try:
            uc.start_task(self.cap, name="锁内开段", task_id="Tech-1")
            uc.touch_task(self.cap)
            # Manager 段结段的证据门槛与有效秒口径属 T8；本条只钉锁覆盖，
            # 旧语义结算路径用无 actor 旧段夹具。
            write_legacy_open_segment(self.cap, name="锁内旧段", seq=2, seg_id="Tech-1#2")
            uc.end_task(self.cap, at="2026-10-15T09:07:00+08:00")
            uc.log_time(self.cap, spent="1h", stage="misc", summary="锁内补登", operator="t")
            uc.override_worklog(self.cap, ref="Tech-1#2", spent="2h", reason="锁内覆盖")
            uc.update_capsule(str(self.cap))
            # 已 logged 段不接受撤段/修正（IMP-C3/IMP-M1）：取到锁后才被业务拒绝，仍计入覆盖
            for call in (lambda: uc.unmeasure_segment(self.cap, seg_id="Tech-1#2", reason="锁内撤段"),
                         lambda: uc.fix_segment(self.cap, seg_id="Tech-1#2",
                                                end_at="2026-10-15T10:00:00+08:00"),
                         lambda: uc.split_segment(self.cap, seg_id="Tech-1#2",
                                                  at=["2026-10-15T09:30:00+08:00"])):
                with self.assertRaises(uc.ToolError):
                    call()
        finally:
            uc._capsule_lock = real
        self.assertEqual(len(seen), 9, "全部写函数都应经 _capsule_lock 持锁执行")

    def test_lock_released_after_success_and_failure(self) -> None:
        """锁必须随 with 退出释放：成功与业务拒绝两条路径都不留锁。"""
        uc.log_time(self.cap, spent="1h", stage="misc", summary="成功路径", operator="t")
        self.assertFalse((self.cap / ".capsule.yaml.lock").exists(), "成功后锁须释放")
        # 五元组完全相同的第二条即重复登记（业务拒绝，非锁失败）
        with self.assertRaises(uc.ToolError) as ctx:
            uc.log_time(self.cap, spent="1h", stage="misc", summary="成功路径", operator="t")
        self.assertIn("已登记过", str(ctx.exception))
        self.assertFalse((self.cap / ".capsule.yaml.lock").exists(), "业务拒绝后锁须释放")
        # 无胶囊路径不取锁，由被装饰函数报「未找到胶囊清单」
        with self.assertRaises(uc.ToolError):
            uc.start_task(self.cap / "nope", name="无胶囊")
        self.assertFalse((self.cap / ".capsule.yaml.lock").exists(), "异常后锁须释放")


class ManagerSegmentTests(unittest.TestCase):
    """Tech-176 T7（方案 4.4 判定 7）：--start-task 只开 manager 段并记 baseline。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_Manager段"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")
        (self.cap / "02_方案.md").write_text("方案\n", encoding="utf-8")
        (self.cap / ".hidden.md").write_text("隐藏文件\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _start(self, **kw) -> dict:
        return uc.start_task(self.cap, name=kw.pop("name", "事务"), task_id="Tech-1", **kw)

    def test_open_role_segment_does_not_block_manager_segment(self) -> None:
        """(a) 有 open role 段时 start_task 成功。"""
        role_seg = {
            "seq": 1, "seg_id": "Tech-1#1", "name": "Architecture→02_方案.md",
            "startAt": "2026-10-15T09:00:00+08:00", "lastActivityAt": "2026-10-15T09:00:00+08:00",
            "state": "open", "actor": "role", "role": "Architecture",
            "dispatch_id": "a" * 32, "trigger": "dispatch", "stage": "02_方案",
        }
        self._write_manifest({"task_segments": [role_seg], "seq_next": 2})
        res = self._start()
        seg = res["segment"]
        self.assertEqual(seg["actor"], "manager")
        self.assertEqual(seg["state"], "open")
        self.assertEqual(seg["seg_id"], "Tech-1#2")

    def test_second_manager_segment_rejected(self) -> None:
        """(b) 已有 open Manager 段时第二次开段被拒。"""
        self._start(name="第一个 Manager 段")
        with self.assertRaises(uc.ToolError) as ctx:
            self._start(name="第二个 Manager 段")
        self.assertIn("Manager", str(ctx.exception))

    def test_open_legacy_segment_blocks_manager_segment(self) -> None:
        """方案 4.4 判定 7：已有 open 旧段（无 actor）同样拒绝开段。"""
        self._write_manifest({"task_segments": [self._legacy_open()], "seq_next": 2})
        with self.assertRaises(uc.ToolError) as ctx:
            self._start()
        self.assertIn("Tech-1#1", str(ctx.exception))

    def test_legacy_segment_settles_with_old_semantics(self) -> None:
        """(c) 旧段（无 actor）用 --end-task --at … --evidence <文本> 按旧语义结算并写 seconds。"""
        self._write_manifest({"task_segments": [self._legacy_open()], "seq_next": 2})
        res = uc.end_task(self.cap, at="2026-10-15T09:07:00+08:00",
                          evidence="会话中断于 10-15 09:07，提交 3f2a91c")
        seg = res["segment"]
        self.assertEqual(seg["state"], "logged")
        self.assertEqual(seg["machine_seconds"], 420)
        self.assertNotIn("effective_seconds", seg, "旧段不写有效秒")
        self.assertEqual(seg["evidence"], "会话中断于 10-15 09:07，提交 3f2a91c")
        entry = res["entry"]
        self.assertEqual(entry["seconds"], 420)
        self.assertNotIn("actor", entry, "旧段结算不推断执行主体")

    def test_baseline_snapshots_capsule_files_only(self) -> None:
        """(d) baseline 只含胶囊内文件指纹，不含 capsule.yaml 与点开头文件。"""
        res = self._start()
        baseline = res["segment"]["baseline"]
        self.assertEqual(set(baseline), {"02_方案.md"}, f"baseline 应只含可见的胶囊文件: {baseline}")
        self.assertEqual(baseline["02_方案.md"],
                         hashlib.sha256((self.cap / "02_方案.md").read_bytes()).hexdigest()[:16])
        self.assertNotIn("capsule.yaml", baseline)
        self.assertNotIn(".hidden.md", baseline)

    def test_baseline_excludes_subdirs_and_missing_files(self) -> None:
        """baseline 只收文件（不收空目录），递归相对路径带子目录名。"""
        (self.cap / "sub").mkdir()
        (self.cap / "sub" / "01_调研.md").write_text("调研\n", encoding="utf-8")
        (self.cap / "sub" / ".dot.md").write_text("点开头\n", encoding="utf-8")
        baseline = self._start()["segment"]["baseline"]
        self.assertEqual(set(baseline), {"02_方案.md", "sub/01_调研.md"})

    def test_capsule_write_refreshes_manager_last_activity(self) -> None:
        """方案 4.4 判定 7「7 工具事件」：任何写胶囊的动作都刷新 open Manager 段的
        lastActivityAt——调度器写 manifest 也走这条路，否则 Manager 段超过 600 秒
        未 --touch 就一律作废（实测缺陷）。"""
        self._start()
        segs = {s["seg_id"]: s for s in uc.read_segments(self.cap)}
        mgr_id = next(k for k, v in segs.items() if v.get("actor") == "manager")
        stale = "2026-10-15T09:00:00+08:00"
        uc.touch_task(self.cap, at=stale)
        segs = {s["seg_id"]: s for s in uc.read_segments(self.cap)}
        self.assertIn(stale, segs[mgr_id]["lastActivityAt"])

        # 非 --touch 的写路径（log_time 补登）同样是一次工具事件
        uc.log_time(self.cap, "0.5", stage="misc", summary="补登")
        segs = {s["seg_id"]: s for s in uc.read_segments(self.cap)}
        refreshed = segs[mgr_id]["lastActivityAt"]
        self.assertNotIn(stale, refreshed, "写胶囊后 Manager 段活跃时间须被刷新")

        # 调度写段（T10 的开段）同样刷新
        uc.touch_task(self.cap, at=stale)
        uc.start_dispatch_segment(self.cap, dispatch_id="b" * 32, role="Architecture",
                                  deliverable="02_方案.md")
        segs = {s["seg_id"]: s for s in uc.read_segments(self.cap)}
        self.assertNotIn(stale, segs[mgr_id]["lastActivityAt"],
                         "调度开段也是一次工具事件")

    def test_touch_refreshes_manager_segment_only(self) -> None:
        """方案 4.4 判定 7 末行：_touch_open_segment 只刷新 Manager 段或旧段，不碰 role 段。"""
        role_seg = {
            "seq": 1, "seg_id": "Tech-1#1", "name": "Architecture→02_方案.md",
            "startAt": "2026-10-15T09:00:00+08:00", "lastActivityAt": "2026-10-15T09:00:00+08:00",
            "state": "open", "actor": "role", "role": "Architecture",
            "dispatch_id": "a" * 32, "trigger": "dispatch", "stage": "02_方案",
        }
        self._write_manifest({"task_segments": [role_seg], "seq_next": 2})
        self._start()
        uc.touch_task(self.cap, at="2026-10-15T09:30:00+08:00")
        segs = {s["seg_id"]: s for s in uc.read_segments(self.cap)}
        self.assertIn("09:30:00", segs["Tech-1#2"]["lastActivityAt"], "Manager 段应被刷新")
        self.assertIn("09:00:00", segs["Tech-1#1"]["lastActivityAt"], "open role 段不得被刷新")

    def test_touch_without_manager_segment_is_noop(self) -> None:
        """只有 open role 段时 --touch 不写盘（Manager 段开段前 T9 才接线刷新 role 段）。"""
        role_seg = {
            "seq": 1, "seg_id": "Tech-1#1", "name": "Architecture→02_方案.md",
            "startAt": "2026-10-15T09:00:00+08:00", "lastActivityAt": "2026-10-15T09:00:00+08:00",
            "state": "open", "actor": "role", "role": "Architecture",
            "dispatch_id": "a" * 32, "trigger": "dispatch", "stage": "02_方案",
        }
        self._write_manifest({"task_segments": [role_seg], "seq_next": 2})
        before = (self.cap / "capsule.yaml").read_bytes()
        res = uc.touch_task(self.cap, at="2026-10-15T09:30:00+08:00")
        self.assertEqual(res["status"], "no_open_segment")
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    def _legacy_open(self) -> dict:
        return {"seq": 1, "seg_id": "Tech-1#1", "name": "旧规则开的段",
                "startAt": "2026-10-15T09:00:00+08:00", "lastActivityAt": "2026-10-15T09:00:00+08:00",
                "state": "open"}

    def _write_manifest(self, data: dict) -> None:
        import yaml
        payload = {"id": "Tech-1", "lifecycle": "active", **data}
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


class ManagerEndSegmentTests(unittest.TestCase):
    """Tech-176 T8（方案 4.4 判定 7、4.4.2）：Manager 结段的证据门槛、尾空档截断与扣 role 段重叠。

    固定时钟注入：monkeypatch `dt.datetime.now` 让「现在」可预测，尾空档与区间秒断言
    才不会随真实时间漂移。
    """

    CST = dt.timezone(dt.timedelta(hours=8))

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_Manager结段"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")
        (self.cap / "02_方案.md").write_text("方案\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    # ---------- 夹具 ----------

    def _write_manifest(self, data: dict) -> None:
        import yaml
        payload = {"id": "Tech-1", "lifecycle": "active", **data}
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _start(self, *, at: str = "2026-10-15T09:00:00+08:00",
               last_activity: str | None = None, **kw) -> dict:
        res = uc.start_task(self.cap, name="事务", task_id="Tech-1", at=at, **kw)
        if last_activity:
            self._write_manifest({"task_segments": uc.read_segments(self.cap),
                                  "worklogs": [], "seq_next": 2})
            # 直接改 lastActivityAt 以模拟「最后一次工具事件」与开段时刻分离
            import yaml
            data = yaml.safe_load((self.cap / "capsule.yaml").read_text(encoding="utf-8"))
            data["task_segments"][-1]["lastActivityAt"] = last_activity
            (self.cap / "capsule.yaml").write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return res

    def _freeze(self, now_iso: str):
        """把 update_capsule 的「现在」固定到 now_iso：替换模块内的 dt 引用。

        只覆盖本测试用到的 now / timezone，其余 datetime 语义仍走真实 datetime，
        避免 mock 整个模块把 normalize_to_cst 等路径一起换掉。
        """
        fixed = dt.datetime.fromisoformat(now_iso)

        class _FrozenDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        fake = mock.Mock(wraps=uc.dt)
        fake.datetime = _FrozenDatetime
        return mock.patch.object(uc, "dt", fake)

    def _evidence(self, name: str = "04_Spec_Tech-1.md", body: str = "产出\n") -> Path:
        p = self.cap / name
        p.write_text(body, encoding="utf-8")
        return p

    def _worklogs(self) -> list[dict]:
        return uc.read_worklogs(self.cap)

    def _segments(self) -> dict:
        return {s["seg_id"]: s for s in uc.read_segments(self.cap)}

    # ---------- (a)/(b) 证据门槛 ----------

    def test_missing_evidence_unmeasured(self) -> None:
        """(a) 不传 --evidence → unmeasured、无 worklog、退出码 0。"""
        self._start()
        before = (self.cap / "capsule.yaml").read_bytes()
        res = uc.end_task(self.cap)
        seg = res["segment"]
        self.assertEqual(seg["state"], "unmeasured")
        self.assertIn("未带 --evidence", seg["note"])
        self.assertEqual(self._worklogs(), [])
        self.assertEqual(res["worklog_refs"], [])
        self.assertNotEqual((self.cap / "capsule.yaml").read_bytes(), before, "作废须落盘")

    def test_unchanged_evidence_unmeasured(self) -> None:
        """(b) 证据文件指纹与 baseline 相同 → unmeasured。"""
        self._start()
        with self._freeze("2026-10-15T09:30:00+08:00"):
            res = uc.end_task(self.cap, evidence="02_方案.md")
        self.assertEqual(res["segment"]["state"], "unmeasured")
        self.assertIn("指纹", res["segment"]["note"])
        self.assertEqual(self._worklogs(), [])

    def test_evidence_outside_capsule_unmeasured(self) -> None:
        """证据路径逃出胶囊（../x.md）→ unmeasured，不读胶囊外文件。"""
        (self._td.name + "/outside.md").write_text("x", encoding="utf-8") if False else None
        outside = Path(self._td.name) / "outside.md"
        outside.write_text("胶囊外\n", encoding="utf-8")
        self._start()
        res = uc.end_task(self.cap, evidence="../outside.md")
        self.assertEqual(res["segment"]["state"], "unmeasured")
        self.assertIn("无效", res["segment"]["note"])

    def test_missing_evidence_file_unmeasured(self) -> None:
        """证据文件不存在 → unmeasured。"""
        self._start()
        res = uc.end_task(self.cap, evidence="07_验收报告.md")
        self.assertEqual(res["segment"]["state"], "unmeasured")
        self.assertIn("无效", res["segment"]["note"])

    def test_new_evidence_logs_manager_worklog(self) -> None:
        """(c) 新证据文件 → logged、actor=manager、evidence_sha256、effective_seconds。"""
        self._start(last_activity="2026-10-15T09:30:00+08:00")
        self._evidence()
        with self._freeze("2026-10-15T09:30:00+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        seg = res["segment"]
        self.assertEqual(seg["state"], "logged")
        self.assertEqual(seg["machine_seconds"], 1800)
        self.assertEqual(seg["effective_seconds"], 1800)
        self.assertEqual(seg["actor"], "manager")
        self.assertEqual(seg["evidence"], "04_Spec_Tech-1.md")
        self.assertRegex(seg["evidence_sha256"], r"^[0-9a-f]{16}$")
        self.assertEqual(seg["evidence_sha256"],
                         hashlib.sha256((self.cap / "04_Spec_Tech-1.md").read_bytes()).hexdigest()[:16])
        entry = res["entry"]
        self.assertEqual(entry["actor"], "manager")
        self.assertNotIn("role", entry)
        self.assertEqual(entry["seconds"], 1800)
        self.assertEqual(entry["spent"], 0.5)
        self.assertEqual(len(self._worklogs()), 1)

    # ---------- (d) 扣 role 段重叠 ----------

    def test_role_overlap_subtracted(self) -> None:
        """(d) 30m 段内扣掉 10m role 段 → effective_seconds=1200，machine_seconds=1800。"""
        # 先开 Manager 段（baseline 记 02_方案.md），再造 role 段与证据
        mgr_id = self._start(at="2026-10-15T09:00:00+08:00",
                             last_activity="2026-10-15T09:30:00+08:00")["segment"]["seg_id"]
        mgr = self._segments()[mgr_id]  # _start 已把 lastActivityAt 改到 09:30，重读生效值
        role_seg = {"seq": 2, "seg_id": "Tech-1#2", "name": "Architecture→02_方案.md",
                    "startAt": "2026-10-15T09:10:00+08:00", "lastActivityAt": "2026-10-15T09:20:00+08:00",
                    "endAt": "2026-10-15T09:20:00+08:00", "state": "logged", "actor": "role",
                    "role": "Architecture", "dispatch_id": "a" * 32, "trigger": "dispatch",
                    "stage": "02_方案", "machine_seconds": 600, "effective_seconds": 600}
        entry = {"date": "2026-10-15", "stage": "02_方案", "spent": 0.1667, "seconds": 600,
                 "summary": "角色段", "operator": "t", "ref": "Tech-1#2", "actor": "role",
                 "role": "Architecture"}
        segs = [s for s in uc.read_segments(self.cap) if s["seg_id"] != mgr_id]
        self._write_manifest({"task_segments": [role_seg, mgr],
                              "worklogs": [entry], "seq_next": 4})
        before_role = self._segments()["Tech-1#2"]
        self._evidence()
        with self._freeze("2026-10-15T09:30:00+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        seg = res["segment"]
        self.assertEqual(seg["seg_id"], mgr_id)
        self.assertEqual(seg["machine_seconds"], 1800)
        self.assertEqual(seg["effective_seconds"], 1200)
        self.assertEqual(res["entry"]["seconds"], 1200)
        self.assertEqual(len(self._worklogs()), 2, "role 段原有 worklog 不得被改动")
        after = self._segments()
        self.assertEqual(after["Tech-1#2"]["state"], "logged")
        self.assertEqual(after["Tech-1#2"]["machine_seconds"], 600)
        self.assertEqual(after["Tech-1#2"], before_role, "role 段事实层不得被 Manager 结段改动")

    def test_fully_covered_by_role_segments_unmeasured(self) -> None:
        """(f) Manager 段完全被 role 段覆盖 → unmeasured。"""
        mgr_id = self._start(at="2026-10-15T09:00:00+08:00",
                             last_activity="2026-10-15T09:30:00+08:00")["segment"]["seg_id"]
        mgr = self._segments()[mgr_id]  # _start 已把 lastActivityAt 改到 09:30，重读生效值
        role_seg = {"seq": 2, "seg_id": "Tech-1#2", "name": "Designer→03_设计.md",
                    "startAt": "2026-10-15T09:00:00+08:00", "lastActivityAt": "2026-10-15T09:30:00+08:00",
                    "endAt": "2026-10-15T09:30:00+08:00", "state": "logged", "actor": "role",
                    "role": "Designer", "dispatch_id": "b" * 32, "trigger": "dispatch",
                    "stage": "03_设计", "machine_seconds": 1800, "effective_seconds": 1800}
        entry = {"date": "2026-10-15", "stage": "03_设计", "spent": 0.5, "seconds": 1800,
                 "summary": "角色段", "operator": "t", "ref": "Tech-1#2", "actor": "role",
                 "role": "Designer"}
        segs = [s for s in uc.read_segments(self.cap) if s["seg_id"] != mgr_id]
        self._write_manifest({"task_segments": [role_seg, mgr],
                              "worklogs": [entry], "seq_next": 4})
        self._evidence()
        with self._freeze("2026-10-15T09:30:00+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        self.assertEqual(res["segment"]["state"], "unmeasured")
        self.assertIn("重叠", res["segment"]["note"])
        self.assertEqual([w for w in self._worklogs() if w.get("actor") != "role"], [])
        self.assertNotIn("machine_seconds", res["segment"])

    def test_open_role_segment_overlap_counts(self) -> None:
        """未收尾的 open role 段也计入重叠（无 endAt 视为持续到结段时刻）。"""
        mgr_id = self._start(at="2026-10-15T09:00:00+08:00",
                             last_activity="2026-10-15T09:20:00+08:00")["segment"]["seg_id"]
        mgr = self._segments()[mgr_id]  # _start 已把 lastActivityAt 改到 09:20，重读生效值
        role_seg = {"seq": 2, "seg_id": "Tech-1#2", "name": "Researcher→01_调研.md",
                    "startAt": "2026-10-15T09:10:00+08:00", "lastActivityAt": "2026-10-15T09:15:00+08:00",
                    "state": "open", "actor": "role", "role": "Researcher",
                    "dispatch_id": "c" * 32, "trigger": "dispatch", "stage": "01_调研"}
        segs = [s for s in uc.read_segments(self.cap) if s["seg_id"] != mgr_id]
        self._write_manifest({"task_segments": [role_seg, mgr],
                              "worklogs": [], "seq_next": 4})
        self._evidence()
        with self._freeze("2026-10-15T09:20:00+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        self.assertEqual(res["segment"]["seg_id"], mgr_id)
        # 区间 09:00–09:20 共 1200s，open role 段 09:10 起无 endAt 视为持续到 end → 重叠 600s
        self.assertEqual(res["segment"]["effective_seconds"], 600)

    def test_sub_second_span_without_role_segments_reports_zero_span(self) -> None:
        """IM-6：区间四舍五入为 0 秒且无 role 段 → 诊断写「不足 1 秒」，不误报角色重叠。"""
        self._start(at="2026-10-15T09:00:00+08:00",
                    last_activity="2026-10-15T09:00:00+08:00")
        self._evidence()
        # 正数 100ms 舍入到 0 秒：没有任何 role 段，真实原因是零区间
        with self._freeze("2026-10-15T09:00:00.100000+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        seg = res["segment"]
        self.assertEqual(seg["state"], "unmeasured", "零区间不入账")
        self.assertIn("不足 1 秒", seg["note"])
        self.assertNotIn("重叠", seg["note"], "无 role 段时不得误报为角色调度重叠")
        self.assertEqual(self._worklogs(), [])
        self.assertNotIn("machine_seconds", seg)

    # ---------- (e) 尾部空档 ----------

    def test_idle_gap_over_threshold_truncates_to_last_activity(self) -> None:
        """(e) 尾空档 2h（>600s）→ 结束时刻取最后一次工具事件。"""
        self._start(last_activity="2026-10-15T09:30:00+08:00")
        self._evidence()
        with self._freeze("2026-10-15T11:30:00+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        seg = res["segment"]
        self.assertIn("09:30:00", seg["endAt"])
        self.assertEqual(seg["machine_seconds"], 1800)
        self.assertEqual(seg["effective_seconds"], 1800)

    def test_idle_gap_within_threshold_takes_now(self) -> None:
        """(e) 尾空档 5m（≤600s）→ 结束时刻取 now。"""
        self._start(last_activity="2026-10-15T09:25:00+08:00")
        self._evidence()
        with self._freeze("2026-10-15T09:30:00+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        seg = res["segment"]
        self.assertIn("09:30:00", seg["endAt"])
        self.assertEqual(seg["machine_seconds"], 1800)

    # ---------- (g) 拒绝 --at ----------

    def test_manager_segment_rejects_at(self) -> None:
        """(g) 对 Manager 段传 --at → 退出码 1、文件字节不变。"""
        self._start()
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError):
            uc.end_task(self.cap, at="2026-10-15T09:30:00+08:00", evidence="02_方案.md")
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    def test_role_segment_rejected_by_end_task(self) -> None:
        """方案 4.4.1：--end-task 拒绝结算 open role 段（退出码 1）。"""
        role_seg = {"seq": 2, "seg_id": "Tech-1#2", "name": "Architecture→02_方案.md",
                    "startAt": "2026-10-15T09:00:00+08:00", "lastActivityAt": "2026-10-15T09:00:00+08:00",
                    "state": "open", "actor": "role", "role": "Architecture",
                    "dispatch_id": "d" * 32, "trigger": "dispatch", "stage": "02_方案"}
        self._write_manifest({"task_segments": [role_seg], "worklogs": [], "seq_next": 3})
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc.end_task(self.cap, at="2026-10-15T09:07:00+08:00")
        self.assertIn("actor=role", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    def test_long_manager_segment_pending_approval(self) -> None:
        """门槛按 effective_seconds 判定（4.4.2）：>8h 的 Manager 段待批，不写 worklog。"""
        self._start(last_activity="2026-10-15T18:00:00+08:00")
        self._evidence()
        with self._freeze("2026-10-15T18:00:00+08:00"):
            res = uc.end_task(self.cap, evidence="04_Spec_Tech-1.md")
        seg = res["segment"]
        self.assertEqual(seg["state"], "pending_approval")
        self.assertEqual(seg["effective_seconds"], 32400)
        self.assertEqual(res["worklog_refs"], [])
        self.assertEqual(self._worklogs(), [])

    def test_cli_unmeasured_prints_warning_and_exits_zero(self) -> None:
        """D-4：无证据结段在 CLI 上退出码 0 并打印 ⚠️。"""
        self._start()
        proc = subprocess.run([sys.executable, str(TOOL), str(self.cap), "--end-task"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("⚠️", proc.stdout)
        self.assertEqual([s["state"] for s in uc.read_segments(self.cap)], ["unmeasured"])

    def test_cli_manager_at_rejected_exit_one(self) -> None:
        """CLI：Manager 段 --end-task --at → 退出码 1 且字节不变。"""
        self._start()
        before = (self.cap / "capsule.yaml").read_bytes()
        proc = subprocess.run([sys.executable, str(TOOL), str(self.cap), "--end-task",
                               "--at", "2026-10-15T09:30:00+08:00", "--evidence", "02_方案.md"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)


class SettlementActorPolicyTests(unittest.TestCase):
    """Tech-176 T8b（方案 4.4.1，PA-1）：既有结算入口的 actor 政策。

    一张表定死（4.4.1）：旧段无 actor → 旧语义按秒；Manager 段不绕过证据门槛
    （`--delivered` 转 unmeasured 不入账，D-14）；role 段不由本类入口结算
    （拒绝退出码 1、字节不变，或 `--delivered` 不碰）。
    """

    CST = dt.timezone(dt.timedelta(hours=8))

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_actor政策"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")
        (self.cap / "02_方案.md").write_text("方案\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    # ---------- 夹具 ----------

    def _write_manifest(self, data: dict) -> None:
        import yaml
        payload = {"id": "Tech-1", "lifecycle": "active", "worklogs": [], **data}
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _append_segment(self, seg: dict) -> dict:
        import yaml
        data = yaml.safe_load((self.cap / "capsule.yaml").read_text(encoding="utf-8"))
        segs = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
        seq = 1 + max([int(s.get("seq", 0)) for s in segs], default=0)
        seg = {"seq": seq, "seg_id": f"Tech-1#{seq}", **seg}
        segs.append(seg)
        data["task_segments"] = segs
        data["seq_next"] = seq + 1
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return seg

    def _open_role_segment(self, dispatch_id: str = "a" * 32, role: str = "Designer",
                           deliverable: str = "03_设计.md",
                           at: str = "2026-10-15T09:00:00+08:00") -> dict:
        """open 的 role 段（固定开始时刻，避免依赖真实 now）。"""
        return self._append_segment({
            "name": f"{role}→{deliverable}", "startAt": at, "lastActivityAt": at,
            "state": "open", "actor": "role", "role": role, "dispatch_id": dispatch_id,
            "trigger": "dispatch", "stage": uc._dispatch_stage(role, deliverable)})

    def _freeze(self, now_iso: str):
        fixed = dt.datetime.fromisoformat(now_iso)

        class _FrozenDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        fake = mock.Mock(wraps=uc.dt)
        fake.datetime = _FrozenDatetime
        return mock.patch.object(uc, "dt", fake)

    def _worklogs(self) -> list[dict]:
        return uc.read_worklogs(self.cap)

    def _segments(self) -> dict:
        return {s["seg_id"]: s for s in uc.read_segments(self.cap)}

    # ---------- (a) PA-1 三场景之一：--delivered 遇 open Manager 段 ----------

    def test_delivered_open_manager_segment_becomes_unmeasured(self) -> None:
        """(a) 无证据的 open manager 段 + --delivered → unmeasured、无新 worklog、lifecycle 仍推进。"""
        uc.start_task(self.cap, name="事务", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        res = uc.update_capsule(str(self.cap), delivered="2026-10-15")
        seg = self._segments()["Tech-1#1"]
        self.assertEqual(seg["state"], "unmeasured")
        self.assertIn("--delivered 收尾", seg["note"])
        self.assertIn("未带证据结段", seg["note"])
        self.assertEqual(self._worklogs(), [], "不入账：不得写 worklog")
        self.assertEqual(res["worklog_refs"], [])
        self.assertEqual(res["to"], "delivered", "胶囊 lifecycle 仍推进到 delivered")

    def test_delivered_open_manager_and_logged_role_segment_keeps_role(self) -> None:
        """(b) open manager 段与 logged role 段并存 + --delivered → manager 段 unmeasured，role 段与 worklog 不变。"""
        role_seg = self._open_role_segment()
        with self._freeze("2026-10-15T09:20:00+08:00"):
            uc.end_dispatch_segment("a" * 32, True, capsule=self.cap)
        manager_seg = uc.start_task(self.cap, name="事务", task_id="Tech-1",
                                    at="2026-10-15T09:05:00+08:00")["segment"]
        before_worklogs = self._worklogs()
        self.assertEqual(len(before_worklogs), 1)
        before_role = dict(self._segments()[role_seg["seg_id"]])

        res = uc.update_capsule(str(self.cap), delivered="2026-10-15")
        segs = self._segments()
        self.assertEqual(segs[manager_seg["seg_id"]]["state"], "unmeasured")
        self.assertEqual(segs[role_seg["seg_id"]]["state"], "logged", "role 段不被 --delivered 触碰")
        self.assertEqual(segs[role_seg["seg_id"]]["endAt"], before_role["endAt"])
        self.assertEqual(segs[role_seg["seg_id"]]["machine_seconds"], before_role["machine_seconds"])
        self.assertEqual(self._worklogs(), before_worklogs, "role 段的 worklog 不变、无新 worklog")
        self.assertEqual(res["to"], "delivered")

    def test_delivered_two_open_role_segments_untouched(self) -> None:
        """(c) 两个 open role 段（d1、d2）+ --delivered → 两段仍 open、无新 worklog。"""
        d1 = self._open_role_segment("a" * 32, at="2026-10-15T09:00:00+08:00")
        d2 = self._open_role_segment("b" * 32, at="2026-10-15T09:10:00+08:00")
        res = uc.update_capsule(str(self.cap), delivered="2026-10-15")
        segs = self._segments()
        self.assertEqual(segs[d1["seg_id"]]["state"], "open")
        self.assertEqual(segs[d2["seg_id"]]["state"], "open")
        self.assertEqual(self._worklogs(), [])
        self.assertEqual(res["worklog_refs"], [])
        self.assertEqual(res["to"], "delivered", "交付照常推进")

    def test_delivered_open_legacy_segment_settles_in_seconds(self) -> None:
        """(d) open 旧段（无 actor）+ --delivered → 按秒结算（现行语义）。"""
        write_legacy_open_segment(self.cap, name="旧规则开的段")
        with self._freeze("2026-10-15T09:07:00+08:00"):
            res = uc.update_capsule(str(self.cap), delivered="2026-10-15")
        seg = self._segments()["Tech-1#1"]
        self.assertEqual(seg["state"], "logged")
        self.assertEqual(seg["machine_seconds"], 420, "7 分钟按秒结算，不取整到 0.25h")
        self.assertEqual(seg["endAt"], "2026-10-15T09:07:00+08:00")
        self.assertEqual(len(self._worklogs()), 1)
        self.assertEqual(self._worklogs()[0]["seconds"], 420)
        self.assertEqual(res["worklog_refs"], ["Tech-1#1"])
        self.assertEqual(res["to"], "delivered")

    # ---------- (e) role 段被各入口拒绝：退出码 1、字节不变 ----------

    def test_role_segment_rejected_by_fix_and_split(self) -> None:
        """(e) --fix-segment / --split-segment 指向 role 段 → 退出码 1、文件字节不变。"""
        role_seg = self._open_role_segment()
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc.fix_segment(self.cap, seg_id=role_seg["seg_id"],
                           end_at="2026-10-15T10:00:00+08:00")
        self.assertIn("actor=role", str(ctx.exception))
        with self.assertRaises(uc.ToolError) as ctx2:
            uc.split_segment(self.cap, seg_id=role_seg["seg_id"],
                             at=["2026-10-15T09:30:00+08:00"])
        self.assertIn("actor=role", str(ctx2.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    def test_role_segment_rejected_by_end_task(self) -> None:
        """(e) --end-task 指向 role 段 → 退出码 1、字节不变（残留段用 --unmeasure-segment）。"""
        role_seg = self._open_role_segment()
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc.end_task(self.cap)
        self.assertIn("actor=role", str(ctx.exception))
        self.assertIn("--unmeasure-segment", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    def test_open_role_segment_rejected_by_approve(self) -> None:
        """(e) open 的 role 段被 --approve-segment 拒绝（AC-15：无 dispatch_id 身份不结算 open role 段）。"""
        role_seg = self._open_role_segment()
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc.approve_segment(self.cap, seg_id=role_seg["seg_id"])
        self.assertIn("actor=role", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    def test_pending_role_segment_approved_by_effective_seconds(self) -> None:
        """PA-1：已由调度结为 pending_approval 的 role 段按其 effective_seconds 批准入账（方案 4.4.1:200）。"""
        role_seg = self._open_role_segment()
        import yaml
        data = yaml.safe_load((self.cap / "capsule.yaml").read_text(encoding="utf-8"))
        data["task_segments"][0]["state"] = "pending_approval"
        data["task_segments"][0]["effective_seconds"] = 30000
        data["task_segments"][0]["machine_seconds"] = 32400
        data["task_segments"][0]["endAt"] = "2026-10-15T18:00:00+08:00"
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        res = uc.approve_segment(self.cap, seg_id=role_seg["seg_id"])
        self.assertEqual(res["entry"]["seconds"], 30000, "入账读段上 effective_seconds")
        self.assertEqual(res["entry"]["spent"], round(30000 / 3600, 4))
        self.assertEqual(res["entry"]["actor"], "role")
        self.assertEqual(res["entry"]["role"], "Designer")
        self.assertEqual(res["segment"]["state"], "logged")
        self.assertEqual(res["segment"]["machine_seconds"], 32400, "机时保持事实值")
        self.assertEqual(uc.read_worklogs(self.cap)[0]["ref"], role_seg["seg_id"])

    def test_pending_role_segment_approved_when_wall_clock_exceeds_24h(self) -> None:
        """IM-1：role 段墙钟 >24h 但有效秒 ≤24h → 门槛按有效秒判定，批准通过。"""
        role_seg = self._open_role_segment()
        import yaml
        data = yaml.safe_load((self.cap / "capsule.yaml").read_text(encoding="utf-8"))
        data["task_segments"][0].update(
            startAt="2026-10-15T00:00:00+08:00", endAt="2026-10-16T01:00:00+08:00",
            lastActivityAt="2026-10-16T01:00:00+08:00",
            state="pending_approval", machine_seconds=90000, effective_seconds=32400)
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        res = uc.approve_segment(self.cap, seg_id=role_seg["seg_id"])
        self.assertEqual(res["entry"]["seconds"], 32400)
        self.assertEqual(res["segment"]["state"], "logged")

    def test_approve_effective_over_24h_rejected(self) -> None:
        """IM-1：有效秒 >24h 仍按有效秒拒绝（不按墙钟放行）。"""
        role_seg = self._open_role_segment()
        import yaml
        data = yaml.safe_load((self.cap / "capsule.yaml").read_text(encoding="utf-8"))
        data["task_segments"][0].update(
            startAt="2026-10-15T00:00:00+08:00", endAt="2026-10-16T01:00:00+08:00",
            lastActivityAt="2026-10-16T01:00:00+08:00",
            state="pending_approval", machine_seconds=90000, effective_seconds=90000)
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc.approve_segment(self.cap, seg_id=role_seg["seg_id"])
        self.assertIn("24h", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    # ---------- (f) manager 段被 split / open 被 fix 拒绝 ----------

    def test_manager_segment_rejected_by_split(self) -> None:
        """(f) --split-segment 指向 manager 段 → 退出码 1、字节不变。"""
        uc.start_task(self.cap, name="事务", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc.split_segment(self.cap, seg_id="Tech-1#1",
                             at=["2026-10-15T09:30:00+08:00"])
        self.assertIn("manager", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    def test_open_manager_segment_rejected_by_fix(self) -> None:
        """(f) open 的 Manager 段被 --fix-segment 拒绝（提示用 --end-task --evidence）。"""
        uc.start_task(self.cap, name="事务", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc.fix_segment(self.cap, seg_id="Tech-1#1", end_at="2026-10-15T10:00:00+08:00")
        self.assertIn("--end-task --evidence", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)

    # ---------- (g) approve 读段上 effective_seconds，不按墙钟重算 ----------

    def test_approve_manager_segment_uses_effective_seconds(self) -> None:
        """(g) pending_approval 的 manager 段（effective_seconds=30000）→ worklog seconds=30000，不按墙钟重算。"""
        self._append_segment({
            "name": "长事务", "startAt": "2026-10-15T09:00:00+08:00",
            "lastActivityAt": "2026-10-15T19:00:00+08:00", "endAt": "2026-10-15T19:00:00+08:00",
            "state": "pending_approval", "actor": "manager", "trigger": "manager",
            "stage": "misc", "machine_seconds": 36000, "effective_seconds": 30000,
            "evidence": "04_Spec_Tech-1.md", "evidence_sha256": "a" * 16,
            "note": "超过 8h（10:00:00）"})
        res = uc.approve_segment(self.cap, seg_id="Tech-1#1")
        self.assertEqual(res["entry"]["seconds"], 30000)
        self.assertEqual(res["entry"]["spent"], round(30000 / 3600, 4))
        self.assertEqual(res["entry"]["actor"], "manager")
        self.assertEqual(res["segment"]["machine_seconds"], 36000, "机时不按墙钟重写")
        self.assertEqual(res["segment"]["effective_seconds"], 30000)
        self.assertEqual(uc.read_worklogs(self.cap)[0]["ref"], "Tech-1#1")

    def test_approve_manager_segment_wall_clock_over_24h_but_effective_legal(self) -> None:
        """IM-1：Manager 段墙钟 25h、有效 9h（扣 16h role 重叠）→ 批准通过，入账读有效秒。"""
        import yaml
        role_seg = {"seq": 1, "seg_id": "Tech-1#1", "name": "Designer→03_设计.md",
                    "startAt": "2026-10-15T00:00:00+08:00", "lastActivityAt": "2026-10-15T16:00:00+08:00",
                    "endAt": "2026-10-15T16:00:00+08:00", "state": "logged", "actor": "role",
                    "role": "Designer", "dispatch_id": "a" * 32, "trigger": "dispatch",
                    "stage": "03_设计", "machine_seconds": 57600, "effective_seconds": 57600}
        role_entry = {"date": "2026-10-15", "stage": "03_设计", "spent": 16.0, "seconds": 57600,
                      "summary": "角色段", "operator": "t", "ref": "Tech-1#1",
                      "actor": "role", "role": "Designer"}
        mgr = {"seq": 2, "seg_id": "Tech-1#2", "name": "长事务",
               "startAt": "2026-10-15T00:00:00+08:00", "lastActivityAt": "2026-10-16T01:00:00+08:00",
               "endAt": "2026-10-16T01:00:00+08:00", "state": "pending_approval", "actor": "manager",
               "trigger": "manager", "stage": "misc", "machine_seconds": 90000,
               "effective_seconds": 32400, "evidence": "04_Spec_Tech-1.md",
               "evidence_sha256": "a" * 16, "note": "超过 8h（9:00:00）"}
        self._write_manifest({"task_segments": [role_seg, mgr], "worklogs": [role_entry], "seq_next": 3})
        res = uc.approve_segment(self.cap, seg_id="Tech-1#2")
        self.assertEqual(res["entry"]["seconds"], 32400)
        self.assertEqual(res["segment"]["machine_seconds"], 90000, "机时保持事实值，不为通过审批伪改")
        self.assertEqual(res["segment"]["state"], "logged")

    def test_fix_manager_pending_segment_recomputes_effective(self) -> None:
        """(g) pending_approval 的 manager 段 --fix-segment → 按 4.4 步骤②重算 effective_seconds 并入账。"""
        role_seg = self._open_role_segment(at="2026-10-15T09:00:00+08:00")
        import yaml
        with self._freeze("2026-10-15T09:30:00+08:00"):
            uc.end_dispatch_segment("a" * 32, True, capsule=self.cap)
        self._append_segment({
            "name": "长事务", "startAt": "2026-10-15T09:00:00+08:00",
            "lastActivityAt": "2026-10-15T19:00:00+08:00", "endAt": "2026-10-15T19:00:00+08:00",
            "state": "pending_approval", "actor": "manager", "trigger": "manager",
            "stage": "misc", "machine_seconds": 36000, "effective_seconds": 33000,
            "evidence": "04_Spec_Tech-1.md", "evidence_sha256": "a" * 16})
        res = uc.fix_segment(self.cap, seg_id="Tech-1#2",
                             end_at="2026-10-15T11:00:00+08:00")
        seg = res["segment"]
        self.assertEqual(seg["state"], "logged")
        self.assertEqual(seg["machine_seconds"], 7200)
        # 区间 09:00→11:00 = 7200s，扣除 role 段（09:00→09:30）1800s → 5400s
        self.assertEqual(seg["effective_seconds"], 5400)
        self.assertEqual(res["entry"]["seconds"], 5400)
        self.assertEqual(seg["evidence_sha256"], "a" * 16, "证据沿用段上已校验的指纹")
        self.assertEqual(self._segments()[role_seg["seg_id"]]["state"], "logged")

    def test_legacy_segment_rejected_by_nothing_new(self) -> None:
        """旧段（无 actor）在各入口仍按旧语义（split/fix 可用），不被 actor 政策拦截。"""
        write_legacy_open_segment(self.cap, name="旧段")
        res = uc.fix_segment(self.cap, seg_id="Tech-1#1", end_at="2026-10-15T10:00:00+08:00")
        self.assertEqual(res["segment"]["machine_seconds"], 3600)
        self.assertEqual(res["entry"]["seconds"], 3600)
        self.assertNotIn("actor", res["entry"])

    def test_cli_delivered_manager_unmeasured_exit_zero(self) -> None:
        """CLI：--delivered 遇 open Manager 段 → 退出码 0、段 unmeasured、无 worklog。"""
        uc.start_task(self.cap, name="事务", task_id="Tech-1", at="2026-10-15T09:00:00+08:00")
        proc = subprocess.run([sys.executable, str(TOOL), str(self.cap), "--delivered", "2026-10-15"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._segments()["Tech-1#1"]["state"], "unmeasured")
        self.assertEqual(self._worklogs(), [])

    def test_cli_fix_role_segment_exit_one_bytes_untouched(self) -> None:
        """CLI：--fix-segment 指向 role 段 → 退出码 1、字节不变。"""
        role_seg = self._open_role_segment()
        before = (self.cap / "capsule.yaml").read_bytes()
        proc = subprocess.run([sys.executable, str(TOOL), str(self.cap), "--fix-segment",
                               role_seg["seg_id"], "--end-at", "2026-10-15T10:00:00+08:00"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("actor=role", proc.stderr)
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)


class DispatchSegmentTests(unittest.TestCase):
    """Tech-176 T9（方案 4.4 判定 3/4、4.4.2）：角色段自开自结、ROLE_STAGE 映射与失败段。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261029_调度段"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")
        (self.cap / "02_方案.md").write_text("方案\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _segments(self) -> dict:
        return {s["seg_id"]: s for s in uc.read_segments(self.cap)}

    def _open(self, dispatch_id: str, role: str, deliverable: str | None = None,
              at: str | None = None) -> dict:
        if at:
            # 直接构造指定开始时刻的 open role 段（固定时钟场景），追加到既有段之后
            import yaml
            data = yaml.safe_load((self.cap / "capsule.yaml").read_text(encoding="utf-8"))
            segs = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
            seq = 1 + max([int(s.get("seq", 0)) for s in segs], default=0)
            seg = {"seq": seq, "seg_id": f"Tech-1#{seq}",
                   "name": f"{role}→{deliverable or 'verify'}",
                   "startAt": at, "lastActivityAt": at, "state": "open", "actor": "role",
                   "role": role, "dispatch_id": dispatch_id, "trigger": "dispatch",
                   "stage": uc._dispatch_stage(role, deliverable)}
            segs.append(seg)
            data["task_segments"] = segs
            data["seq_next"] = seq + 1
            (self.cap / "capsule.yaml").write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return seg
        return uc.start_dispatch_segment(self.cap, dispatch_id=dispatch_id, role=role,
                                         deliverable=deliverable)["segment"]

    # ---------- (a) P-2 复现：只结同 dispatch_id 的段 ----------

    def test_p2_only_own_dispatch_id_is_closed(self) -> None:
        """同一胶囊先后开 d1、d2 两个 Designer 段：先结 d2 只结 d2，d1 仍 open。"""
        d1 = self._open("a" * 32, "Designer", "03_设计.md")
        d2 = self._open("b" * 32, "Designer", "03_设计.md")
        self.assertNotEqual(d1["seg_id"], d2["seg_id"])
        self.assertEqual(len(uc.read_segments(self.cap)), 2)

        res2 = uc.end_dispatch_segment("b" * 32, True, capsule=self.cap)
        self.assertEqual(res2["segment"]["state"], "logged")
        segs = uc.read_segments(self.cap)
        states = {s["dispatch_id"]: s["state"] for s in segs}
        self.assertEqual(states["b" * 32], "logged")
        self.assertEqual(states["a" * 32], "open", "先结 d2 不得碰 d1")
        self.assertEqual(len(uc.read_worklogs(self.cap)), 1)

        res1 = uc.end_dispatch_segment("a" * 32, True, capsule=self.cap)
        self.assertEqual(res1["segment"]["state"], "logged")
        states = {s["dispatch_id"]: s["state"] for s in uc.read_segments(self.cap)}
        self.assertEqual(states["a" * 32], "logged")
        self.assertEqual(len(uc.read_worklogs(self.cap)), 2, "两段各自入账一条 worklog")

    def test_p2_each_segment_duration_is_own(self) -> None:
        """两段时长各自正确：d1 与 d2 的开始时刻不同，结段时刻各取当时 now。"""
        d1 = self._open("a" * 32, "Designer", "03_设计.md",
                        at="2026-10-15T09:00:00+08:00")
        d2 = self._open("b" * 32, "Designer", "03_设计.md",
                        at="2026-10-15T09:10:00+08:00")
        fixed = dt.datetime.fromisoformat("2026-10-15T09:20:00+08:00")

        class _Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        fake = mock.Mock(wraps=uc.dt)
        fake.datetime = _Frozen
        with mock.patch.object(uc, "dt", fake):
            r2 = uc.end_dispatch_segment("b" * 32, True, capsule=self.cap)
            r1 = uc.end_dispatch_segment("a" * 32, True, capsule=self.cap)
        self.assertEqual(r2["segment"]["machine_seconds"], 600, "d2 自 09:10 起 10 分钟")
        self.assertEqual(r1["segment"]["machine_seconds"], 1200, "d1 自 09:00 起 20 分钟")
        self.assertEqual(r2["entry"]["seconds"], 600)
        self.assertEqual(r1["entry"]["seconds"], 1200)
        by_ref = {w["ref"]: w["seconds"] for w in uc.read_worklogs(self.cap)}
        self.assertEqual(by_ref[d1["seg_id"]], 1200)
        self.assertEqual(by_ref[d2["seg_id"]], 600)

    # ---------- (b) 失败段 ----------

    def test_failed_dispatch_writes_failed_segment_without_worklog(self) -> None:
        """succeeded=False → state=failed、有 machine_seconds、无 worklog。"""
        self._open("c" * 32, "Architecture", "02_方案.md",
                   at="2026-10-15T09:00:00+08:00")
        fixed = dt.datetime.fromisoformat("2026-10-15T09:05:00+08:00")

        class _Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        fake = mock.Mock(wraps=uc.dt)
        fake.datetime = _Frozen
        with mock.patch.object(uc, "dt", fake):
            res = uc.end_dispatch_segment("c" * 32, False, failure_code="CHAIN_EXHAUSTED", capsule=self.cap)
        seg = res["segment"]
        self.assertEqual(seg["state"], "failed")
        self.assertEqual(seg["machine_seconds"], 300)
        self.assertIn("CHAIN_EXHAUSTED", seg["note"])
        self.assertNotIn("effective_seconds", seg, "失败段不计有效工时")
        self.assertEqual(uc.read_worklogs(self.cap), [])
        self.assertEqual(res["worklog_refs"], [])

    # ---------- (c) Reviewer 前缀映射 ----------

    def test_reviewer_stage_by_deliverable_prefix(self) -> None:
        """Reviewer：03_方案审计报告.md → 03_方案审计；05_实施审计报告.md → 05_实施审计。"""
        s1 = uc.start_dispatch_segment(self.cap, dispatch_id="d" * 32, role="Reviewer",
                                       deliverable="03_方案审计报告.md")["segment"]
        self.assertEqual(s1["stage"], "03_方案审计")
        self.assertEqual(s1["name"], "Reviewer→03_方案审计报告.md")
        uc.end_dispatch_segment("d" * 32, True, capsule=self.cap)
        s2 = uc.start_dispatch_segment(self.cap, dispatch_id="e" * 32, role="Reviewer",
                                       deliverable="05_实施审计报告.md")["segment"]
        self.assertEqual(s2["stage"], "05_实施审计")
        self.assertEqual(s2["name"], "Reviewer→05_实施审计报告.md")

    def test_role_stage_mapping_table(self) -> None:
        """ROLE_STAGE 全表映射（方案 4.4 判定 3）。"""
        self.assertEqual(uc.ROLE_STAGE["Researcher"], "01_调研")
        self.assertEqual(uc.ROLE_STAGE["Architecture"], "02_方案")
        self.assertEqual(uc.ROLE_STAGE["Designer"], "03_设计")
        self.assertEqual(uc.ROLE_STAGE["Builder"], "04_实施")
        self.assertEqual(uc.ROLE_STAGE["Maintainer"], "07_验收")
        self.assertEqual(uc.ROLE_STAGE["Reporter"], "08_汇报")
        for role, stage in uc.ROLE_STAGE.items():
            seg = uc.start_dispatch_segment(self.cap, dispatch_id=uuid.uuid4().hex, role=role,
                                            deliverable="x.md")["segment"]
            self.assertEqual(seg["stage"], stage, role)
            self.assertEqual(seg["actor"], "role")
            self.assertEqual(seg["role"], role)
            self.assertEqual(seg["trigger"], "dispatch")
            uc.end_dispatch_segment(seg["dispatch_id"], True, capsule=self.cap)

    # ---------- (d) 静默契约 ----------

    def test_no_capsule_or_terminal_returns_none(self) -> None:
        """无胶囊、胶囊终态、无同 dispatch_id open 段 → 返回 None，不抛异常。"""
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty"
            empty.mkdir()
            self.assertIsNone(uc.start_dispatch_segment(empty, dispatch_id="f" * 32,
                                                        role="Builder", deliverable="x.md"))
            self.assertIsNone(uc.end_dispatch_segment("f" * 32, True, capsule=empty))

        self.assertIsNone(uc.end_dispatch_segment("9" * 32, True, capsule=self.cap),
                          "无同 dispatch_id 的 open 段时静默返回 None")

        import yaml
        (self.cap / "capsule.yaml").write_text(
            yaml.safe_dump({"id": "Tech-1", "lifecycle": "archived", "task_segments": [],
                            "worklogs": [], "seq_next": 1}, allow_unicode=True), encoding="utf-8")
        self.assertIsNone(uc.start_dispatch_segment(self.cap, dispatch_id="f" * 32,
                                                    role="Builder", deliverable="x.md"))
        self.assertIsNone(uc.end_dispatch_segment("f" * 32, True, capsule=self.cap))

    def test_effective_seconds_subtracts_failed_attempts(self) -> None:
        """4.4.2：effective_seconds = machine_seconds − 失败 attempt 时长，最少 1 秒。"""
        self._open("1" * 32, "Builder", "04_Spec_Tech-1.md",
                   at="2026-10-15T09:00:00+08:00")
        fixed = dt.datetime.fromisoformat("2026-10-15T09:01:40+08:00")

        class _Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        fake = mock.Mock(wraps=uc.dt)
        fake.datetime = _Frozen
        with mock.patch.object(uc, "dt", fake):
            res = uc.end_dispatch_segment("1" * 32, True, failed_attempt_seconds=40, capsule=self.cap)
        self.assertEqual(res["segment"]["machine_seconds"], 100)
        self.assertEqual(res["segment"]["effective_seconds"], 60)
        self.assertEqual(res["entry"]["seconds"], 60)
        self.assertEqual(res["entry"]["actor"], "role")
        self.assertEqual(res["entry"]["role"], "Builder")

    def test_effective_seconds_floor_is_one(self) -> None:
        """失败 attempt 时长不小于总时长时，有效秒取 1（4.4.2 最少 1 秒）。"""
        self._open("2" * 32, "Builder", "04_Spec_Tech-1.md",
                   at="2026-10-15T09:00:00+08:00")
        fixed = dt.datetime.fromisoformat("2026-10-15T09:00:30+08:00")

        class _Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        fake = mock.Mock(wraps=uc.dt)
        fake.datetime = _Frozen
        with mock.patch.object(uc, "dt", fake):
            res = uc.end_dispatch_segment("2" * 32, True, failed_attempt_seconds=9999,
                                          capsule=self.cap)
        self.assertEqual(res["segment"]["machine_seconds"], 30)
        self.assertEqual(res["segment"]["effective_seconds"], 1)

    def test_manager_segment_not_blocked_by_open_role_segment(self) -> None:
        """open 的 role 段不阻塞 Manager 开段；I-9 同 dispatch_id 不重复开段。"""
        self._open("3" * 32, "Designer", "03_设计.md")
        mgr = uc.start_task(self.cap, name="事务", task_id="Tech-1")
        self.assertEqual(mgr["segment"]["actor"], "manager")
        self.assertIsNone(uc.start_dispatch_segment(self.cap, dispatch_id="3" * 32,
                                                    role="Designer", deliverable="03_设计.md"),
                          "同 dispatch_id 已有 open 段，不重复开段")

    def test_duplicate_dispatch_id_blocked_by_i9(self) -> None:
        """I-9：同一胶囊内 dispatch_id 唯一，重复写入被拒且字节不变。"""
        self._open("4" * 32, "Designer", "03_设计.md")
        seg2 = {"seq": 2, "seg_id": "Tech-1#2", "name": "Designer→03_设计.md",
                "startAt": "2026-10-15T09:10:00+08:00", "lastActivityAt": "2026-10-15T09:10:00+08:00",
                "state": "open", "actor": "role", "role": "Designer",
                "dispatch_id": "4" * 32, "trigger": "dispatch", "stage": "03_设计"}
        import yaml
        before = (self.cap / "capsule.yaml").read_bytes()
        with self.assertRaises(uc.ToolError) as ctx:
            uc._check_invariants({"task_segments": [uc.read_segments(self.cap)[0], seg2],
                                  "worklogs": [], "seq_next": 3})
        self.assertIn("dispatch_id 重复", str(ctx.exception))
        self.assertEqual((self.cap / "capsule.yaml").read_bytes(), before)


class StageEffortHmsTests(unittest.TestCase):
    """Tech-176 T13（方案 4.4 判定「1 effort」）：Front Matter effort 写 H:MM:SS 并补齐映射键。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "cap"
        self.cap.mkdir()
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(lifecycle="active"), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_log_time_writes_effort_hms_and_validates(self):
        """log 之后 02_方案.md 的 effort: "0:07:00" 能通过 front_matter 校验（Spec T13 行）。"""
        (self.cap / "02_方案.md").write_text(
            "---\ntype: Proposal\ntopic: x\ndate: 2026-10-15\nauthor: Architecture\n---\n\n方案\n",
            encoding="utf-8")
        res = uc.log_time(self.cap, seconds=420, stage="02_方案", summary="方案", operator="t")
        self.assertEqual(res["status"], "added")
        fm = (self.cap / "02_方案.md").read_text(encoding="utf-8")
        self.assertIn('effort: 0:07:00', fm)
        import validate_schema as vs
        block = fm.split("---")[1]
        effort = dict(line.split(": ", 1) for line in block.strip().splitlines()
                      if line.startswith("effort: "))["effort"].strip()
        self.assertEqual(vs.validate(effort, vs.load_schema("front_matter")["properties"]["effort"]), [])

    def test_stage_map_covers_audit_and_report_keys(self):
        """映射键补 03_方案审计 / 05_实施审计 / 08_汇报（方案 4.4 判定「1 effort」）。"""
        for stage, name in (("03_方案审计", "03_方案审计报告.md"),
                            ("05_实施审计", "05_实施审计报告.md"),
                            ("08_汇报", "08_汇报.md")):
            target = self.cap / name
            target.write_text("---\ntype: Report\ntopic: x\ndate: 2026-10-15\nauthor: Builder\n---\n\nx\n",
                              encoding="utf-8")
            uc._sync_stage_deliverable_effort(self.cap, stage, 3600)
            self.assertIn("effort: 1:00:00", target.read_text(encoding="utf-8"), stage)


if __name__ == "__main__":
    unittest.main()
