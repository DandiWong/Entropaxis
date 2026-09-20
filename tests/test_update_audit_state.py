"""update_audit_state 的字段级读写与门禁委托核验。

重点不是"能不能写"，而是**能不能绕过 check_audit_gate**——本工具存在的前提是它不复制
任何门禁策略，所以拒绝路径的用例比成功路径更重要。
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
