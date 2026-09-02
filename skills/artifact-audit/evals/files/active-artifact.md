# Access design

Acceptance gates:

1. Every data read is tenant-scoped.
2. Administrators can revoke active sessions immediately.
3. Security audit records identify actor, action, time, and resource.

Current design:

- All document reads, including export, require `tenant_id` from the authenticated session.
- Revoking a session invalidates it before the next request.
- Audit records contain action, timestamp, and resource ID. Actor ID is not recorded.
