# Changelog

All notable changes to the `artifact-audit` skill will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-09-27

### 变更
- 报告模板对齐 `audit_report.schema.json` 的 `schema_version: 3`：Front Matter 必填字段、`audit-state` 围栏承载机器状态、级别 Critical/Major/Minor、状态 open/closed/waived_by_user。
- 结论取值改为《治理指令》的 通过 / 附条件通过 / 阻断退回。
- 指纹改用 `check_audit_gate.py --fingerprint` 计算，状态改用 `update_audit_state.py` 字段级写入，不再手算、手改。

## [1.0.0]

### 新增
- 首次发布连续审计生命周期 Skill。
