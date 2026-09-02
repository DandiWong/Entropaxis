---
audit_event_id: "<stable-event-id>"
target: "<artifact path or scope>"
audit_objective: "<objective and acceptance gates>"
decision: "<rejected|conditional-approval|approved>"
review_round: 1
current_snapshot: "sha256:<snapshot-id>"
previous_snapshot: null
finalized_at: null
---

# <工件名称>审计报告

## 当前结论

- **结论**：<decision>
- **审计轮次**：第 <N> 轮
- **当前快照**：`<snapshot-id>`
- **阻断问题**：<数量与 ID；没有则写“无”>
- **终审条件**：<尚未满足的门禁；已通过则写“全部满足”>

## 审计边界

- **受审目标**：<路径或范围>
- **审计目标**：<质量、安全、合规或验收门禁>
- **不在范围**：<明确排除项；没有则写“无”>
- **前序事件**：<前序报告链接；没有则写“无”>

## 当前快照

| 路径 | SHA-256 / 可验证版本 |
|---|---|
| `<relative-path>` | `<fingerprint>` |

## 问题台账

| ID | 严重度 | 状态 | 首现轮次 | 当前证据 | 风险 | 关闭条件 |
|---|---|---|---:|---|---|---|
| A-001 | blocking / major / minor | open / closed / pending-confirmation | 1 | `<path:line 或其他证据>` | <风险> | <可验证条件> |

## 轮次记录

### 第 1 轮 · <YYYY-MM-DD>

#### 受审快照

- `current_snapshot`: `<snapshot-id>`
- `previous_snapshot`: `null`

#### 前序问题复核

首次审计，无前序问题。

#### 新增问题

- <问题 ID、证据与关闭条件；没有则写“无”>

#### 结论

- **决定**：<decision>
- **依据**：<对应审计门禁和问题证据>
