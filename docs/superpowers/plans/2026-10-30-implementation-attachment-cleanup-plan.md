# Implementation attachment cleanup execution

- [x] Inspect installed cleanup RPCs, storage protocol and Worker configuration.
- [x] Choose a bounded native Worker cron within the approved backend scope.
- [x] Implement transport, candidate validation, deletion verification and lease.
- [x] Add focused unit and isolated Redis tests; run backend quality gates.
- [ ] Review diff and publish the tested revision.
- [ ] Enable production Worker flag and observe two automatic runs.
- [ ] Record real results, unchanged business state and remaining acceptance limits.

Local gates: 41 focused unit tests; 3,426 backend fast-suite tests; 28 isolated
real Redis tests (Redis 7.4.0 compiled in a temporary directory, bound only to
127.0.0.1, no persistence, terminated after testing). All passed.

## Operations

Set `V22_IMPLEMENTATION_ATTACHMENT_CLEANUP_ENABLED=true` only on the existing
production Worker, then deploy/restart it. Look for the startup `enabled` entry
and minute `result` entries. The latest owner-confirmed receipt is stored at
`<V22_REDIS_PREFIX>:maintenance:implementation-attachments:last-run` for seven
days; it contains timestamps, a run ID, status and counters only. Busy skips do
not replace it. On failure/timeout counters are unknown, not a claimed zero.
`partial` means individual candidates remain due for retry. `receipt_unavailable`
means the log exists but Redis recording was not confirmed.

Rollback: set the flag false and redeploy/restart the Worker. This does not alter
reports, attachment metadata or retention rules. Existing SQL cleanup RPCs and
the manual frontend maintenance script remain available. Do not run a manual
cleanup concurrently with automatic maintenance (the legacy script has no Redis
lease). No new database migration or public endpoint is introduced.
