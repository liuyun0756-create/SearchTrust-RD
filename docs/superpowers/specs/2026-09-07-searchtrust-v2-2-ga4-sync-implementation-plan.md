# V22-061 GA4 synchronization implementation plan

Design source: `2026-09-07-searchtrust-v2-2-ga4-sync-design.md`.
Production rollout keeps all Google sync feature flags disabled.

## 1. Extend the durable database lifecycle

- Add one additive frontend Supabase migration after `20260906000000`.
- Extend `google_sync_jobs` to accept `ga4`, persist a constrained host filter,
  and retain existing GSC rows and behavior.
- Add GA4 request/claim/finish/fail RPC boundaries with the same lock ordering,
  ownership, scope, idempotency, lease and retry guarantees as GSC.
- Validate `ga4_sync_v1`, exact Property/date/host identity and health payload in
  the completion transaction before inserting an immutable GA4 snapshot.
- Add PostgreSQL-semantic regression scenarios for cross-source isolation,
  idempotency, active conflicts, lease fencing, recovery, identity changes,
  invalid payloads, immutable history and the unchanged GSC path.

## 2. Implement the bounded GA4 provider

- Add focused backend models for configured key events, report metadata,
  totals, landing pages, dates, event rows, periods and `ga4_sync_v1`.
- Implement fixed-origin Admin `keyEvents.list` pagination with loop and page caps.
- Implement eight fixed Data API `runReport` requests with exact hostname/web
  filtering, fixed current/previous periods, deterministic headers and row limits.
- Normalize HS-safe numbers, dates, metadata, restrictions and truncation without
  retaining raw responses, query strings, identifiers or provider error bodies.
- Add a pure health evaluator matching the approved unhealthy/warning rules.
- Test request bodies, boundaries, pagination, malicious/invalid responses,
  Google error classification, metadata limitations and every health branch.

## 3. Reuse the broker, repository and ARQ worker safely

- Generalize only the source-independent broker/repository pieces; keep GSC public
  behavior unchanged and require the exact GA4 readonly scope for GA4 tokens.
- Add GA4 repository calls, reconciliation and execution functions with unique
  queue job IDs, bounded timeout, at most three database attempts and lease recovery.
- Add disabled-by-default GA4 worker configuration and lifecycle-managed HTTP client.
- Test that disabled workers enqueue nothing, tokens are acquired only after claim,
  retries never carry tokens, old leases cannot finish and logs contain fixed codes.

## 4. Add the private frontend request/status boundary

- Add a GA4 sync service using narrow Supabase projections and fixed browser-safe errors.
- Add an authenticated, origin-checked, size-limited GA4 sync route gated by
  `GOOGLE_GA4_SYNC_ENABLED` plus existing Google connection configuration.
- Reuse the minimal sync control interaction while keeping source-specific copy,
  health reasons, limitations and request endpoints explicit.
- Render it only for an active GA4 binding on the private connections page.
- Test disabled/auth/input/ownership/conflict/status/success/failure/expiration,
  prior-snapshot retention, polling cancellation and public response projections.

## 5. Verify the complete change

- Run frontend focused tests during development, then the complete frontend suite,
  typecheck, scoped lint, production build and `git diff --check`.
- Run backend focused tests during development, then complete pytest and
  `git diff --check`.
- Review changed code for token/error leakage, unbounded payloads, unsafe origins,
  source confusion, date overlap, additive-metric mistakes and stale identity writes.
- Update change-management and write an implementation/deployment completion record.

## 6. Deploy in safe order

- Commit and push tested code.
- Apply and verify the linked production database migration before application rollout.
- Confirm frontend and worker GA4 flags, broker origin and secret remain absent/off.
- Let Git deployments publish frontend, API and Worker; verify exact commits and status.
- Perform read-only production homepage, API, queue and error-log checks without a
  real Google call or purchase.
- Record deployment IDs, build duration, health results, remaining live-acceptance
  gap and the next V22-062 milestone.

## Completion gate

V22-061 is complete when migration, code, automated verification and flag-off
production rollout succeed. Real Google acceptance remains a separate credentialed
operation and must not be represented as complete by mocked tests.
