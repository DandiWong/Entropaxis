---
name: artifact-audit
description: "对可迭代工件执行可追溯审计，并在连续复审中维护同一份审计报告，逐轮核验旧问题、发现新问题，直到终审通过后冻结。用户要求审计、评审、复审、重新检查某份方案/文档/代码工件，提到既有审计报告，或要求记录问题闭环时应使用；一次性口头点评也先用本 Skill 判断是否需要落盘。"
metadata:
  scope: control-plane
  version: "2.1.0"
---

# Artifact Audit

把审计视为一个有边界的事件，而不是每次修改都生成一份新报告。触发、产物位置与关闭门禁的规则真源见 [`../../rules/治理指令.md`](../../rules/治理指令.md)「审计」；Reviewer 承载与问题终态见 [`../../rules/角色协作.md`](../../rules/角色协作.md)；报告字段契约见 [`../../schemas/audit_report.schema.json`](../../schemas/audit_report.schema.json)（新报告用 `schema_version: 4`）；落盘命名与打开遵循 [`../../rules/文件交付.md`](../../rules/文件交付.md)。

## 输入

确认以下信息；优先从当前会话、目标文件、最近的 `AGENTS.md` 和既有报告中取得，不重复询问：

- 受审目标：一个文件或一组明确文件；
- 审计目标：要验证的质量、安全、合规或验收门禁；
- 既有审计报告：如有，必须先读取；
- 输出方式：仅会话反馈，或维护磁盘报告。

只有用户明确要求落档、生成报告或继续维护既有报告时才写文件。否则完成一次性审计并在会话中返回结论，不创建报告。

外置审计的任务书用 [`references/reviewer-brief.md`](references/reviewer-brief.md)（审什么、不审什么、问题准入、复核范围），由 Manager 填占位符，不临场自拟。

## 工作流

### 1. 界定审计事件

以“同一目标谱系 + 同一审计目标 + 尚未终审通过”判定同一事件：

- 同一事件且已有报告：原地更新该报告，不创建 `v2`、`新版` 或日期不同的副本。
- 既有报告已终审通过（`status: completed`）：保持冻结。目标或审计目标出现新变化时创建新事件，并在新报告中引用前序报告。
- 找到多个疑似活动报告：先按目标、目标范围和状态确定唯一真源；无法确定时停止写入，列出冲突证据。

### 2. 固定受审快照

受审对象可以是一个文件或一个目录（多文件审计把范围收成一个目录）。指纹一律用工具算，不手算：

```bash
python3 .entropaxis/tools/check_audit_gate.py --fingerprint <受审文件或目录>
```

输出写入 Front Matter 的 `target_sha256`，`target_path` 写受审对象相对报告的路径。无法读取稳定字节的外部对象：在正文记录可验证版本、来源和取得时间，不得伪造哈希。

当前指纹与报告最后一轮完全相同时，不增加轮次、不改写报告；直接说明没有新的受审版本。

### 3. 审计并维护问题台账

先复核历史问题，再检查当前工件是否产生新问题：

- 问题 ID 跨轮次稳定；新问题顺序递增，禁止因排序变化重编号。
- 只有当前工件中的可定位证据证明修复完成时才标记 `closed`；承诺、计划或报告文字本身不算关闭证据。
- 级别只用 `Critical` / `Major` / `Minor`，状态只用 `open` / `closed` / `withdrawn` / `waived_by_user`（取值域真源为 schema 的 `*_enum`）。`withdrawn` 用于误报、重复（正文写"重复于 X"）或前提已删除。
- v4 报告的每条 open 问题带 `gate` / `basis` / `evidence`，实施主审的阻断级另带 `repro`，准入要求见任务书。
- 状态只记在 `audit-state` 围栏里，用工具字段级修改，不手改 JSON：
  ```bash
  python3 .entropaxis/tools/update_audit_state.py <报告> --add-issue M-1:Major --gate plan --basis AC-1 --evidence traced
  python3 .entropaxis/tools/update_audit_state.py <报告> --set M-1 status=closed
  ```
  工具写入前自动过 `check_audit_gate.py`；Critical 转 `closed` 须外置 Reviewer，`waived_by_user` 须有用户确认的 `critical_ack`，工具不提供绕过路径。已盖章报告经工具改动后会删去 `receipt_id`，须下一轮外置 Reviewer 复核重新盖章才恢复有效。
- 正文"问题说明"为每个开放问题写现象与证据、根因、风险、整改建议与关闭条件。
- 区分事实与推断；证据不足时保持 `open` 并在说明中写明待确认，不把推断写成事实。

### 4. 作出轮次结论

结论只使用以下值：

- **阻断退回**：本阶段存在阻断问题（`update_audit_state.py --list` 末行，v3 报告按 `open` 的 Critical 判）；
- **附条件通过**：本阶段无阻断，但仍有 `open` 的 Minor，或待实施、上线前提、另立事项；
- **通过**：无任何 `open` 问题（或指定 Critical 记 `waived_by_user`）。

不得用"基本通过""应该没问题"等模糊措辞替代结论。方案轻审通过后，下一轮把 `audit_phase` 改为 `impl`、`target_path` 改指改动目录，沿用同一报告；实施主审通过（无阻断）后把 Front Matter `status` 改为 `completed` 并冻结，门禁会拒绝残留阻断的冻结。

### 5. 维护报告

首次创建报告时使用 [`references/report-template.md`](references/report-template.md)，并按项目既有报告位置与《文件交付》命名规范落盘。后续轮次：

1. 受审对象变化时更新 `target_sha256`；
2. 用 `update_audit_state.py` 更新问题状态，再改"当前结论"与"问题说明"；
3. 在"轮次记录"末尾追加本轮，保留全部历史轮次；
4. 只在更正客观错误时修改历史，并显式记录更正原因；
5. 收尾前运行 `python3 .entropaxis/tools/check_audit_gate.py <报告>`，退出码须为 0；
6. 终审通过后不再改写该报告。

### 6. 多报告合并（Manager）

首轮多家并审时：

1. 按根因去重：保留最早的 ID，其余记 `withdrawn` 并在正文写"重复于 X"。
2. 按任务书「每条问题的准入」逐条复核 `basis` / `evidence` / `gate`：不达标的降级或改 gate，写明原因；`gate: none` 的归类交用户确认。
3. 输出一份主报告交作者；用 `update_audit_state.py --list` 记录本轮阻断数，供熔断比较。
4. 运行 `check_audit_gate.py <主报告>`，直到只剩回执类提示。

## 输出

会话回复保持简洁，至少包含：

- 审计事件与轮次；
- 当前受审指纹；
- 结论；
- 已关闭、新增、仍开放的问题数量与阻断项；
- 报告路径，或“仅会话反馈，未落盘”。

涉及外部写入、敏感信息、个人信息或受控材料时，继续遵守最近的 `AGENTS.md` 和工作区安全规则；本 Skill 不降低任何授权门禁。
