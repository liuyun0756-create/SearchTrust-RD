# V22-091 Backend test infrastructure implementation plan

Status: ready for implementation review

Design source:
`2026-09-11-searchtrust-v2-2-backend-test-infrastructure-design.md`.

This plan builds the SearchTrust V2.2 backend release gate, proves restart safety with
real Redis, and retires the executable V2.1 product path across the backend and frontend.
All provider tests are synthetic. No task may contact production Redis, Supabase,
Railway data services, Google, Dify, Firecrawl, SerpAPI or payment providers.

## Baseline and repositories

- Backend repository: `/Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD`
- Frontend repository: `/Users/liuyun3/Documents/SearchTurst前后端/search-trust`
- Backend baseline on 2026-09-11: 1,621 tests passed in 27.42 seconds.
- Frontend release gate: V22-090, already active on `main`.
- Railway project: `3e69fdd3-3241-412c-b224-6bf468bc5b15`.
- Production Web service: `SearchTrust-RD`
  (`f3a526ab-b156-4223-a0ef-c7aa886b3b4f`).
- Production Worker service: `SearchTrust-v2-2-Worker-Production`
  (`fd8d13e2-3546-4bf2-a02a-5f018a0550a8`).

Implementation uses small, independently verified commits. Backend code commits remain
local until Railway Wait for CI is active. Frontend retirement is pushed first because
its existing Vercel gate is already operational; the backend legacy endpoints are
removed only after their frontend consumers are gone.

## Task 1: Bootstrap the backend CI workflow

Files:

- Create: `.github/workflows/backend-quality.yml`
- Modify: `requirements-dev.txt`
- Modify: `pytest.ini`
- Create: `tests/test_v22_backend_ci_contract.py`

Steps:

1. Add a failing workflow-contract test that parses the workflow and requires:
   pull-request and `main` push triggers, concurrency cancellation, Python 3.12, pinned
   Redis 7.4 service, pip caching, a backend quality job and a Redis integration job.
2. Add `hypothesis`, `pytest-socket` and `pytest-timeout` to development requirements,
   pinned to versions verified with Python 3.12. Production requirements remain
   unchanged unless an application import proves otherwise.
3. Register `redis_integration` and `contract` pytest markers. Markers are strict so a
   misspelled or unknown marker fails collection.
4. Create the initial workflow. The quality job installs `requirements-dev.txt` and runs
   the full 1,621-test baseline with sockets disabled. The Redis job starts
   `redis:7.4-alpine`, proves it is healthy, and initially runs a minimal Redis protocol
   smoke assertion.
5. Ensure the workflow never reads production environment files or repository secrets.
6. Run:

   ```bash
   .venv/bin/python -m pytest tests/test_v22_backend_ci_contract.py -q
   .venv/bin/python -m pytest -q
   ```

7. Commit as `ci(v2.2): bootstrap backend quality gate`.
8. Push only the approved design/plan and this bootstrap workflow. Verify the initial
   GitHub Actions run passes. This one controlled push may use Railway's current direct
   Git deployment because it contains no backend behavior change.

## Task 2: Enable Railway Wait for CI before backend changes

External configuration:

- Railway production environment
- `SearchTrust-RD`
- `SearchTrust-v2-2-Worker-Production`

Steps:

1. Confirm the backend workflow is visible to Railway and contains a `push` trigger for
   `main`.
2. In each production service's source/deploy settings, enable **Wait for CI** while
   leaving the GitHub repository and `main` branch connection intact.
3. Read the settings again and verify both services show Wait for CI enabled.
4. Record the most recent deployment IDs and commit before any backend implementation
   push. This becomes the negative control for proving no early deployment occurs.
5. Do not create a Railway account token or GitHub deployment secret. The design uses
   Railway's native check-suite integration.

Completion gate: no backend behavior commit may be pushed until both production service
settings have been verified.

## Task 3: Add reproducible quality commands and fail-closed network controls

Files:

- Create: `scripts/run_v22_backend_quality.py`
- Create: `scripts/run_v22_redis_integration.py`
- Create: `tests/support/__init__.py`
- Create: `tests/support/network_guard.py`
- Create: `tests/support/output_security.py`
- Create: `tests/conftest.py`
- Create: `tests/test_v22_backend_quality_runner.py`
- Create: `tests/test_v22_network_guard.py`
- Create: `tests/test_v22_test_output_security.py`
- Modify: `.gitignore`
- Modify: `.github/workflows/backend-quality.yml`

Steps:

1. Write failing tests for three stable command modes:
   `python scripts/run_v22_backend_quality.py fast`,
   `python scripts/run_v22_redis_integration.py`, and
   `python scripts/run_v22_backend_quality.py release`.
2. Implement `fast` as the non-Redis suite plus fixture/architecture consistency checks.
   Implement `release` as fast tests followed by the real Redis runner.
3. Use `pytest-socket` to disable sockets by default. Fast tests have no host allowlist.
   The Redis runner permits only its explicit test host and rejects production-like
   Redis hosts, Railway domains and non-test environment variables.
4. Make the local Redis runner use a dedicated Docker Compose project, an ephemeral
   Redis 7.4 container, a non-production database and a unique key prefix. It must stop
   and remove only its own test container on normal exit or interruption.
5. Capture subprocess diagnostics to a temporary directory, scan them for authorization
   headers, OAuth parameters, API-key patterns, share credentials and configured secret
   values, then print only safe or redacted output. Do not retain successful-run output.
6. Add deliberate negative controls proving an external socket attempt fails before
   connection and a planted secret is reported only as `[REDACTED]`.
7. Update the workflow to use the repository entrypoint instead of duplicating commands.
8. Run the fast command twice and confirm identical collection and outcome.
9. Commit as `test(v2.2): establish backend quality boundaries`.

## Task 4: Consolidate provider contract coverage

Files:

- Create: `tests/provider_contracts/__init__.py`
- Create: `tests/provider_contracts/conftest.py`
- Create: `tests/provider_contracts/test_contract_manifest.py`
- Create: `tests/provider_contracts/test_serpapi_contract.py`
- Create: `tests/provider_contracts/test_firecrawl_contract.py`
- Create: `tests/provider_contracts/test_dify_contract.py`
- Create: `tests/provider_contracts/test_google_contract.py`
- Create: `tests/provider_contracts/test_delivery_contracts.py`
- Create: `tests/fixtures/provider_contracts/manifest.json`
- Create or relocate sanitized fixtures under:
  `tests/fixtures/provider_contracts/{serpapi,firecrawl,dify,google,delivery}/`
- Modify active adapters only where a failing contract exposes a missing validation seam.

Steps:

1. Build a manifest listing every active provider operation, owning adapter, request
   contract, fixture set, error mapping and maximum response size.
2. Make the manifest test fail while any active operation lacks a declared contract.
3. Reuse existing sanitized fixtures where possible; copy only the minimum response
   shape needed by the consolidated suite. No captured headers or raw customer payloads
   enter the repository.
4. Test request method, URL path, bounded parameters/body and absence of unexpected
   authorization propagation.
5. Test smallest valid, complete, empty, partial, malformed, oversized and unknown-field
   responses.
6. Test timeout, connection, 401/403, 429 and 5xx classification, including exact retry
   and attempt-limit behavior.
7. Assert all public errors contain stable internal codes and no provider response body,
   URL query secret or credential value.
8. Run the contract marker in isolation and then the fast quality command.
9. Commit as `test(v2.2): lock provider contracts`.

## Task 5: Add normalized full-report golden tests

Files:

- Create: `tests/support/golden_reports.py`
- Create: `tests/test_v22_report_goldens.py`
- Create: `scripts/check_v22_report_goldens.py`
- Create: `tests/fixtures/report_v22_golden/prospect.json`
- Create: `tests/fixtures/report_v22_golden/verified.json`
- Create: `tests/fixtures/report_v22_golden/version_change.json`
- Modify, only if required: existing V2.2 fixture builders and report assemblers.

Steps:

1. Construct Prospect, Verified and changed-only version scenarios from existing
   synthetic V2.2 builders with fixed clocks and UUIDs.
2. Define an explicit normalization allowlist for unavoidable non-semantic runtime
   values. Any new dynamic path fails the test rather than being silently removed.
3. Compare the complete canonical JSON, including report version, identity, scores,
   evidence, coverage, findings, top actions, verified performance and version diff.
4. Add semantic assertions that prevent a golden update from approving internally
   inconsistent evidence IDs, finding references, action order or parent-report binding.
5. Make the checker write proposed output only to a temporary candidate directory by
   default. Updating an accepted fixture requires an explicit `--accept` action and is
   never called by CI.
6. Run the checker twice and compare hashes to prove deterministic bytes.
7. Run the three golden tests and the complete fast suite.
8. Commit as `test(v2.2): lock full report goldens`.

## Task 6: Add Hypothesis invariants

Files:

- Create: `tests/property/__init__.py`
- Create: `tests/property/strategies.py`
- Create: `tests/property/test_digest_properties.py`
- Create: `tests/property/test_model_properties.py`
- Create: `tests/property/test_report_properties.py`
- Create: `tests/property/test_idempotency_properties.py`
- Create: `tests/property/test_cost_properties.py`
- Modify: `tests/conftest.py`

Steps:

1. Register local and CI Hypothesis profiles with explicit example counts, deterministic
   generation for CI, no production example database and bounded per-test deadlines.
2. Generate only syntactically valid synthetic domains, URLs, identifiers, counters,
   monetary values, evidence graphs and provider-result sequences.
3. Prove canonical digests and report output are invariant to semantically irrelevant
   mapping and input ordering.
4. Prove strict models reject NaN, infinity, undeclared fields, unsafe coercion and
   values above declared limits.
5. Prove builders and provider boundaries never mutate caller-owned objects.
6. Generate duplicate and conflicting task/callback sequences and verify exactly-once
   logical outcomes.
7. Generate provider success/failure/unknown sequences and prove cost counters and
   credit compensation settle once without underflow or duplicate refund.
8. Run each property file separately, then run the fast command twice.
9. Commit as `test(v2.2): add backend invariants`.

## Task 7: Build the real Redis integration harness

Files:

- Create: `docker-compose.test.yml`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/support/__init__.py`
- Create: `tests/integration/support/redis_process.py`
- Create: `tests/integration/test_redis_environment_guard.py`
- Create: `tests/integration/test_redis_store_atomicity.py`
- Modify: `scripts/run_v22_redis_integration.py`
- Modify: `.github/workflows/backend-quality.yml`

Steps:

1. Pin an ephemeral Redis 7.4 image with no production volume and a dedicated health
   check. Local Docker binds only loopback; CI uses the workflow service hostname.
2. Require `V22_TEST_REDIS_URL` and a generated `V22_TEST_REDIS_PREFIX`. Fail if the URL
   matches any normal application Redis variable or resolves outside the allowed test
   boundary.
3. Flush only keys carrying the generated prefix. Never issue an unscoped `FLUSHALL` or
   operate on the user's normal Redis database.
4. Verify real Redis transactions, watch conflicts, TTLs, sorted sets, pub/sub and byte
   decoding used by `DurableJobStore`.
5. Add concurrent registration and transition tests that prove one idempotent identity,
   monotonic revision and fencing of stale generations.
6. Make the CI Redis job invoke this suite, not a smoke-only placeholder.
7. Run the Redis suite three consecutive times to prove isolated cleanup.
8. Commit as `test(v2.2): add real Redis integration harness`.

## Task 8: Prove Worker interruption and restart recovery

Files:

- Create: `tests/integration/support/worker_probe.py`
- Create: `tests/integration/test_worker_restart_recovery.py`
- Create: `tests/integration/test_duplicate_delivery.py`
- Create: `tests/integration/test_reconciliation_recovery.py`
- Create: `tests/integration/test_restart_cost_settlement.py`
- Modify only if tests expose a defect:
  `app/jobs_v22/store.py`, `app/jobs_v22/reconciler.py`,
  `app/jobs_v22/worker.py`, `app/jobs_v22/cost_ledger.py`,
  `app/jobs_v22/callbacks.py`.

Steps:

1. Implement a test-only ARQ probe job that records a real checkpoint, signals the test
   process and then blocks at a controlled interruption point.
2. Start the Worker as a child process against test Redis, wait for the checkpoint, stop
   that process and confirm Redis retains the logical task and checkpoint.
3. Advance the test clock beyond the stale lease, run reconciliation, start a replacement
   Worker and verify it resumes with a higher generation.
4. Attempt a stale-owner write and assert `JobLeaseLost` without any state mutation.
5. Deliver the physical job and callback more than once and prove one terminal report,
   one callback revision and one cost/credit settlement.
6. Verify deadline and retry exhaustion terminate safely and do not loop after restart.
7. Ensure child processes are bounded by timeouts and always reaped; a leaked process or
   key fails teardown.
8. Run the integration command three times, then the full release command.
9. Commit as `test(v2.2): prove queue restart recovery`.

## Task 9: Retire the frontend V2.1 product path

Primary files and directories:

- Delete: `src/lib/report-v21/`
- Delete: `src/components/report/v21/`
- Delete: `src/components/report/sampleReportV21.ts`
- Delete: `src/components/report/sampleReportV21Data.json`
- Delete: `src/app/test-report/`
- Modify or remove: `src/app/reports/page.tsx`
- Modify: `src/components/report/ReportContent.tsx`
- Modify: `src/components/report/pdf/ReportPDFDocument.tsx`
- Modify: `src/app/api/reports/[id]/pdf/route.ts`
- Modify: `src/app/api/report-status/route.ts`
- Modify or remove: `src/app/api/generate-report/route.ts`
- Modify: `src/app/api/report-meta/route.ts`
- Modify: `src/types/database.ts` only if generated typing permits; the unused database
  column declaration may remain until V22-092.
- Add: `src/lib/ci-v22/no-v21-runtime.test.ts`
- Update/delete all directly owned V2.1 tests and fixtures discovered by the guard.

Steps:

1. Add a failing architecture guard for executable references to `report-v21`,
   `report_v2_1`, `ReportV21`, the legacy report routes and sample assets. Narrowly
   exclude migration history, generated database column declarations and documentation.
2. Map each old route and component to its current navigation consumer. Remove dead V2.1
   navigation rather than redirecting users to a compatibility renderer.
3. Remove V2.1 display, normalization, fallback, persistence and PDF branches. Preserve
   the V2.2 Case, Prospect, Verified, sharing and PDF behavior already covered by V22-090.
4. Remove frontend proxy calls to the legacy backend analyze/task endpoints.
5. Delete V2.1-only tests and fixtures; do not reduce V2.2 browser journeys or critical
   component coverage.
6. Run `npm run quality`, including all 11 browser journeys and the security scan.
7. Commit as `refactor(v2.2): retire legacy report frontend` and push through the existing
   Vercel quality gate.
8. Verify the Vercel deployment is Ready, `trysearchtrust.com` returns HTTP 200 and no
   runtime error appears during the observation window.

Completion gate: do not remove the backend legacy endpoints until this frontend commit
has passed CI and reached production.

## Task 10: Retire the backend V2.1 product path

Primary files and directories:

- Delete: `app/report_v21/`
- Delete after dependency audit: `app/tasks/pipeline.py`
- Delete if no V2.2 import remains: `app/tasks/address_ai.py`,
  `app/tasks/dify_client.py` and V2.1-only task helpers.
- Create: `app/api/health.py`
- Modify: `app/main.py`
- Delete or reduce: `app/api/v1/analyze.py`
- Create as required: focused version-neutral helpers under `app/report_common/`
- Modify V2.2 consumers including `app/preflight_v22/gbp.py`,
  `app/preflight_v22/extractors.py`, `app/tasks/scraper.py` and
  `app/report_v22/evidence_adapters/site.py`.
- Create: `tests/test_v22_no_v21_runtime.py`
- Create: `tests/test_health_api.py`
- Delete/update all V2.1-owned tests and fixtures identified by the dependency audit.
- Modify: `README.md`, `DEPLOY.md`, `Dockerfile`, `railway.toml` only where retired
  behavior is described or the health router changes ownership.

Steps:

1. Add a failing architecture guard for executable `report_v21`, `ReportV21`,
   `report_v2_1` and legacy analysis-pipeline references. Exclude historical documents,
   database migration history and generated schema declarations.
2. Move `/api/v1/health` into a minimal independent router and prove its exact existing
   response contract before altering the legacy router.
3. Inventory shared imports. Move only helpers required by active V2.2 paths into small
   version-neutral modules with their existing tests updated first.
4. Replace `app.report_v21.evidence_ledger` use in the V2.2 site evidence adapter with a
   V2.2-owned or version-neutral implementation.
5. Remove legacy analyze submission, in-memory queue, task polling, SSE and deletion
   endpoints after the frontend consumer gate from Task 9 is green in production.
6. Delete the V2.1 report package, pipeline, dedicated fixtures and tests.
7. Run collection to ensure deletion introduced no hidden imports. Compare removed test
   counts with the explicit deletion manifest; unexpected collection loss fails review.
8. Run the fast, Redis and full release commands. Confirm `/api/v1/health` still passes
   through FastAPI's test client and a local production-style server.
9. Commit as `refactor(v2.2): retire legacy report backend`.

## Task 11: Complete the final CI and Railway production proof

Files:

- Modify: `.github/workflows/backend-quality.yml`
- Modify: `tests/test_v22_backend_ci_contract.py`
- Modify: `docs/superpowers/specs/2026-08-26-searchtrust-v2-2-development-plan.md`
- Create:
  `docs/superpowers/specs/2026-09-11-searchtrust-v2-2-backend-test-infrastructure-completion.md`

Steps:

1. Ensure the final workflow uses the stable quality entrypoints, strict timeouts,
   Redis 7.4 health checks, least-privilege default permissions and no production
   secrets.
2. Run locally:

   ```bash
   .venv/bin/python scripts/run_v22_backend_quality.py fast
   .venv/bin/python scripts/run_v22_redis_integration.py
   .venv/bin/python scripts/run_v22_backend_quality.py release
   ```

3. Confirm both repositories are clean except for the planned commits and all generated
   fixture checks report no diff.
4. Read Railway deployment state, then push the backend commits to `main`.
5. While GitHub Actions is running, verify both production services do not start a new
   active deployment for the pushed commit.
6. Verify Backend quality and Redis restart integration both succeed. Record test totals,
   durations and the GitHub Actions run URL.
7. Verify Railway then deploys the same commit to both `SearchTrust-RD` and
   `SearchTrust-v2-2-Worker-Production`.
8. Confirm the Web deployment returns HTTP 200 from `/api/v1/health`, both deployments
   reach `SUCCESS`, and initial Web/Worker logs contain no error-level event.
9. Update the completion report with the frontend V2.1 retirement run, backend run,
   deployment IDs, health result, removed-code manifest and unchanged V22-092/V22-093
   boundaries.
10. Mark V22-091 completed in the main development plan, commit as
    `docs(v2.2): complete backend test infrastructure`, push, and verify the documentation
    commit itself passes the same Railway gate.

## Final acceptance gate

Do not mark V22-091 complete unless all of the following are simultaneously true:

- provider contract, golden, property and real Redis restart suites pass;
- unexpected external requests fail closed;
- all executable V2.1 frontend/backend product paths are gone;
- `/api/v1/health` remains healthy and independent;
- frontend and backend quality gates are green;
- Railway waits for the backend check suite and both production services deploy the
  exact successful commit;
- no production provider, payment, database or Redis data was used by tests;
- the completion report contains reproducible online evidence;
- both worktrees are clean and their local heads equal `origin/main`.
