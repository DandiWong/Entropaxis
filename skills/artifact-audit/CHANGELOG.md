# Changelog

All notable changes to the `artifact-audit` skill will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-10-19
### Changed
- 方案轻审与实施主审拆成两个事件、两份报告（`03_方案审计报告.md` / `05_实施审计报告.md`），各自通过即冻结；实施主审首轮沿用方案报告中 open 的 `gate: impl` 问题 ID。
- 任务书复核轮要求原地更新，禁止另存轮次副本。

## [2.1.1] - 2026-10-16
### Changed
- 压缩 description 至 ≤250 字符，只保留能力一句与触发词，降低常驻元数据开销。

## [2.1.0] - 2026-09-28

### 新增
- `references/reviewer-brief.md`：外置 Reviewer 固定任务书（审/不审清单、问题准入、复核范围）。
- 「多报告合并（Manager）」步骤。

### 变更
- 报告模板升到 `schema_version: 4`：`audit_phase`、open 问题带 `gate` / `basis` / `evidence`（实施主审阻断级带 `repro`）；状态新增 `withdrawn`。
- 结论按本阶段阻断判定；方案轻审转实施主审沿用同一报告，实施主审通过后冻结。

## [2.0.0] - 2026-09-27

### 变更
- 报告模板对齐 `audit_report.schema.json` 的 `schema_version: 3`：Front Matter 必填字段、`audit-state` 围栏承载机器状态、级别 Critical/Major/Minor、状态 open/closed/waived_by_user。
- 结论取值改为《治理指令》的 通过 / 附条件通过 / 阻断退回。
- 指纹改用 `check_audit_gate.py --fingerprint` 计算，状态改用 `update_audit_state.py` 字段级写入，不再手算、手改。

## [1.0.0]

### 新增
- 首次发布连续审计生命周期 Skill。
