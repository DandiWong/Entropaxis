---
type: Audit
topic: <中文主题>
date: <YYYY-MM-DD>
author: Reviewer
status: active
schema_version: 4
audit_phase: <plan|impl>
reviewer_mode: <external|session>
reviewer_ref: <承载命令原文；会话内承载写 session>
fallback_reason: <仅 reviewer_mode: session 时填：not_configured|not_executable|launch_failed|timeout|no_valid_output>
target_path: <受审对象相对本报告的路径，文件或目录>
target_sha256: <check_audit_gate.py --fingerprint 的输出>
---

# <工件名称>审计报告

```audit-state
{
  "issues": [
    {"id": "<问题 ID>", "level": "<Critical|Major|Minor>", "status": "open", "gate": "<plan|impl|release|none>", "basis": "<G-/NG-/AC- 编号或项目硬约束>", "evidence": "<measured|traced|inferred>"}
  ],
  "critical_acks": []
}
```

## 当前结论

- **结论**：<通过 | 附条件通过 | 阻断退回>
- **审计轮次**：第 <N> 轮
- **受审指纹**：`<target_sha256>`
- **门禁**：<方案轻审 plan | 实施主审 impl>
- **阻断问题**：<update_audit_state.py --list 末行：本阶段阻断数与 ID；没有则写"无">
- **待实施 / 上线前提 / 另立事项**：<gate 为 impl / release / none 的 open 数量与 ID>
- **终审条件**：<尚未满足的门禁；已通过则写"全部满足">

## 审计边界

- **受审目标**：<路径或范围>
- **审计目标**：<质量、安全、合规或验收门禁>
- **不在范围**：<明确排除项；没有则写"无">
- **前序事件**：<前序报告链接；没有则写"无">

## 问题说明

机器状态（ID、级别、状态）只以上方 `audit-state` 为准；本节只写人读的说明，不重复状态字段。

### <问题 ID>

- **现象与证据**：<measured 写命令与结果；traced 写从真实入口逐跳的 path:line>
- **复现**：<实施主审的 Critical/Major 必填，与围栏 repro 一致>
- **根因**：<查证的原因；未查明写"原因未查明">
- **风险**：<影响>
- **整改建议与关闭条件**：<可验证的条件>

## 轮次记录

### 第 1 轮 · <YYYY-MM-DD>

- **受审指纹**：`<target_sha256>`
- **前序问题复核**：首次审计，无前序问题。
- **新增问题**：<问题 ID；没有则写"无">
- **结论**：<通过 | 附条件通过 | 阻断退回>，依据：<对应门禁和问题证据>
