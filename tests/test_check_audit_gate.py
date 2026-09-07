import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.check_audit_gate import (
    check_candidate_commit,
    check_report,
    extract_independence,
    find_closed_critical_blocks,
)

SESSION_INTERNAL_WITH_CLOSED_CRITICAL = """---
type: Audit
topic: 某系统方案
date: 2026-09-04
author: Reviewer
status: active
independence: session-internal-downgraded (external: some-cli missing)
---

## 问题清单

### 问题 1
级别: Critical
状态: 已关闭
现象: ...
"""

SESSION_INTERNAL_ALL_OPEN = """---
type: Audit
independence: session-internal-downgraded (external: some-cli missing)
---

### 问题 1
级别: Critical
状态: 待处理
"""

EXTERNAL_REVIEWER_WITH_CLOSED_CRITICAL = """---
type: Audit
independence: external:some-cli
---

### 问题 1
级别: Critical
状态: 已关闭
"""

NO_FRONT_MATTER = "# 没有 Front Matter 的报告\n级别: Critical\n状态: 已关闭\n"


class CheckAuditGateTests(unittest.TestCase):
    def test_extract_independence_reads_declared_field(self) -> None:
        self.assertEqual(
            extract_independence(SESSION_INTERNAL_WITH_CLOSED_CRITICAL),
            "session-internal-downgraded (external: some-cli missing)",
        )

    def test_extract_independence_missing_returns_none(self) -> None:
        self.assertIsNone(extract_independence(NO_FRONT_MATTER))

    def test_find_closed_critical_blocks_detects_closed_entry(self) -> None:
        blocks = find_closed_critical_blocks(SESSION_INTERNAL_WITH_CLOSED_CRITICAL)
        self.assertEqual(len(blocks), 1)

    def test_find_closed_critical_blocks_skips_open_entry(self) -> None:
        self.assertEqual(find_closed_critical_blocks(SESSION_INTERNAL_ALL_OPEN), [])

    def test_check_report_blocks_session_internal_closed_critical(self) -> None:
        issues = check_report(SESSION_INTERNAL_WITH_CLOSED_CRITICAL)
        self.assertEqual(len(issues), 1)
        self.assertIn("会话内自评不可关闭 Critical", issues[0])

    def test_check_report_passes_when_critical_still_open(self) -> None:
        self.assertEqual(check_report(SESSION_INTERNAL_ALL_OPEN), [])

    def test_check_report_passes_for_external_reviewer(self) -> None:
        self.assertEqual(check_report(EXTERNAL_REVIEWER_WITH_CLOSED_CRITICAL), [])

    def test_check_report_blocks_when_independence_undeclared(self) -> None:
        """fail-closed：缺 independence 不再放行（schema_version<2 旧契约分支）。

        原断言把 fail-open 写成了契约——缺字段即通过，等于「忘写声明」比「如实上报
        会话内降级」更容易过关，奖励漏报。改为缺字段即阻断。
        """
        issues = check_report(NO_FRONT_MATTER)
        self.assertEqual(len(issues), 1)
        self.assertIn("未声明 independence", issues[0])


V2_SESSION_CLOSED_CRITICAL = """---
type: Audit
schema_version: 2
reviewer_mode: session
fallback_reason: not_configured
target_path: 02_方案.md
target_sha256: {sha}
---

### 问题 1
级别: Critical
ID: C-1
状态: closed
"""

V2_WAIVED_MISSING_ACK = """---
type: Audit
schema_version: 2
reviewer_mode: session
fallback_reason: not_configured
target_path: 02_方案.md
target_sha256: {sha}
---

### 问题 1
级别: Critical
ID: C-1
状态: waived_by_user
"""

V2_WAIVED_WITH_ACK_TEMPLATE = """---
type: Audit
schema_version: 2
reviewer_mode: session
fallback_reason: not_configured
target_path: 02_方案.md
target_sha256: {sha}
---

### 问题 1
级别: Critical
ID: C-1
状态: waived_by_user

### critical_ack C-1
- target_sha256: {ack_sha}
- 确认事件: 用户在会话中明确确认承担风险
- 适用范围: 仅本轮受审指纹
"""

V2_EXTERNAL_CLOSED_CRITICAL = """---
type: Audit
schema_version: 2
reviewer_mode: external
reviewer_ref: some-cli --model x
target_path: 02_方案.md
target_sha256: {sha}
---

### 问题 1
级别: Critical
ID: C-1
状态: closed
"""

SHA_A = "a" * 64
SHA_B = "b" * 64


class CheckAuditGateV2Tests(unittest.TestCase):
    def test_v2_session_mode_cannot_close_critical(self) -> None:
        issues = check_report(V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A))
        self.assertEqual(len(issues), 1)
        self.assertIn("会话内承载不可将 Critical 置为 closed", issues[0])

    def test_v2_external_mode_can_close_critical(self) -> None:
        self.assertEqual(check_report(V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A)), [])

    def test_v2_waived_without_ack_block_blocked(self) -> None:
        issues = check_report(V2_WAIVED_MISSING_ACK.format(sha=SHA_A))
        self.assertEqual(len(issues), 1)
        self.assertIn("未找到对应 critical_ack", issues[0])

    def test_v2_waived_with_matching_ack_passes(self) -> None:
        text = V2_WAIVED_WITH_ACK_TEMPLATE.format(sha=SHA_A, ack_sha=SHA_A)
        self.assertEqual(check_report(text), [])

    def test_v2_waived_with_mismatched_fingerprint_blocked(self) -> None:
        text = V2_WAIVED_WITH_ACK_TEMPLATE.format(sha=SHA_A, ack_sha=SHA_B)
        issues = check_report(text)
        self.assertEqual(len(issues), 1)
        self.assertIn("复核对象已变化", issues[0])


class CheckAuditGateAtomicTests(unittest.TestCase):
    def test_candidate_commit_rejects_target_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "02_方案.md"
            target.write_text("方案内容", encoding="utf-8")
            actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            commit_path = tmp_path / "05_审计报告.md"
            self.assertNotEqual(actual_hash, SHA_A)  # SHA_A 是候选报告里故意写错的指纹
            candidate_text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A)
            issues = check_candidate_commit(candidate_text, commit_path)
            self.assertTrue(any("受审对象已变化" in i for i in issues))

    def test_candidate_commit_passes_when_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "02_方案.md"
            target.write_text("方案内容", encoding="utf-8")
            actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            commit_path = tmp_path / "05_审计报告.md"
            candidate_text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=actual_hash)
            self.assertEqual(check_candidate_commit(candidate_text, commit_path), [])


if __name__ == "__main__":
    unittest.main()
