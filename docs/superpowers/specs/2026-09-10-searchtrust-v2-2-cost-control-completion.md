# V22-082 Cost control completion

Completed: 2026-09-10  
Status: implemented, verified and deployed to production

## Delivered

- Added one Redis-shared, recoverable cost ledger per logical prospect report,
  competitor discovery, GSC sync, GA4 sync and GBP sync job.
- Counted every actual SerpAPI, Firecrawl, Jina, PageSpeed, Dify and Google data
  provider attempt, including failures and conservatively unknown outcomes.
- Shared claims across concurrency, automatic retries and recovery. Checkpoint reuse
  records a hit without duplicating a provider attempt.
- Enforced fixed per-operation claim limits atomically. Core-contract exhaustion fails
  the job and continues to use V22-080's exactly-once credit compensation.
- Recorded job attempts, retry count, wall time, active time, provider duration, Dify
  token usage and configurable integer-microdollar estimates. Missing prices remain
  explicit through `pricing_unknown_units`; no missing price is treated as zero.
- Persisted monotonic terminal summaries in `job_cost_summaries` and Google sync live
  counters in `google_sync_jobs.cost_counters`, with a retrying Redis outbox for report
  and discovery summary delivery.
- Kept all cost fields behind service-role-only database and internal callback
  boundaries. Public task status, report, share and PDF contracts contain no provider
  cost data.
- Retained full report depth: there is no total-dollar budget and no cost-driven report
  truncation or user-facing cost coverage state.

## Verification

- Backend full suite: 1,621 tests passed.
- Frontend full suite: 592 tests passed across 72 files.
- Frontend TypeScript check and production build: passed.
- Supabase migration/database suite: 22 tests passed.
- Sensitive artifact scan: 44 files scanned, zero findings.
- Production database permission check: anonymous access to `job_cost_summaries` was
  denied; service-role access and `google_sync_jobs.cost_counters` were available.
- Public contract checks prove report and discovery status serializers omit both the
  `cost_counters` container and every V1 counter key.
- Railway API health returned `ok`; queue health reported Redis connected, Worker alive
  and zero pending callbacks. The production Worker loaded the cost-summary reconciler.
- Vercel production alias returned HTTP 200, the signed callback endpoint rejected an
  unsigned request with HTTP 401, and the post-deploy 30-minute error scan found zero
  errors.

## Production drills

- An isolated cost-summary drill persisted three job attempts, two retries, four
  checkpoint hits, two provider attempts and two explicitly unknown pricing units.
- A stale ledger revision could not replace the newer terminal summary.
- An isolated technical-failure drill debited one credit, compensated the attempt and
  returned exactly one credit. Replaying the same terminal event did not add another
  compensation.
- Both drills deleted their exact synthetic users. Cascaded Case, job, charge, credit
  ledger and cost-summary checks all returned zero remaining rows.

## Production release

- Supabase migration: `20260910100000_add_v2_2_cost_control.sql`, verified present in
  the remote migration ledger.
- Backend commit: `54cbdcb`.
- Railway production API deployment: `ea0d7442-f257-4eba-8cc5-6e9dcc281dbc`.
- Railway production Worker deployment: `be0d8102-f62c-4030-835b-aa8845378b55`.
- Frontend commit: `7652932`.
- Vercel production deployment: `dpl_DqbkgiMAtEQ7yKEeSNfVrRaPqiao`.
- Production alias: `https://trysearchtrust.com`.

## Release boundary

`V22_ANALYZE_ENABLED` remains enabled for the existing prospect flow. GSC, GA4 and
official GBP synchronization remain disabled through their default false settings.
Verified Generation is not activated by V22-082. Provider pricing variables remain
unset, so production records usage as pricing unknown until real contract rates are
configured.
