# V22-060 GSC sync implementation plan

The user approved the queue-based design on 2026-09-06. This document records
the implementation decisions; no periodic collection or Google flag activation.

## Scope and boundaries

Reuse the approved V22-050 short-lived token broker. Refresh tokens remain in
Next.js. A private Next.js endpoint checks Clerk ownership and requests a durable
Supabase sync job. Railway's existing ARQ worker dispatches only user-requested
jobs; its reconciliation timer does not create recurring collection requests.
The worker fetches Google, normalizes results and atomically persists an immutable
snapshot plus job result. No changes to report generation, billing, GA4 or GBP sync.

## Data contract

- Fixed web search, final data, Pacific calendar. End date is request date minus
  three days; current period is 90 inclusive days and previous is the preceding
  90 days. The requested end date remains fixed across retries.
- Twelve bounded requests: each period has independent totals, query, page,
  date, device and country views. Totals are not summed from privacy-filtered rows.
- Query/page detail cap is 1,000 across both periods (250 per view per period).
  Fetch one extra row to detect a cap. Dates (90), devices (3), countries (250)
  are separate bounded breakdowns, never added together as independent totals.
- Store versioned gsc_sync_v1 payload, period ranges, metrics and dimensions,
  truncation flags, source aggregation type and limitation codes. No raw Google
  response, token or HTTP exception body is persisted or returned to the browser.
- Current zero impressions, absent query/page rows, a gap of 14 consecutive days
  between activity dates, or over 7 days without recent activity require review
  (unhealthy). Gaps do not assert broken tracking: low-traffic sites can be quiet.
  Missing comparison period is a limitation, not fabricated zero-percent change.
- Every result states query privacy filtering, top-row limits, web-only scope and
  selected-property scope. A confirmed parent property is not silently filtered
  to the Case URL. Snapshot freshness is seven days; expiration is evaluated on read.

## Durable execution and safety

New service-role-only google_sync_jobs table and narrow RPCs: request, claim,
finish and fail. Jobs retain owned binding, connection and Case revision. One
active job per binding; user request UUID is idempotent. Claim issues a lease and
increments an attempt count (maximum three). Expired leases are recoverable;
transient errors retry with a delay. Final failure retains prior snapshots.
No access token in job arguments, Redis or database payloads.

Validate active owner, binding identity confirmation, scopes and Case revision
both before acquiring a token and under locks before snapshot commit. Revocation,
replacement or Case edits prevent old jobs from updating current health. Database
lock order follows connection → Case → binding → job. Duplicate completion is
idempotent. Existing snapshots/report history remain untouched.

Frontend GOOGLE_GSC_SYNC_ENABLED and backend V22_GSC_SYNC_ENABLED default off.
The frontend also requires GOOGLE_CONNECTIONS_ENABLED and its server config.
Minimal GSC sync/status UI lives on the private resource page. Full Connection
Center, report adapters and verified-report generation remain later milestones.

## Checklist

- [x] Add SQL job lifecycle and transactional snapshot persistence, with DB tests.
- [x] Implement typed GSC provider, bounded data normalization and health tests.
- [x] Implement signed token-broker client, job repository and leased worker.
- [x] Wire private request/status endpoint and minimal sync control with tests.
- [x] Run backend/frontend tests, type checks, build, migration dry-run and review.
- [x] Apply migration before deployment; keep flags off; check production health.

Self-review: no unresolved product choices, no all-query completeness claim,
no current/previous overlap, no summing overlapping breakdowns, no automatic
sync initiation, no changes to public reports. Google live acceptance remains
pending production credentials and approvals.

References:
- https://developers.google.com/webmaster-tools/v1/searchanalytics/query
- https://developers.google.com/webmaster-tools/v1/how-tos/all-your-data
