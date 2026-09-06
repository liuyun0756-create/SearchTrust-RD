# V22-060 GSC synchronization — implementation and deployment record

Date: 2026-09-06. Approved scope: the GSC synchronization design dated today.
Implementation is deployed with Google feature flags OFF. This is not a claim
of live Google-account acceptance or availability to end users.

## Delivered

- User-requested durable synchronization, private request/status endpoint and
  minimal control on the selected GSC resource. Merely opening a page never
  starts collection. One active job per binding; request UUIDs are idempotent.
- Current 90 days versus preceding 90 days, with a fixed Pacific-time end date
  three days behind the request. Independent totals/query/page/date/device/country
  views retain clicks, impressions, CTR and position without summing overlapping
  breakdowns. Query/page detail is capped at 1,000 rows across both periods;
  truncation, query privacy, web-only and property-scope limitations are explicit.
- Health reasons for absent current data, prolonged interior activity gaps and
  stale recent activity. Low traffic is not asserted to be broken tracking.
  Prior-period absence is a limitation. Snapshot freshness is seven days.
- Immutable snapshots, transactional ownership/identity/Case-revision validation,
  fenced leases, at most three attempts and recovery of expired executions.
  Failures retain previous snapshots. The reconciliation timer only dispatches
  pending user requests; it does not initiate scheduled Google collection.
- Signed short-lived token-broker requests. Tokens remain in worker memory, not
  queue arguments, snapshots, logs or browser responses. HTTP responses are bounded
  and normalized; public errors use fixed codes, not provider response bodies.

GA4/GBP synchronization, full Connection Center, report-envelope adapters and
verified-report generation remain outside this milestone.

## Verification

- Frontend: 51 test files, **489 tests passed**; typecheck and production build passed.
- Backend: **1,442 tests passed** with `.venv/bin/python -m pytest -q`.
- Scoped ESLint using the installed native Next flat configurations passed.
  The repository's existing global ESLint configuration was not changed.
- Both repositories passed `git diff --check` before code commits.
- Database tests use actual PostgreSQL semantics through PGlite, including
  ownership, request deduplication, leases, stale completion, retries, immutable
  history and invalidated identity. Provider tests use mocked HTTP responses.
- No authenticated browser/real Google sync was executed. No real purchase,
  data deletion or Google feature activation was performed.

## Database

Applied `20260906000000_add_v2_2_gsc_sync.sql` before publishing the applications.
The linked production migration catalog and local catalog both show version
`20260906000000`. Adds service-role-only `google_sync_jobs` and lifecycle RPCs;
existing snapshots/reports remain intact. There is no outstanding manual SQL step.

## Deploy Result

- **URL**: https://search-trust-r2w4g3xif-liuyuns-projects-9eb2d9a4.vercel.app
- **Production alias**: https://trysearchtrust.com
- **Target**: production
- **Status**: READY
- **Deployment ID**: `dpl_EsPpH7cTRAxm3yyS3WkDuLdK6A2h`
- **Frontend commit**: `6736ee4`
- **Framework**: Next.js 16.2.4
- **Build Duration**: 41 seconds in build logs; 53 seconds total deployment in listing.
- **Completed**: 2026-09-06 02:48:39 UTC.
- **Backend implementation commit**: `047184b`
- Railway production API and Worker: both SUCCESS / RUNNING at that commit.
- Railway production Redis: SUCCESS / RUNNING.

### Post-Deploy Observability

Read-only checks at approximately 2026-09-06 02:51 UTC:

- Production homepage: HTTP 200.
- Signed-out private connections page and GSC-sync status endpoint: HTTP 404.
  Middleware protection may account for this response; this is not an assertion
  that the disabled handler itself returned 503.
- Backend health: `status=ok`.
- Queue health: `status=ok`, Redis connected, worker alive, pending callbacks 0.
- **Error scan**: 0 returned error entries in the deployment's preceding 10-minute
  Vercel log scan (limit 30). This is a point-in-time check, not proof of zero future errors.
- **Drains**: configuration not audited in this milestone.
- **Monitoring**: deployment and health checks performed manually; ongoing external
  alert coverage is unverified. No recurring monitoring automation was created.

Production frontend Google environment entries were absent. Worker
`V22_GSC_SYNC_ENABLED` was absent/off; broker origin and secret were not configured.
No credentials or flag values were changed.

## Activation and next step

Before activation, configure approved Google OAuth credentials/scopes and matching
frontend/worker broker secrets, then validate a real owned test Case. Enable the
coordinated frontend and worker flags only for that acceptance workflow. Include
successful sync, retry, reconnect/revocation and snapshot freshness checks. Disable
flags to stop new work on rollback; preserve audit and snapshot history.

Next planned implementation milestone: V22-061 GA4 connector. Live GSC acceptance
remains separately pending credentials and account access.
