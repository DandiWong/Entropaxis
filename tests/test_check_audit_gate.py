import hashlib
import json
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


_V2_BASE_FM = """---
type: Audit
topic: 测试方案
date: 2026-09-08
author: Reviewer
status: active
schema_version: 2
reviewer_mode: {mode}
reviewer_ref: {ref}
target_path: 02_方案.md
target_sha256: {{sha}}
"""

V2_SESSION_CLOSED_CRITICAL = _V2_BASE_FM.format(mode="session", ref="session") + """fallback_reason: not_configured
---

### 问题 1
级别: Critical
ID: C-1
状态: closed
"""

V2_WAIVED_MISSING_ACK = _V2_BASE_FM.format(mode="session", ref="session") + """fallback_reason: not_configured
---

### 问题 1
级别: Critical
ID: C-1
状态: waived_by_user
"""

V2_WAIVED_WITH_ACK_TEMPLATE = _V2_BASE_FM.format(mode="session", ref="session") + """fallback_reason: not_configured
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

V2_EXTERNAL_CLOSED_CRITICAL = _V2_BASE_FM.format(mode="external", ref="some-cli --model x") + """---

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


class CheckAuditGateV2FailOpenRegressionTests(unittest.TestCase):
    """第 4 轮外置复核实测抓到的 4 处 fail-open；每条对应一个回归断言，防止再次悄悄放行。"""

    def test_v2_missing_required_fields_blocked(self) -> None:
        text = "---\ntype: Audit\nschema_version: 2\nreviewer_mode: external\n---\n" + \
            "### 问题 1\n级别: Critical\nID: C-1\n状态: closed\n"
        self.assertTrue(check_report(text))

    def test_v2_forbidden_independence_blocked(self) -> None:
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "---\n\n### 问题 1", "independence: external: x\n---\n\n### 问题 1"
        )
        issues = check_report(text)
        self.assertTrue(any("不得再声明 independence" in i for i in issues))

    def test_v2_challenged_status_blocked(self) -> None:
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace("状态: closed", "状态: challenged")
        issues = check_report(text)
        self.assertTrue(any("不在 v2 枚举" in i for i in issues))

    def test_malformed_schema_version_does_not_downgrade_to_legacy(self) -> None:
        text = ("---\ntype: Audit\nschema_version: banana\n"
                "independence: session-internal-downgraded (external: x missing)\n---\n"
                "### 问题 1\n级别: Critical\n状态: 已关闭\n")
        self.assertTrue(check_report(text))

    def test_candidate_commit_rejects_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            commit_path = Path(tmp) / "05_审计报告.md"
            text = "---\ntype: Audit\nschema_version: 2\nreviewer_mode: external\n---\n" + \
                "### 问题 1\n级别: Critical\nID: C-1\n状态: closed\n"
            self.assertTrue(check_candidate_commit(text, commit_path))

    def test_legacy_report_without_schema_version_key_still_validates(self) -> None:
        """回归防护：v2 校验不得误吞没有 schema_version 键的历史报告。"""
        self.assertEqual(check_report(EXTERNAL_REVIEWER_WITH_CLOSED_CRITICAL), [])

    def test_schema_version_with_space_before_colon_still_detected_as_v2(self) -> None:
        """第 5 轮实测抓到：`schema_version : 2`（冒号前带空格）曾被误判为旧契约，
        配合伪造 independence 即可放行 session 模式关闭 Critical。"""
        text = ("---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
                "schema_version : 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
                "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n"
                "independence: external: fake-cli\n---\n"
                "### 问题 1\n级别: Critical\nID: C-1\n状态: closed\n")
        issues = check_report(text)
        self.assertTrue(any("会话内承载不可将 Critical 置为 closed" in i for i in issues))
        self.assertTrue(any("不得再声明 independence" in i for i in issues))

    def test_status_with_space_before_colon_still_detected(self) -> None:
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace("状态: closed", "状态 : closed")
        issues = check_report(text)
        self.assertTrue(any("会话内承载不可将 Critical 置为 closed" in i for i in issues))

    def test_level_with_space_before_colon_still_detected(self) -> None:
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace("级别: Critical", "级别 : Critical")
        issues = check_report(text)
        self.assertTrue(any("会话内承载不可将 Critical 置为 closed" in i for i in issues))

    def test_lowercase_level_keyword_still_detected(self) -> None:
        """自查发现：`级别: critical`（小写）此前会让整个问题块从 _iter_issue_blocks
        彻底消失，比枚举校验失败更危险——不是被判错级别，是完全不存在。"""
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace("级别: Critical", "级别: critical")
        issues = check_report(text)
        self.assertTrue(any("会话内承载不可将 Critical 置为 closed" in i for i in issues))

    def test_issue_block_missing_id_is_blocked(self) -> None:
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace("ID: C-1\n", "")
        issues = check_report(text)
        self.assertTrue(any("`ID:` 标注出现 0 次" in i for i in issues))

    def test_issue_block_missing_status_is_blocked(self) -> None:
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace("状态: closed\n", "")
        issues = check_report(text)
        self.assertTrue(any("`状态:` 标注出现 0 次" in i for i in issues))


class CheckAuditGateRound6RegressionTests(unittest.TestCase):
    """第 6 轮外置复核系统性排查发现的 4 Critical + 1 Minor，逐条锁定。"""

    def test_r6c1_legacy_candidate_rejected_by_atomic_interface(self) -> None:
        """legacy（无 schema_version）候选不得经原子接口写入，即便伪造了 independence。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "02_方案.md"
            target.write_text("x", encoding="utf-8")
            commit_path = Path(tmp) / "05_审计报告.md"
            legacy_bad = "---\nindependence: external: fake\n---\n### 问题 1\n级别: Critical\n状态: 已关闭\n"
            issues = check_candidate_commit(legacy_bad, commit_path)
            self.assertTrue(issues)
            self.assertFalse(commit_path.exists())

    def test_r6c2_schema_version_above_two_still_requires_v2_fields(self) -> None:
        """schema_version: 3 曾经绕过 conditional_required（when 精确匹配 2），
        改为 enum:[2] 后任何非 2 值本身就不合法。"""
        text = ("---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
                "schema_version: 3\nreviewer_mode: external\n---\n"
                "### 问题 1\n级别: Critical\nID: C-1\n状态: closed\n")
        issues = check_report(text)
        self.assertTrue(any("schema_version" in i for i in issues))

    def test_r6c3_invalid_level_value_reported_not_invisible(self) -> None:
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace("级别: Critical", "级别: Blocker")
        issues = check_report(text)
        self.assertTrue(any("级别='Blocker'" in i or "级别=\"Blocker\"" in i for i in issues))

    def test_r6c4_duplicate_issue_id_blocked(self) -> None:
        """重复 ID 会让一份 critical_ack 同时"覆盖"多条不同问题，破坏逐问题豁免绑定。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### 问题 2\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- target_sha256: " + SHA_A + "\n- 确认事件: x\n- 适用范围: x\n"
        )
        issues = check_report(text)
        self.assertTrue(any("重复出现" in i for i in issues))

    def test_r7c1_duplicate_status_line_blocked(self) -> None:
        """第 7 轮实测抓到：状态行重复两次（open 在前、closed 在后），旧实现
        .search() 只取第一个匹配（open），永远不会触发"closed 需外置复核"的检查，
        原子接口会把含 Critical+closed 的候选当作通过。改为要求恰好一次，重复即违规。"""
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "状态: closed\n", "状态: open\n状态: closed\n"
        )
        issues = check_report(text)
        self.assertTrue(any("状态:` 标注出现 2 次" in i for i in issues))

    def test_missing_level_line_entirely_blocked(self) -> None:
        """整行删除"级别:"（不是写错值），改用标题分段后仍能被识别为候选问题块
        （因为段内还有 ID/状态），并因级别标注缺失被拒绝——不再是"该块彻底不存在"。"""
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace("级别: Critical\n", "")
        issues = check_report(text)
        self.assertTrue(any("级别:` 标注出现 0 次" in i for i in issues))

    def test_misspelled_level_label_still_caught_via_id_and_status(self) -> None:
        """级别标注被写错成无法匹配的形式（如误用不同字符），只要段内 ID/状态仍存在，
        该段依然被识别为候选问题块并因缺级别被拒绝，不会像纯字段值锚点方案那样
        整块消失。"""
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace("级别: Critical\n", "階級: Critical\n")
        issues = check_report(text)
        self.assertTrue(any("级别:` 标注出现 0 次" in i for i in issues))

    def test_r8c1_no_heading_at_all_still_detected(self) -> None:
        """第 8 轮实测抓到：全文没有任何 markdown 标题时，旧的"按标题分段"策略
        一次候选段都产不出来，字段齐全的 Critical+closed 直接放行。改用空行/---/
        标题三者之一作边界后，纯文本三行字段本身即构成一个候选段。"""
        text = ("---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
                "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
                "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
                "级别: Critical\nID: C-1\n状态: closed\n")
        issues = check_report(text)
        self.assertTrue(any("会话内承载不可将 Critical 置为 closed" in i for i in issues))

    def test_r8c2_duplicate_ack_block_for_same_id_blocked(self) -> None:
        """同一问题 ID 出现两个 critical_ack 确认块，此前用 dict 字面赋值，后一个
        静默覆盖前一个；改为检测重复块本身即违规，且该 ID 的确认结果标记为不可用。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- target_sha256: " + SHA_B + "\n- 确认事件: bad\n- 适用范围: bad\n\n"
            "### critical_ack C-1\n- target_sha256: " + SHA_A + "\n- 确认事件: good\n- 适用范围: good\n"
        )
        issues = check_report(text)
        self.assertTrue(any("确认块出现 2 次" in i for i in issues))

    def test_r8c2_duplicate_field_within_ack_block_blocked(self) -> None:
        """单个确认块内 target_sha256 重复两次（一个错误一个正确），此前 re.search()
        只取第一个匹配，可能采信错误的那个而放行。改为要求每字段恰好一次。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- target_sha256: " + SHA_B + "\n- target_sha256: " + SHA_A
            + "\n- 确认事件: x\n- 适用范围: x\n"
        )
        issues = check_report(text)
        self.assertTrue(any("target_sha256 出现 2 次" in i for i in issues))

    def test_r6m1_ack_heading_colon_variant_still_matches(self) -> None:
        """`### critical_ack: C-1`（冒号在前）不应被误判为"未找到确认块"。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack: C-1\n- target_sha256: " + SHA_A + "\n- 确认事件: x\n- 适用范围: x\n"
        )
        self.assertEqual(check_report(text), [])


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


class CheckAuditGateRound10RegressionTests(unittest.TestCase):
    """第 10 轮外置复核（C-2 补核）发现的 3 处残留绕过，逐条锁定。

    共同根因：带取值约束的正则数出的是"合法取值个数"而非"字段行出现次数"，
    重复字段行只要第二个取值非法就从计数中消失；全角冒号则让统一解析器
    读不出键，整份报告被误判为旧契约走宽松 legacy 路径。
    """

    def test_ack_duplicate_field_with_invalid_second_value_blocked(self) -> None:
        """R8-C2 残留：第二个 target_sha256 为大写哈希，旧取值正则把它从计数中
        抹掉，"恰好一次"被满足。计数改按字段行出现次数后须报重复。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- target_sha256: " + SHA_A + "\n- target_sha256: " + "B" * 64
            + "\n- 确认事件: x\n- 适用范围: x\n"
        )
        issues = check_report(text)
        self.assertTrue(any("target_sha256 出现 2 次" in i for i in issues))

    def test_ack_duplicate_field_with_empty_trailing_value_blocked(self) -> None:
        """第二个确认事件为空值时同样不得从重复计数中消失。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- target_sha256: " + SHA_A + "\n- 确认事件: x\n- 确认事件:\n- 适用范围: x\n"
        )
        issues = check_report(text)
        self.assertTrue(any("确认事件 出现 2 次" in i for i in issues))

    def test_issue_duplicate_id_line_with_empty_second_value_blocked(self) -> None:
        """问题块字段同源缺口：`ID: C-1` + 空值 `ID:` 的重复写法须按 2 次计。"""
        text = V2_EXTERNAL_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "ID: C-1\n", "ID: C-1\nID:\n"
        )
        issues = check_report(text)
        self.assertTrue(any("`ID:` 标注出现 2 次" in i for i in issues))

    def test_issue_status_duplicate_with_empty_second_value_blocked(self) -> None:
        """`状态: closed` + 空值 `状态:` 的重复写法不得绕过 closed 需外置复核。"""
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "状态: closed\n", "状态: closed\n状态:\n"
        )
        issues = check_report(text)
        self.assertTrue(any("`状态:` 标注出现 2 次" in i for i in issues))

    def test_fullwidth_colon_schema_version_treated_as_v2(self) -> None:
        """全角冒号 `schema_version：2` 不得让报告降级到 legacy 并凭伪造
        independence: external 放行 Critical+closed；统一解析器认全角冒号后
        该写法走 v2 校验，会话内承载关闭 Critical 必被拦截。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version：2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n"
            "independence: external: fake\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: closed\n"
        )
        issues = check_report(text)
        self.assertTrue(any("会话内承载不可将 Critical 置为 closed" in i for i in issues))
        self.assertTrue(any("不得再声明 independence" in i for i in issues))


class CheckAuditGateRound11RegressionTests(unittest.TestCase):
    """第 11 轮外置复核发现的 4 处绕过，逐条锁定。

    共同根因：计数/取值/候选判定三处正则均未锚定字段行的完整形态——
    子串命中伪字段名与嵌套子项、贪婪截取 65+ 位 hex、行内重复字段使
    取值正则全不满足进而整段退出候选集。
    """

    def test_ack_65_plus_hex_not_truncated_to_64(self) -> None:
        """65 位 hex 不得被贪婪截取为前 64 位并与 FM 指纹匹配。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- target_sha256: " + SHA_A + "a\n- 确认事件: x\n- 适用范围: x\n"
        )
        issues = check_report(text)
        self.assertTrue(any("取值为空或不符合格式" in i for i in issues))

    def test_all_fields_inline_duplicated_still_candidate(self) -> None:
        """三行字段全部行内重复（取值正则全不满足）时，段仍须进入候选集
        并因取值格式不符被拒，不得整段从校验中消失。"""
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "级别: Critical\nID: C-1\n状态: closed\n",
            "级别: Critical 级别: open\nID: C-1 ID: C-2\n状态: closed 状态: open\n",
        )
        issues = check_report(text)
        self.assertTrue(issues)
        self.assertTrue(any("取值为空或格式不符" in i or "标注出现" in i for i in issues))

    def test_ack_nested_subfields_rejected(self) -> None:
        """嵌套列表子项（`- 证据:` 下的缩进字段）不得被当作直接字段采信。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- 证据:\n  - target_sha256: " + SHA_A
            + "\n  - 确认事件: x\n  - 适用范围: x\n"
        )
        issues = check_report(text)
        self.assertTrue(any("未定义字段" in i for i in issues))

    def test_ack_pseudo_field_prefix_rejected(self) -> None:
        """`not_target_sha256:`/`未确认事件:` 等伪字段名前缀不得子串命中真字段。"""
        text = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 2\nreviewer_mode: session\nfallback_reason: not_configured\n"
            "reviewer_ref: session\ntarget_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n---\n"
            "### 问题 1\n级别: Critical\nID: C-1\n状态: waived_by_user\n\n"
            "### critical_ack C-1\n- not_target_sha256: " + SHA_A
            + "\n- 未确认事件: x\n- 不适用范围: x\n"
        )
        issues = check_report(text)
        self.assertTrue(any("未定义字段" in i or "出现 0 次" in i for i in issues))

    def test_all_english_label_variants_still_candidate(self) -> None:
        """第 11 轮实测：Level:/Issue-ID:/Status: 全变体段不得从候选集消失。"""
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "### 问题 1\n级别: Critical\nID: C-1\n状态: closed\n",
            "### Issue 1\nLevel: Critical\nIssue-ID: C-1\nStatus: closed\n",
        )
        issues = check_report(text)
        self.assertTrue(any("标注出现 0 次" in i for i in issues))

    def test_all_traditional_label_variants_still_candidate(self) -> None:
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "### 问题 1\n级别: Critical\nID: C-1\n状态: closed\n",
            "### 問題 1\n級別: Critical\nID: C-1\n狀態: closed\n",
        )
        issues = check_report(text)
        self.assertTrue(any("标注出现 0 次" in i for i in issues))

    def test_html_comment_hidden_fields_blocked(self) -> None:
        """第 11 轮第二批实测：`<!-- 级别: Critical -->` 注释包裹让字段行
        在行首锚定下不可见；规范化剥注释后照常进入校验并拦截。"""
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "级别: Critical\nID: C-1\n状态: closed\n",
            "<!-- 级别: Critical -->\n<!-- ID: C-1 -->\n<!-- 状态: closed -->\n",
        )
        issues = check_report(text)
        self.assertTrue(any("标注出现 0 次" in i for i in issues))

    def test_zero_width_chars_in_field_names_blocked(self) -> None:
        """零宽字符插入字段名（级<U+200B>别:）视觉不变而模式失配；规范化
        剥除 Cf 类字符后按正常字段行校验。"""
        zw = "​"
        text = V2_SESSION_CLOSED_CRITICAL.format(sha=SHA_A).replace(
            "级别: Critical\nID: C-1\n状态: closed\n",
            f"级{zw}别: Critical\nI{zw}D: C-1\n状{zw}态: closed\n",
        )
        issues = check_report(text)
        self.assertTrue(any("会话内承载不可将 Critical 置为 closed" in i for i in issues))

    def test_ack_block_variant_continuation_not_flagged(self) -> None:
        """ack 块内以变体标注开头的续行不得被误判为问题段（第 11 轮第二批
        实测误阻断）。"""
        text = V2_WAIVED_WITH_ACK_TEMPLATE.format(ack_sha=SHA_A, sha=SHA_A).replace(
            "- 适用范围: 仅本轮受审指纹\n",
            "- 适用范围: 仅本轮受审指纹\n  status: confirmed\n  level: noted\n",
        )
        self.assertEqual(check_report(text), [])


class CheckAuditGateV3Tests(unittest.TestCase):
    """schema_version 3（audit-state 围栏）契约：第 12 轮 sidecar 重构。

    机器状态唯一真源为正文内唯一 ```audit-state``` 围栏 JSON；散文问题清单
    不再被解析。严格 JSON 解析：篡改要么照常解析受检，要么解析失败 fail-closed。
    """

    def _v3(self, mode="external", issues=None, acks=None):
        state = {
            "issues": issues if issues is not None else [
                {"id": "C-1", "level": "Critical", "status": "closed"}
            ],
            "critical_acks": acks or [],
        }
        fm = (
            "---\ntype: Audit\ntopic: t\ndate: 2026-09-08\nauthor: Reviewer\nstatus: active\n"
            "schema_version: 3\nreviewer_mode: " + mode + "\n"
            "reviewer_ref: " + ("some-cli --model x" if mode == "external" else "session") + "\n"
            "target_path: 02_方案.md\ntarget_sha256: " + SHA_A + "\n"
        )
        if mode == "session":
            fm += "fallback_reason: not_configured\n"
        return fm + "---\n\n```audit-state\n" + json.dumps(state, ensure_ascii=False) + "\n```\n"

    def test_valid_v3_external_closed_passes(self) -> None:
        self.assertEqual(check_report(self._v3()), [])

    def test_v3_session_cannot_close_critical(self) -> None:
        issues = check_report(self._v3(mode="session"))
        self.assertTrue(any("不可将 Critical 置为 closed" in i for i in issues))

    def test_v3_missing_fence_blocked(self) -> None:
        text = self._v3().split("```audit-state")[0]
        self.assertTrue(any("围栏出现 0 次" in i for i in check_report(text)))

    def test_v3_duplicate_fence_blocked(self) -> None:
        text = self._v3() + "\n```audit-state\n{\"issues\": [], \"critical_acks\": []}\n```\n"
        self.assertTrue(any("围栏出现 2 次" in i for i in check_report(text)))

    def test_v3_invalid_json_fail_closed(self) -> None:
        text = self._v3().replace("\"status\": \"closed\"", "\"status\": \"closed\",,")
        self.assertTrue(any("不是合法 JSON" in i for i in check_report(text)))

    def test_v3_duplicate_issue_id_blocked(self) -> None:
        issues = check_report(self._v3(issues=[
            {"id": "C-1", "level": "Critical", "status": "closed"},
            {"id": "C-1", "level": "Major", "status": "open"},
        ]))
        self.assertTrue(any("重复出现" in i for i in issues))

    def test_v3_waived_requires_matching_ack(self) -> None:
        waived = [{"id": "M-1", "level": "Major", "status": "waived_by_user"}]
        no_ack = check_report(self._v3(issues=waived))
        self.assertTrue(any("无对应确认" in i for i in no_ack))
        wrong_fp = check_report(self._v3(issues=waived, acks=[
            {"issue_id": "M-1", "target_sha256": SHA_B, "confirm_event": "x", "scope": "y"}
        ]))
        self.assertTrue(any("复核对象已变化" in i for i in wrong_fp))

    def test_v3_ack_unknown_issue_id_blocked(self) -> None:
        issues = check_report(self._v3(acks=[
            {"issue_id": "X-9", "target_sha256": SHA_A, "confirm_event": "x", "scope": "y"}
        ]))
        self.assertTrue(any("指向不存在的问题 ID" in i for i in issues))

    def test_v3_prose_not_parsed(self) -> None:
        """散文中的状态行不再是机器契约——围栏是唯一真源，散文仅供人读。
        此前 5 轮外置复核的格式变体攻击面对 v3 整体不适用。"""
        text = self._v3() + "\n### 问题 9\n级别: Critical\nID: C-9\n状态: closed\n"
        self.assertEqual(check_report(text), [])

    def test_v3_essay_weapons_only_fail_closed(self) -> None:
        """对 v2 有效的散文绕过形态（注释隐藏/零宽/变体/Setext）对 v3 无效：
        围栏标签被破坏时只会得到"围栏 0 次"的 fail-closed，不存在放行路径。"""
        text = self._v3().replace("```audit-state\n", "```audit-state-broken\n")
        self.assertTrue(any("围栏出现 0 次" in i for i in check_report(text)))


    def test_v3_duplicate_json_key_fail_closed(self) -> None:
        """第 12 轮实测：`"status":"closed","status":"open"` 被 Python 静默取
        末值；重复键即歧义载荷，fail-closed 拒绝。"""
        text = self._v3(mode="session").replace(
            "\"status\": \"closed\"", "\"status\": \"closed\", \"status\": \"open\""
        )
        self.assertTrue(any("重复键" in i or "不是合法 JSON" in i for i in check_report(text)))

    def test_v3_dollar_prefixed_key_smuggle_blocked(self) -> None:
        """第 12 轮实测：`$` 前缀豁免被用于走私 "$issues" 穿过
        additionalProperties:false；豁免只保留给标准 JSON-Schema 指令键。"""
        text = self._v3(mode="session").replace(
            "\"critical_acks\": []",
            "\"critical_acks\": [], \"$issues\": [{\"id\": \"C-9\", \"level\": \"Critical\", \"status\": \"closed\"}]"
        )
        self.assertTrue(any("未声明字段" in i for i in check_report(text)))

    def test_v3_non_object_top_level_reported_not_raised(self) -> None:
        """第 12 轮第三批：围栏 JSON 顶层为 null/数组/字符串时返回违规说明，
        不得抛未捕获异常（接口契约）。"""
        for raw in ("null", "[]", '"closed"'):
            text = self._v3().replace(
                '{"issues": [{"id": "C-1", "level": "Critical", "status": "closed"}], "critical_acks": []}',
                raw,
            )
            issues = check_report(text)
            self.assertTrue(any("顶层必须是 JSON 对象" in i for i in issues), raw)

    def test_v3_fm_duplicate_reviewer_mode_blocked(self) -> None:
        """第 12 轮终验：session 后补写第二行 external 不得凭末值获得外置特权。"""
        text = self._v3(mode="session").replace(
            "reviewer_mode: session\n", "reviewer_mode: session\nreviewer_mode: external\n"
        )
        self.assertTrue(any("重复出现 2 次" in i for i in check_report(text)))

    def test_v3_fm_duplicate_schema_version_blocked(self) -> None:
        """第 12 轮终验：重复 schema_version 3/2 不得令 v3 围栏被 v2 分支忽略。"""
        text = self._v3(mode="session").replace(
            "schema_version: 3\n", "schema_version: 3\nschema_version: 2\n"
        )
        self.assertTrue(any("重复出现 2 次" in i for i in check_report(text)))

    def test_v3_zero_width_schema_version_key_routes_correctly(self) -> None:
        """第 12 轮终验：schema_version 键注入零宽字符不得降级 legacy 放行。"""
        zw = "​"
        text = self._v3(mode="session").replace(
            "schema_version: 3\n", f"schema{zw}_version: 3\n"
        )
        self.assertTrue(check_report(text))

    def test_v3_nested_item_type_errors_reported_not_raised(self) -> None:
        """第 12 轮终验：issues/critical_acks 元素错型返回违规说明，不抛异常。"""
        text = self._v3(mode="session").replace(
            "{\"issues\": [{\"id\": \"C-1\", \"level\": \"Critical\", \"status\": \"closed\"}], \"critical_acks\": []}",
            "{\"issues\": [null], \"critical_acks\": [42]}",
        )
        issues = check_report(text)
        self.assertTrue(any("元素必须是对象" in i for i in issues))
        text2 = self._v3(mode="session").replace(
            "{\"issues\": [{\"id\": \"C-1\", \"level\": \"Critical\", \"status\": \"closed\"}], \"critical_acks\": []}",
            "{\"issues\": \"oops\", \"critical_acks\": []}",
        )
        self.assertTrue(check_report(text2))



    def test_v3_leading_invisible_chars_before_delimiter(self) -> None:
        """第 12 轮第五批：BOM+零宽组合位于开头 --- 之前不得令 FM 整体失配
        降级 legacy 放行。"""
        text = "\ufeff\u200b" + self._v3(mode="session")
        self.assertTrue(any("不可将 Critical 置为 closed" in i for i in check_report(text)))


    def test_v3_leading_newline_class_and_fence_guard(self) -> None:
        """第 12 轮第六批：前导隐形字符+换行不再降级 legacy；FM 彻底不可解析
        但围栏存在时直接拒绝。"""
        text = "\ufeff\u200b\n" + self._v3(mode="session")
        self.assertTrue(any("不可将 Critical 置为 closed" in i for i in check_report(text)))
        no_fm = self._v3(mode="session").split("---", 2)[-1]
        self.assertTrue(any("Front Matter 无法解析" in i for i in check_report(no_fm)))


    def test_v3_version_removed_with_fence_blocked(self) -> None:
        """第 12 轮第七批：删除 schema_version 但保留围栏，只读路径不得
        降级 legacy 无视机器状态。"""
        text = self._v3(mode="session").replace("schema_version: 3\n", "")
        self.assertTrue(any("schema_version 不是 3" in i for i in check_report(text)))


if __name__ == "__main__":
    unittest.main()
