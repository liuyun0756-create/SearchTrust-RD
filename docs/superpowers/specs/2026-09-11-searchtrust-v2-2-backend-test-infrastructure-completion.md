# V22-091 Backend test infrastructure completion

Date: 2026-09-11  
Status: completed and verified in production

## Outcome

SearchTrust V2.2 now has a production-blocking backend quality gate. Pull requests and
pushes to `main` run the deterministic backend suite and an isolated real-Redis restart
suite. Railway production Web and Worker both wait for that GitHub check suite and only
deploy its exact successful commit.

The executable V2.1 report product path has been removed from both repositories. The
independent `/api/v1/health` infrastructure probe remains available; it is not a V2.1
product compatibility surface.

## Delivered test and safety boundaries

- Stable repository entrypoints cover fast and release modes plus the dedicated Redis
  integration run.
- Provider contracts cover SerpAPI, Firecrawl, Dify, Google and delivery adapters with
  sanitized bounded fixtures and stable public errors.
- Normalized Prospect, Verified and changed-only full-report goldens are checked for
  drift.
- Property and idempotency tests exercise task state, credit/refund, callback, share,
  sync and report invariants.
- A real ephemeral Redis 7.4 instance proves enqueue, cancellation, restart recovery,
  stale-worker fencing, callback recovery and cleanup behavior.
- Fast tests fail closed on all unexpected network access. Redis tests permit only their
  explicit loopback endpoint and reject production-like environment or host values.
- Captured diagnostics are bounded, scanned and redacted. Tests do not load production
  environment files, repository secrets or live provider data.
- The workflow has read-only repository permissions, non-persistent checkout
  credentials, bounded job timeouts and no production secrets.

## Final local verification

All commands ran from a clean backend worktree at code commit
`c7a649109b03d9fb10a7eb17c1425a68466db4c7`:

- CI contract: 7 tests passed in 0.46 seconds.
- Fast entrypoint: 1,456 passed and 21 Redis tests deselected in 34.31 seconds.
- Redis entrypoint: 21 passed and 1,456 non-Redis tests deselected in 1.58 seconds.
- Release entrypoint: 1,456 passed in 34.71 seconds, followed by 21 Redis tests in
  1.71 seconds.
- Final active collection: 1,477 tests. Fixture, contract and golden consistency checks
  produced no worktree diff.

No command contacted production Redis, Supabase, Railway data services, Google, Dify,
Firecrawl, SerpAPI, payment providers or any other external origin.

## Frontend V2.1 retirement evidence

- Retirement commit: `4220ff4d5a718f5b317d4e28a48f2407690d28c5`
  (`refactor(v2.2): retire legacy report frontend`).
- Change manifest: 88 files changed, 52 files deleted, 392 insertions and 13,605
  deletions.
- Removed surfaces include legacy checkout/generation/report API routes,
  `/sample-case`, `/test-report`, legacy audit/payment/report components,
  `src/components/report/v21`, `src/lib/report-v21` and the old submit-audit client.
- Final frontend GitHub Actions run:
  `https://github.com/liuyun0756-create/search-trust/actions/runs/34587810546`
  (`#28`), successful in 2 minutes 44 seconds. Its quality job passed in 1 minute
  50 seconds, browser job in 2 minutes 30 seconds and gated deploy job in 6 seconds.
- Frontend verification retained 639 deterministic tests and 11 Playwright journeys.
- Vercel production deployment: `dpl_S8WkTdqQVSt65FkF28FbMbNyrHnV`, status `Ready`,
  serving `https://trysearchtrust.com`. Home, new-Case and sample-report routes returned
  HTTP 200; the retired sample-Case route returned 404 and no error-level runtime event
  was observed.

## Backend V2.1 retirement evidence

- Retirement commit: `fbd26d04655bcfac718a2ed92f3ae63b9a9a10c6`
  (`refactor(v2.2): retire legacy report backend`).
- Change manifest: 114 files changed, 93 files deleted, 453 insertions and 32,916
  deletions.
- Removed surfaces include the legacy analyze, task polling/stream/deletion API,
  in-memory task store, V1 request/response models, `app/report_v21`, the legacy
  pipeline/scraper/Dify/address tasks, their fixtures and 18 dedicated test files.
- The deletion manifest accounted for all 253 retired test cases. Collection lost no
  unlisted active tests.
- Local production-style checks returned HTTP 200 and
  `{"status":"ok","version":"1.0.0"}` from `/api/v1/health`; retired analyze and task
  routes returned 404.

## GitHub and Railway production proof

Final backend GitHub Actions run:
`https://github.com/liuyun0756-create/SearchTrust-RD/actions/runs/34593665540`
(`#11`) completed successfully in 1 minute 57 seconds. `Backend quality` passed in
1 minute 52 seconds and `Redis integration` passed in 36 seconds.

The Railway negative-control deployment was commit
`2d7086b1bf9b8767afb5662d201ac2bc7d44dce8`: Web deployment
`3c6fea33-03ff-4a86-8650-49f8156f8b98` and Worker deployment
`b567247a-ade4-4227-96fb-e37a069592b7` were still serving while the first final gate
run executed. New deployments for `ccf14676b4c91e8962522cf71ce8af76977c04d6`
remained `WAITING` until GitHub Actions #10 succeeded, then both reached `SUCCESS`.

The final logging correction repeated the proof. While GitHub Actions #11 was running,
Railway created Web deployment `0092c603-e606-4120-bb3b-41ed91fb1845` and Worker
deployment `40822911-92a8-46d1-bd75-dd70515dbb29` for exact commit
`c7a649109b03d9fb10a7eb17c1425a68466db4c7`, but kept both in `WAITING`. Only after
the check suite succeeded did both deployments proceed and reach `SUCCESS`.

Production verification at the final code commit:

- `https://searchtrust-rd-production.up.railway.app/api/v1/health` returned HTTP 200
  with `{"status":"ok","version":"1.0.0"}`.
- The first Web log sample contained 6 startup events, zero Railway error-level events
  and zero application error messages.
- The first Worker log sample contained 9 startup/reconcile events, zero Railway
  error-level events and zero application error messages. Normal ARQ activity is now
  explicitly routed to stdout so Railway severity is accurate.

## Unchanged boundaries

- V22-092 remains a separate data-migration milestone: staging migration application,
  historical-report read regression, new-table RLS/service-role checks, the approved
  no-mandatory-backfill policy and its non-destructive rollback boundary are unchanged.
- V22-093 remains a separate staged-release milestone: internal accounts, three
  controlled Local SEO consultants, 10–15 paid validation users, observation and the
  expand-or-rollback decision are unchanged.
- No Google production feature flag or Verified Generation release switch was opened by
  this milestone.

V22-091 is fully closed. The deterministic backend suites, real Redis restart proof,
complete V2.1 product retirement, GitHub gate, Railway wait behavior, exact-commit
production deployments, health probe and clean startup logs have all been verified.
