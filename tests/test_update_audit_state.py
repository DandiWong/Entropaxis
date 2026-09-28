"""update_audit_state 的字段级读写与门禁委托核验。

重点不是"能不能写"，而是**能不能绕过 check_audit_gate**——本工具存在的前提是它不复制
任何门禁策略，所以拒绝路径的用例比成功路径更重要。
"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import update_audit_state as uas

REPORT_TEMPLATE = """---
type: Audit
topic: 某系统设计方案
date: 2026-09-20
author: Reviewer
status: active
schema_version: 3
reviewer_mode: {mode}
reviewer_ref: some-cli --model some-model
target_path: {target}
target_sha256: {sha}
{extra}---

## 问题清单（人读叙事）

正文叙事不参与机器判定。

```audit-state
{{
  "issues": [
    {{"id": "C-1", "level": "Critical", "status": "open"}},
    {{"id": "M-1", "level": "Major", "status": "open"}}
  ],
  "critical_acks": []
}}
```
"""


class UpdateAuditStateTests(unittest.TestCase):
    def _fixture(self, tmp: Path, mode: str = "external", extra: str = "") -> Path:
        target = tmp / "01_方案.md"
        target.write_text("方案正文\n", encoding="utf-8")
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        report = tmp / "05_审计报告.md"
        report.write_text(
            REPORT_TEMPLATE.format(mode=mode, target=target.name, sha=sha, extra=extra),
            encoding="utf-8",
        )
        return report

    def test_load_and_add_issue(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td))
            state = uas.load_state(report.read_text(encoding="utf-8"))
            self.assertEqual([i["id"] for i in state["issues"]], ["C-1", "M-1"])

            state = uas.add_issue(state, "m-2", "Minor")
            self.assertEqual(state["issues"][-1], {"id": "m-2", "level": "Minor", "status": "open"})

    def test_add_duplicate_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td))
            state = uas.load_state(report.read_text(encoding="utf-8"))
            with self.assertRaises(uas.AuditStateError):
                uas.add_issue(state, "C-1", "Critical")

    def test_unknown_level_and_status_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td))
            state = uas.load_state(report.read_text(encoding="utf-8"))
            with self.assertRaises(uas.AuditStateError):
                uas.add_issue(state, "X-1", "Blocker")           # 级别不在 level_enum
            with self.assertRaises(uas.AuditStateError):
                uas.set_field(state, "M-1", "status", "resolved")  # 状态不在 status_enum
            with self.assertRaises(uas.AuditStateError):
                uas.set_field(state, "M-1", "critical_acks", "x")  # 不可代写风险接受记录

    def test_dump_preserves_surrounding_prose(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td))
            text = report.read_text(encoding="utf-8")
            state = uas.load_state(text)
            state = uas.set_field(state, "M-1", "status", "closed")
            new_text = uas.dump_state(text, state)
            self.assertIn("正文叙事不参与机器判定。", new_text)
            self.assertIn("topic: 某系统设计方案", new_text)
            self.assertEqual(uas.load_state(new_text)["issues"][1]["status"], "closed")

    def test_commit_allows_major_close_under_external_review(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td))
            text = report.read_text(encoding="utf-8")
            state = uas.set_field(uas.load_state(text), "M-1", "status", "closed")
            uas.commit(report, uas.dump_state(text, state))
            self.assertEqual(uas.load_state(report.read_text(encoding="utf-8"))["issues"][1]["status"], "closed")

    def test_commit_refuses_critical_close_in_session_mode(self):
        """会话内承载不得关闭 Critical——策略归 check_audit_gate，本工具只负责不绕过它。"""
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td), mode="session", extra="fallback_reason: not_configured\n")
            text = report.read_text(encoding="utf-8")
            state = uas.set_field(uas.load_state(text), "C-1", "status", "closed")
            with self.assertRaises(uas.AuditStateError) as ctx:
                uas.commit(report, uas.dump_state(text, state))
            self.assertIn("reviewer_mode", str(ctx.exception))
            # 关键：拒绝后磁盘上的报告必须原样未变
            self.assertEqual(uas.load_state(report.read_text(encoding="utf-8"))["issues"][0]["status"], "open")

    def test_commit_refuses_waiver_without_ack(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td))
            text = report.read_text(encoding="utf-8")
            state = uas.set_field(uas.load_state(text), "C-1", "status", "waived_by_user")
            with self.assertRaises(uas.AuditStateError):
                uas.commit(report, uas.dump_state(text, state))

    def test_commit_refuses_when_target_fingerprint_stale(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            report = self._fixture(tmp)
            (tmp / "01_方案.md").write_text("方案正文被改过了\n", encoding="utf-8")
            text = report.read_text(encoding="utf-8")
            state = uas.set_field(uas.load_state(text), "M-1", "status", "closed")
            with self.assertRaises(uas.AuditStateError) as ctx:
                uas.commit(report, uas.dump_state(text, state))
            self.assertIn("target_sha256", str(ctx.exception))

    def test_missing_fence_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "05_审计报告.md"
            path.write_text("---\ntype: Audit\n---\n\n无围栏正文\n", encoding="utf-8")
            with self.assertRaises(uas.AuditStateError) as ctx:
                uas.load_state(path.read_text(encoding="utf-8"))
            self.assertIn("👉", str(ctx.exception))

    def test_duplicate_json_key_rejected(self):
        """重复键是歧义载荷，必须 fail-closed（复用 check_audit_gate 的判据）。"""
        text = (
            "---\ntype: Audit\n---\n\n```audit-state\n"
            '{"issues": [], "issues": [], "critical_acks": []}\n```\n'
        )
        with self.assertRaises(uas.AuditStateError):
            uas.load_state(text)


class UpdateAuditStateV4AndReceiptTests(unittest.TestCase):
    """v4 字段写入，以及已盖章报告的状态推进（写入即清回执、报告回到待复核态）。"""

    def _fixture(self, tmp: Path, version: int = 3, mode: str = "external") -> Path:
        target = tmp / "01_方案.md"
        target.write_text("方案正文\n", encoding="utf-8")
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        extra = ""
        if version == 4:
            extra = "audit_phase: plan\n"
        if mode == "session":
            extra += "fallback_reason: not_configured\n"
        text = REPORT_TEMPLATE.format(mode=mode, target=target.name, sha=sha, extra=extra)
        text = text.replace("schema_version: 3", f"schema_version: {version}")
        if version == 4:
            text = text.replace('"status": "open"}', '"status": "open", "gate": "impl", "basis": "AC-1", '
                                                       '"evidence": "traced"}')
        report = tmp / "05_审计报告.md"
        report.write_text(text, encoding="utf-8")
        return report

    def _sign(self, report: Path, tmp: Path):
        """模拟调度器盖章：签发 Reviewer 回执并写入 carrier/receipt_id。"""
        from tools import check_audit_gate as cag, dispatch_receipt as rc
        patches = [mock.patch.object(rc, "RECEIPT_DIR", tmp / "receipts"),
                   mock.patch.object(rc, "load_key", return_value=b"k")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        rec = rc.issue("Reviewer", "0" * 64, report, tmp / "01_方案.md", "reviewer-primary")
        text = report.read_text(encoding="utf-8").replace(
            "---\n\n## 问题清单", f"carrier: reviewer-primary\nreceipt_id: {rec['receipt_id']}\n---\n\n## 问题清单", 1)
        report.write_text(text, encoding="utf-8")
        self.assertEqual(cag.check_report_file(report), [])
        return cag

    def _set(self, report: Path, issue_id: str, field: str, value: str) -> bool:
        text = report.read_text(encoding="utf-8")
        return uas.commit(report, uas.dump_state(text, uas.set_field(uas.load_state(text), issue_id, field, value)))

    def test_signed_report_advances_and_loses_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            report = self._fixture(tmp)
            cag = self._sign(report, tmp)
            self.assertTrue(self._set(report, "M-1", "status", "closed"))
            self.assertNotIn("receipt_id:", report.read_text(encoding="utf-8"))
            self.assertTrue(any("receipt_id" in i for i in cag.check_report_file(report)))

    def test_signed_report_critical_close_is_exposed_not_laundered(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            report = self._fixture(tmp)
            cag = self._sign(report, tmp)
            self.assertTrue(self._set(report, "C-1", "status", "closed"))
            self.assertTrue(cag.check_report_file(report))  # 失效，须下一轮 Reviewer 重盖章

    def test_session_report_still_cannot_close_critical(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td), mode="session")
            with self.assertRaises(uas.AuditStateError):
                self._set(report, "C-1", "status", "closed")

    def test_v4_add_issue_writes_fields_and_gate_enforces(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._fixture(Path(td), version=4)
            text = report.read_text(encoding="utf-8")
            state = uas.add_issue(uas.load_state(text), "M-2", "Major", gate="plan", basis="AC-1",
                                  evidence="measured")
            uas.commit(report, uas.dump_state(text, state))
            self.assertEqual(uas.load_state(report.read_text(encoding="utf-8"))["issues"][-1]["evidence"], "measured")
            text = report.read_text(encoding="utf-8")
            state = uas.add_issue(uas.load_state(text), "M-3", "Major", gate="plan", basis="AC-1")
            with self.assertRaises(uas.AuditStateError):  # 缺 evidence 由门禁拒绝
                uas.commit(report, uas.dump_state(text, state))

    def test_v4_field_enums_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state = uas.load_state(self._fixture(Path(td), version=4).read_text(encoding="utf-8"))
            with self.assertRaises(uas.AuditStateError):
                uas.set_field(state, "C-1", "gate", "later")
            with self.assertRaises(uas.AuditStateError):
                uas.set_field(state, "C-1", "evidence", "guess")

    def test_blocking_summary_matches_gate(self):
        with tempfile.TemporaryDirectory() as td:
            state = uas.load_state(self._fixture(Path(td), version=4).read_text(encoding="utf-8"))
            self.assertIn("阻断 0（无）｜待实施 2", uas.blocking_summary(state["issues"], "plan"))
            self.assertIn("阻断 2（C-1, M-1）", uas.blocking_summary(state["issues"], "impl"))


if __name__ == "__main__":
    unittest.main()
