# V22-061 GA4 synchronization — implementation and deployment record

Date: 2026-09-07. Scope source: the approved GA4 synchronization design dated
today. Code and migration are deployed to production with GA4 and GSC sync flags
OFF. This record does not claim live Google-account acceptance.

## Delivered

- Explicit user-requested GA4 synchronization on the existing durable Google job
  lifecycle. Opening the resource page does not initiate collection. Request UUIDs
  are idempotent and each binding has at most one queued/running job.
- Current complete 90 days versus the preceding 90 days, fixed two days behind the
  request. Eight Data API reports cover aggregate sessions/users/engagement/views/
  key events, top 250 landing pages, daily activity and top 100 key-event rows.
- Every Data API request filters to web and the Case hostname. Registrable apex and
  `www` are treated as one site; other subdomains remain exact. The production API
  derives this scope server-side using the Public Suffix List through `tldts`.
- Admin API key-event discovery distinguishes no configured key events from configured
  events with zero current activity. No-configuration is unhealthy; configured-zero
  is a warning, matching the approved product rule.
- Whitelisted sampling, thresholding, `(other)` data loss, empty reason, Property
  timezone and metric restriction metadata. Top-row and measurement limitations are
  explicit; breakdowns are never summed into totals or presented as complete data.
- Health evaluation for absent current sessions/landing pages/dates/key-event setup,
  stale recent activity and 14-day interior gaps. Wording requires review without
  claiming low traffic proves broken tracking.
- Transactional Case/connection/binding/Property/host revalidation, fenced leases,
  three attempts, expired-run recovery and immutable seven-day snapshots. A failed
  attempt cannot persist a partial result or overwrite a previous successful snapshot.
- Signed short-lived GA4 token broker requests. Tokens remain in Worker memory and
  never enter job arguments, database payloads, browser responses or logs.

Not included: real-time reports, user-level data, query strings, event parameters,
advertising cost, revenue/e-commerce detail, custom metrics, GA4 report Findings,
full Connection Center or automatic periodic Google collection.

## Verification

- Frontend: **55 files / 511 tests passed**.
- Backend: **1,466 tests passed** with `.venv/bin/python -m pytest -q`.
- Frontend typecheck, native Next scoped lint and Next.js 16.2.4 production build passed.
- PostgreSQL-semantic migration suite: 18 scenarios passed, including GA4/GSC source
  isolation, ownership, host freezing, idempotency, lease fencing, invalid payloads,
  Case changes and immutable history.
- Linked database dry-run listed only `20260907000000_add_v2_2_ga4_sync.sql`.
- Both repositories passed `git diff --check` before commits.
- Provider verification uses fake HTTP and sanitized aggregate fixtures; CI and this
  rollout made no authenticated or real Google Data/Admin API request.

## Database

Applied `20260907000000_add_v2_2_ga4_sync.sql` before application pushes. The local
and linked production catalogs both list `20260907000000`. The Docker warning affected
only local CLI catalog caching after the successful remote migration; PGlite exercised
the full migration chain. No manual SQL step remains and no existing data was deleted.

## Deploy Result

- **URL**: https://search-trust-1skt9djoi-liuyuns-projects-9eb2d9a4.vercel.app
- **Production alias**: https://trysearchtrust.com
- **Target**: production
- **Status**: READY
- **Deployment ID**: `dpl_8NScdipn3kCncBUd2TrG2W8C5eL2`
- **Frontend commit**: `ae3339d`
- **Framework**: Next.js 16.2.4
- **Build Duration**: 44 seconds in build logs; 56 seconds total in deployment listing.
- **Completed**: 2026-09-07 03:39:03 UTC (11:39:03 Asia/Shanghai).
- **Backend implementation commit**: `c2f637c`.
- Railway production API and Worker: SUCCESS / RUNNING at that implementation commit.
- Railway production Redis: SUCCESS / RUNNING.

### Post-Deploy Observability

Read-only checks at approximately 2026-09-07 03:39 UTC:

- Production homepage: HTTP 200.
- Signed-out private connections page and GA4-sync endpoint: HTTP 404. Middleware
  protection may account for this response; this is not a claim that the disabled
  handler itself returned 503.
- Backend health: `status=ok`.
- Queue health: `status=ok`, Redis connected, Worker alive, pending callbacks 0.
- **Error scan**: 0 returned Vercel error entries in the preceding 10-minute scan
  (limit 30). This is a point-in-time check, not a future-error guarantee.
- **Drains**: configuration not audited in this milestone.
- **Monitoring**: manual deployment and health verification completed; continuous
  external alert coverage is unverified. No recurring automation was created.

Production Vercel contained no GA4/GSC sync flag or broker-secret entries. Railway
Worker `V22_GA4_SYNC_ENABLED` and `V22_GSC_SYNC_ENABLED` were absent/off; broker origin
and secret were not configured. No credential or flag value was changed.

## Activation and next step

Before enabling GA4, configure approved OAuth credentials and matching broker secrets,
then use an owned test Case to verify one real Property. Acceptance must cover apex/
`www` filtering, a non-apex subdomain, key-event configuration versus zero activity,
sampling/threshold metadata, retry, revocation, reconnect and snapshot expiration.
Enable frontend and Worker flags together only for that acceptance workflow.

Next planned milestone is V22-062: read-only GBP synchronization. GA4 report Findings
and the unified three-source Connection Center remain later milestones.
