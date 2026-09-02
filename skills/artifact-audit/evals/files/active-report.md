---
audit_event_id: "access-design-security"
target: "active-artifact.md"
audit_objective: "Verify tenant isolation, session revocation, and attributable audit records"
decision: "rejected"
review_round: 1
current_snapshot: "sha256:previous-snapshot"
previous_snapshot: null
finalized_at: null
---

# Access design audit

## Current findings

| ID | Severity | Status | Evidence | Closure condition |
|---|---|---|---|---|
| A-001 | blocking | open | Export reads by document ID without tenant scope | Export requires authenticated tenant scope |
| A-002 | blocking | open | Active sessions remain valid after revocation | Revocation invalidates active sessions before the next request |

## Round history

### Round 1

Decision: rejected.
