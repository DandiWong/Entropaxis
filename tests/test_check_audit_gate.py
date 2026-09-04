import unittest

from tools.check_audit_gate import (
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

    def test_check_report_passes_without_independence_declared(self) -> None:
        self.assertEqual(check_report(NO_FRONT_MATTER), [])


if __name__ == "__main__":
    unittest.main()
