# V22-082 Cost control implementation plan

Status: completed, verified and deployed to production on 2026-09-10.

Design source: `2026-09-10-searchtrust-v2-2-cost-control-design.md`.

This milestone adds internal provider usage accounting and bounded per-job request
claims across `SearchTrust-RD` and `search-trust`. It does not expose cost data to
users, impose a total dollar budget, reduce purchased report depth, enable Jina or
PageSpeed, or change the current Google/Verified Generation feature flags.

## 1. Freeze cost, pricing and limit contracts

Backend files:

- add `app/jobs_v22/cost_models.py`;
- update `app/core/config.py` and `.env.example`;
- add `tests/test_v22_cost_models.py` and extend
  `tests/test_v22_deployment_config.py`.

Work:

- define the fixed provider and operation catalog for SerpAPI market search,
  SerpAPI public profile/reviews, Firecrawl Map, Jina, PageSpeed, GSC, GA4, GBP and
  Dify;
- define bounded claim, completion, usage and flat `CostCountersV1` models;
- allow only finite non-negative numeric callback counters and reject unbounded or
  unknown dynamic keys;
- define the approved request ceilings: SerpAPI 30 plus one location lookup for
  discovery, SerpAPI public collection 15, Firecrawl one per site and four per
  report, PageSpeed five when enabled, GSC 12, GA4 28, GBP 12 and Dify three;
- keep Jina and PageSpeed actual usage at zero while their V2.2 main-pipeline
  integrations remain disabled;
- add integer-microdollar price settings, separate Dify input/output rates and a
  numeric pricing revision; represent missing price data explicitly rather than as
  a known zero cost.

Verification:

- model tests cover negative, non-finite, excessive and unknown values;
- pricing tests prove deterministic integer rounding and immutable revision use;
- configuration tests prove every new environment setting is documented and
  server-only.

Commit boundary: cost contracts and configuration only.

## 2. Build the atomic Redis job cost ledger

Backend files:

- add `app/jobs_v22/cost_ledger.py`;
- update `app/jobs_v22/keys.py` and `app/competitors_v22/keys.py` only where a
  common key helper is required;
- add `tests/test_v22_cost_ledger.py`.

Work:

- implement `claim`, idempotent `complete`, local-denial, checkpoint-hit and
  snapshot operations;
- use one Redis-backed ledger per logical job ID, independent of `run_generation`,
  with the same TTL as its durable job state;
- make claim-and-limit enforcement atomic under concurrency;
- retain a claimed slot after process loss and summarize it as outcome unknown when
  no valid completion exists;
- make duplicate completion harmless and reject a completion whose provider,
  operation or claim identity does not match;
- derive aggregate attempts, successes, failures, unknown outcomes, duration,
  billable units, estimated microdollars and missing-price units without scanning
  customer payloads;
- increment a monotonic ledger revision on every accepted claim or completion;
- fail closed before an external request when the Redis claim cannot be persisted.

Verification:

- concurrent claims never exceed the configured operation ceiling;
- three worker generations share the same count;
- duplicate completions do not duplicate time, tokens, units or estimated cost;
- expired ledgers follow the job TTL and cannot be silently recreated for an active
  existing job.

Commit boundary: Redis ledger and focused tests only.

## 3. Add service-role-only durable cost summaries

Frontend/database files:

- add
  `search-trust/supabase/migrations/20260910100000_add_v2_2_cost_control.sql`;
- update `search-trust/src/types/database.ts`;
- extend `search-trust/supabase/tests/database/v22_schema.test.sql` and
  `search-trust/src/lib/database/v22Migration.test.ts`.

Backend files:

- add `app/jobs_v22/cost_persistence.py`;
- add `tests/test_v22_cost_persistence.py`.

Work:

- create `job_cost_summaries` keyed by job ID with Case, job kind, terminal status,
  attempt count, monotonic ledger revision, flat cost counters and bounded timestamps;
- restrict job kinds to competitor discovery, prospect report, verified report,
  GSC sync, GA4 sync and GBP sync;
- enable RLS, revoke `anon` and `authenticated`, grant only `service_role`, and
  provide no browser-callable RPC;
- add terminal-safe idempotent upsert that cannot change Case or job kind and cannot
  replace a higher ledger revision with an older one;
- add `cost_counters` to `google_sync_jobs` and extend the three finish/fail RPC
  families to save the same terminal summary in their own job row;
- keep `analysis_jobs.cost_counters` unchanged and compatible with the existing
  signed callback;
- implement a bounded service-role backend persister for discovery and terminal job
  summaries without logging request bodies or database response text;
- add a Redis pending-summary set and an independent bounded reconciler so a temporary
  database outage retries only the summary write and never reruns provider or report
  work.

Verification:

- database tests cover valid upsert, replay, cross-Case substitution, job-kind
  substitution and terminal immutability;
- `anon` and `authenticated` cannot select, insert, update or delete either direct
  cost summaries or protected job cost fields;
- service-role persistence retries do not duplicate summaries, and an older revision
  cannot overwrite a newer revision.

Commit boundary: additive database contract and persistence adapter.

## 4. Instrument SerpAPI and competitor discovery

Backend files:

- update `app/integrations/serpapi.py`;
- update `app/jobs_v22/serp_market_stage.py`;
- update `app/competitors_v22/discovery_service.py`;
- update `app/competitors_v22/models.py`, `store.py` and `worker.py`;
- update `app/competitors_v22/public_profile_stage.py` and
  `collection_stage.py`;
- extend the existing SerpAPI, competitor discovery, public-profile and collection
  tests.

Work:

- compose the current per-key `before_attempt` hook with the shared ledger claim so
  every real key attempt is counted exactly once;
- distinguish location lookup, market search, place detail and review page
  operations while keeping secrets, queries and resource identifiers out of keys;
- preserve the current local limits and make the common ledger the outer ceiling,
  never a way to loosen a smaller operation budget;
- add `cost_counters` to internal competitor discovery state without adding it to
  the public discovery response;
- snapshot counters on retry and terminal transitions, and persist a terminal
  `competitor_discovery` summary even when checkout never follows;
- when a report later uses a confirmed discovery, merge a prefixed linked-discovery
  snapshot into the report's end-to-end cost totals without deleting or mutating the
  standalone discovery summary;
- count circuit-open rejection as a local denial only when no provider request was
  sent.

Verification:

- key rotation, rate limiting, timeout, 4xx, 5xx and malformed response paths record
  the correct attempt outcome;
- cached market snapshots and request checkpoints add checkpoint hits but no provider
  attempts;
- discovery retry shares the original 30-attempt ceiling;
- a discovery abandoned before payment still receives a terminal durable summary.

Commit boundary: SerpAPI and discovery accounting.

## 5. Instrument site collection, Firecrawl and controlled copy

Backend files:

- update `app/jobs_v22/site_inventory_stage.py`;
- update `app/collectors/site_inventory_firecrawl.py`;
- update `app/jobs_v22/copy_provider.py` and
  `prospect_report_pipeline.py`;
- update `app/jobs_v22/executor.py` and `worker.py`;
- extend site-inventory, Firecrawl, copy-provider, report-pipeline, executor and
  worker tests.

Work:

- claim one Firecrawl Map attempt per site namespace and enforce no more than four
  for a report with three competitors;
- preserve direct safe website fetching as non-provider network telemetry; do not
  mislabel it as Jina or Firecrawl cost;
- emit explicit zero Jina and PageSpeed usage while those modules are absent from the
  V2.2 generation path;
- claim Dify before each workflow request and complete it for success, safe 4xx,
  429, 5xx, timeout, transport and invalid-response outcomes;
- parse only bounded numeric Dify usage fields, record input/output/total tokens when
  present and increment usage-unknown calls otherwise;
- keep the controlled-copy checkpoint ahead of provider execution so a successful
  result is not called again after worker recovery;
- update report worker transitions with live snapshots on running, automatic retry,
  success and final failure;
- compute wall time from logical job creation and active time from attempt intervals;
- send the terminal analysis snapshot through the existing signed callback and the
  internal terminal summary persister.

Verification:

- Firecrawl failure remains an existing evidence-availability outcome when the report
  contract can still be completed;
- Dify cannot exceed three real workflow attempts across automatic retries;
- provider usage is not exposed in `ReportV22`, public task status, share output or
  PDF contracts;
- hard-limit exhaustion that prevents core report completion reaches the existing
  failed terminal path and returns exactly one credit.

Commit boundary: report-generation provider accounting and financial regression.

## 6. Instrument GSC, GA4 and GBP synchronization

Backend files:

- update `app/google_connections_v22/gsc.py`, `ga4.py` and `gbp.py`;
- update `app/google_connections_v22/sync_io.py`;
- update `app/google_connections_v22/sync_worker.py`,
  `ga4_sync_worker.py` and `gbp_sync_worker.py`;
- extend `tests/test_v22_gsc_sync.py`, `test_v22_ga4_sync.py` and
  `test_v22_gbp_sync.py`.

Work:

- inject an explicit Google-provider request hook around only Google GSC/GA4/GBP
  calls; exclude the internal token broker and Supabase persistence calls from
  provider cost totals;
- count GSC's twelve bounded calls, GA4's up-to-twenty configuration pages plus
  eight reports, and GBP's profile, performance and up-to-ten keyword pages;
- share the same ledger across database-controlled sync retry attempts;
- pass live cost counters to the updated finish/fail RPCs, and upsert the matching
  `job_cost_summaries` row only when the database job reaches a terminal state;
- keep Google access tokens and raw GBP Content out of cost state and persistence.

Verification:

- success, retryable provider failure, permanent access failure, timeout and lease
  recovery retain correct counts;
- pagination stops at the existing ceilings even under malformed continuation-token
  behavior;
- current production-disabled Google flags remain disabled after the code change.

Commit boundary: Google sync accounting.

## 7. Lock public contracts and privacy boundaries

Frontend files:

- extend `search-trust/src/lib/jobs-v22/callback-contract.ts` tests without changing
  the numeric callback shape;
- extend `search-trust/src/lib/jobs-v22/handlers.test.ts` and
  `repository.test.ts`;
- add focused report/share serialization assertions in the closest existing tests.

Backend files:

- extend `tests/test_v22_job_callbacks.py`, `test_v22_job_models.py` and API/report
  contract tests;
- extend the V22-081 sensitive-output scan fixtures if new cost error codes or
  logging paths require coverage.

Work:

- keep callbacks flat and numeric, rejecting strings, nested objects, NaN and
  infinity;
- ensure `TaskStatusResponse`, competitor status, `ReportV22`, customer-share and PDF
  models contain no cost, price, quota, billing-unit or provider-balance fields;
- ensure application logs use only fixed provider/operation names, safe error codes,
  bounded counts and job ID suffixes;
- scan for API keys, OAuth values, queries, URLs, email, phone, provider bodies and
  Dify inputs in new Redis, database and log serialization paths.

Verification:

- public-contract snapshots remain unchanged except internal callbacks carrying
  numeric counters;
- browser bundles contain no provider pricing configuration or internal cost table
  access path.

Commit boundary: contract and privacy gates.

## 8. Full verification and release

Local gates:

1. run focused cost-model, ledger, persistence, provider and worker tests;
2. run the complete backend test suite;
3. run frontend unit tests, type checking, contract checks and production build;
4. run Supabase migration and pgTAP/database integration tests;
5. run V22-081 sensitive-output scans against test output and frontend artifacts;
6. verify both worktrees are clean except the intended V22-082 commits.

Production order:

1. apply `20260910100000_add_v2_2_cost_control.sql` to production Supabase;
2. verify new RLS/grants and both cost JSON shapes through service-role and
   anon/authenticated probes;
3. deploy Railway API, then Railway Worker;
4. deploy Vercel only if database types or server callback handling changed;
5. configure `pricing_revision` and known unit prices; leave genuinely unknown rates
   explicit rather than inventing values;
6. verify API health, queue/Redis health, Worker heartbeat and callback backlog;
7. run synthetic, non-customer success/failure/checkpoint/retry accounting drills
   without enabling disabled Google or Verified features;
8. prove failure compensation remains exactly one credit and remove every synthetic
   record;
9. record deployment IDs, test totals, cost-counter examples with no customer data,
   cleanup evidence and unchanged feature flags in the V22-082 completion document;
10. update the main V2.2 development plan with the approved replacement for the old
    dollar-budget truncation rule.

## Completion gate

V22-082 is complete only when every report, discovery and Google sync logical job has
one bounded shared ledger; real provider attempts and Dify usage are conservatively
counted across concurrency, recovery and retry; checkpoint hits never duplicate cost;
terminal summaries are stored behind service-role-only database boundaries; missing
prices remain explicit; no total-dollar budget or cost-driven report reduction is
introduced; public report, task, share and PDF contracts expose no cost data; core
hard-limit failure still returns exactly one credit; full local and production gates
pass; synthetic data is removed; and existing Google/Verified feature flags are
unchanged.
