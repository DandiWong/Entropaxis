import contextlib
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
