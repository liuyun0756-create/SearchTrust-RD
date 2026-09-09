# V22-080 Task reliability completion

Completed: 2026-09-09  
Status: implemented, verified and deployed to production

## Delivered

- Added Redis-shared run leases, generation-fenced state/checkpoint/result writes,
  30-second heartbeat, 180-second stale takeover and an immutable 20-minute deadline.
- Kept automatic transient execution to three total attempts and made the legacy
  same-logical-job retry endpoint require a new paid attempt instead.
- Added shared provider-and-operation circuits with five failures per rolling minute,
  one half-open probe and 60/120/300-second cooldowns.
- Added SerpAPI key-level circuits: auth, quota and rate rejection isolate immediately;
  three consecutive transport/service failures isolate only that key.
- Added `analysis_attempt_charges` and `audit_credit_ledger`, with one charge per logical
  job and unique per-job compensation provenance.
- The first attempt consumes the paid Case entitlement. Terminal failure closes it as
  `compensated` and returns exactly one permanent account credit. A later attempt gets a
  new job/idempotency identity and debits one account credit.
- Added generation-aware callback settlement and result persistence; duplicate, stale
  and former-generation writes cannot repeat financial effects.
- Added authorized latest-task lookup, authenticated SSE proxy, revision ordering and
  polling fallback capped at one request per ten seconds.
- Updated recovery copy so a failed attempt states that one credit was returned and the
  new action clearly states that generating again uses one credit.

## Verification

- Backend full suite: 1,584 tests passed.
- Frontend full suite: 561 tests passed.
- Frontend TypeScript check: passed.
- Frontend production build: passed.
- Cross-repository v2.2 contract check: passed.
- Additive migration replay and behavior suite: 20 tests passed inside the frontend
  full suite.
- Railway API health: `ok`.
- Railway queue health: Redis connected, Worker alive, zero pending callbacks.
- Vercel post-deploy error scan: no errors found.
- Production compensation drill: one paid Case attempt failed, its charge became
  `compensated`, the entitlement became `compensated`, exactly one ledger entry was
  created and the balance became one; replaying the identical failure did not add a
  second credit. The synthetic user and all cascaded test records were then deleted.

## Production release

- Supabase migration: `20260909170000_add_v2_2_task_reliability.sql`, verified present
  in the remote migration ledger.
- Backend commit: `8bfd6f7`.
- Railway production API deployment: `2e66eebe-5dd4-48c1-8abf-6bdb2c815fa1`.
- Railway production Worker deployment: `e1860fcb-e59e-4987-b075-a05de11d293c`.
- Frontend commit: `94d8133`.
- Vercel production deployment: `dpl_HGkRVf6T6yFFic3kK5G9HKaAVTz6`.
- Production alias: `https://trysearchtrust.com`.

## Release boundary

`V22_ANALYZE_ENABLED` remains enabled for the existing prospect flow. GSC, GA4 and
official GBP synchronization remain disabled by their default false settings, and
Verified Generation is not activated by V22-080.
