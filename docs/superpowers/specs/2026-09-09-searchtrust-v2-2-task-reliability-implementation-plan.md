# V22-080 Task reliability implementation plan

Design source: `2026-09-09-searchtrust-v2-2-task-reliability-design.md`.
This milestone hardens paid report generation across `SearchTrust-RD` and
`search-trust`. It does not enable Google sync or Verified Generation.

## 1. Freeze reliability contracts and policy

- Add the 20-minute logical-job deadline, 30-second heartbeat, 180-second stale
  threshold and bounded retry/circuit settings to backend configuration.
- Extend durable task state and callbacks with deadline, generation and terminal
  settlement information while preserving payload-free public errors.
- Add deterministic retry classification for validation, provider, database and
  callback failures.
- Cover the new contracts and policy with focused tests before changing execution.

## 2. Add immutable charge and compensation accounting

- Add an additive Supabase migration for per-attempt charge records and an immutable
  credit ledger with uniqueness constraints for idempotent reservation and settlement.
- Make the first attempt consume the paid Case entitlement and later attempts consume
  one account credit from `users.audit_credits`.
- On terminal technical failure, close the attempt as compensated and return exactly
  one general account credit; never reopen the Case entitlement.
- Fence database callbacks by task generation and make duplicate/out-of-order terminal
  callbacks harmless.

## 3. Add leases, fencing, heartbeat and stale takeover

- Introduce a Redis lease carrying task ID, generation and an opaque token digest.
- Require the expected generation for worker state writes and checkpoint persistence.
- Refresh heartbeat every 30 seconds while work is active and stop a superseded worker
  from publishing progress, report data or terminal state.
- Replace same-generation stale requeue with an atomic generation increment and a new
  physical queue identity; fail and compensate jobs past the immutable deadline.

## 4. Add shared provider circuit breakers

- Store provider-and-operation circuit state in Redis with closed, open and half-open
  transitions and 60/120/300-second cooldowns.
- Open after five eligible failures in a rolling minute; allow one half-open probe.
- Isolate individual SerpAPI keys immediately for quota/auth rejection and after three
  consecutive transport/service failures, while allowing healthy configured keys.
- Exclude validation, deterministic data absence and user-input failures from breaker
  accounting; add concurrency and recovery tests.

## 5. Make a retry a new paid logical attempt

- Replace the public same-task reopen route with creation of a new task ID and a new
  idempotency key after atomically reserving a fresh credit.
- Keep the prior failed task immutable and link the new attempt to its Case and previous
  attempt for auditability.
- Preserve completed same-task checkpoints only for automatic retries; do not share
  mutable execution state between separate paid attempts.
- Verify insufficient-credit, duplicate-submit and concurrent retry behavior.

## 6. Add server-owned reconnection and recovery UI

- Add a server lookup for the latest authorized task for a Case.
- Prefer authenticated SSE for live progress, fall back to polling at no more than one
  request per 10 seconds, and ignore events older than the highest seen revision.
- Resume the real server task after refresh/reopen and expose clear states for reconnect,
  stale takeover, circuit cooldown, compensation pending and compensated failure.
- Update the retry action to create a new attempt and explain that it consumes one credit;
  preserve the user's Case draft independently of browser-session task ownership.

## 7. Verify and release safely

- Run focused backend, migration, frontend recovery and integration suites, followed by
  the complete backend and frontend test/type/build checks.
- Apply the additive production database migration before deploying backend and frontend.
- Verify API, Worker, queue health, callback settlement and one controlled failure-credit-
  retry drill in production.
- Record exact results and keep Google sync and Verified Generation disabled.

## Completion gate

V22-080 is complete only when every logical generation is fenced, stale ownership can be
taken over safely, retries and circuits are bounded, every failed paid attempt returns
exactly one permanent account credit, every retry creates and charges a new logical task,
refresh/reopen resumes server-owned progress, full regressions pass and production health
plus the controlled compensation drill are verified.
