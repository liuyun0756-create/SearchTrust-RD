# V22-091 Backend test infrastructure design

Date: 2026-09-11  
Status: approved on 2026-09-11

## 1. Goal

V22-091 establishes a production-blocking backend quality gate for SearchTrust V2.2
and retires the executable V2.1 product path across both repositories. It preserves the
existing fast pytest coverage, adds strict provider contracts, normalized full-report
golden tests, property-based invariants and real Redis restart integration, and requires
the Railway production services to wait for GitHub Actions.

SearchTrust V2.1 is not a compatibility target. Its backend report implementation and
pipeline, frontend report UI and PDF path, fixtures, compatibility adapters and dedicated
tests will be removed because the product has no users or retained V2.1 support
obligation. The Railway health URL remains `/api/v1/health` to avoid an unnecessary
infrastructure change, but it moves to an independent health router and has no V2.1
product dependency.

## 2. Current state

- The backend currently collects 1,621 pytest tests.
- Fast unit and component-style tests already cover many strict models, evidence and
  findings builders, cost controls, provider adapters, checkpoints and Redis-backed
  state machines.
- Most Redis behavior is exercised with `fakeredis`, which does not prove real Redis
  process interruption or restart behavior.
- Provider fixtures exist in several focused suites, but there is no single suite or
  guard proving that CI cannot contact external services.
- Full normalized V2.2 report output is not locked as an explicit golden acceptance
  boundary.
- Hypothesis is not installed and property-based invariants are not part of the gate.
- The repository has no backend GitHub Actions workflow.
- Railway Web and Worker are linked to the repository and can currently deploy from a
  branch push without waiting for the backend test suite.
- `app/report_v21` and its dedicated fixtures and tests remain in the codebase.
- The frontend still contains `src/lib/report-v21`, `src/components/report/v21`, the old
  report/PDF surfaces and compatibility persistence logic, and its legacy report route
  still calls the backend `/api/v1` analysis endpoints.

## 3. Chosen approach

Implementation follows a layered gate with dependency-guided V2.1 removal:

1. establish the quality commands and outbound-network guard;
2. make existing tests the fast regression baseline;
3. add provider contract, golden report and property suites;
4. add a separate real Redis integration suite;
5. identify and migrate any genuinely shared helpers, then remove the V2.1 backend and
   frontend product paths;
6. rerun the frontend quality gate after its V2.1 deletion;
7. run fast and Redis jobs in backend GitHub Actions;
8. enable Railway Wait for CI for both production services and verify an actual gated
   deployment.

This order provides a working safety net before deletion. A cleanup-first rewrite was
rejected because it would make regressions difficult to distinguish from infrastructure
failures. Leaving V2.1 in place was rejected because it contradicts the product scope
and preserves unnecessary maintenance cost.

## 4. Test layers

### 4.1 Fast unit layer

The existing pytest suite remains the primary fast feedback loop. It continues to use
pure functions, in-memory collaborators and `fakeredis` where Redis protocol fidelity is
not the subject under test. Coverage includes strict models, validation, evidence,
findings, report assembly, cost accounting, checkpoints, task state transitions and API
boundaries.

The implementation will provide one repository-level backend quality command that runs
all required non-integration checks. CI invokes the same command used locally rather
than maintaining a second, divergent command list.

### 4.2 Provider contract layer

Contract tests cover every provider used by a V2.2 execution path, including SerpAPI,
Firecrawl, Dify and first-party Google collectors. Supabase and the frontend callback
are covered as outbound persistence/delivery contracts rather than data providers.

Each contract suite verifies:

- bounded request method, URL, headers, parameters and body;
- strict parsing of the smallest valid response and representative complete response;
- empty, partial, malformed, oversized and unknown-field responses;
- timeout, connection, authentication, rate-limit and provider-failure classification;
- retryability and attempt limits;
- sanitization of provider errors and authorization material;
- deterministic output from identical semantic inputs.

All responses are synthetic or sanitized fixtures checked into the repository. CI must
not contact SerpAPI, Firecrawl, Dify, Google, Supabase, Railway or any other external
origin.

### 4.3 Golden report layer

Golden tests compare complete normalized V2.2 report JSON for Prospect, Verified and
version-change scenarios. Normalization removes only fields that are explicitly
non-semantic and unstable, such as generated timestamps, task identifiers and trace
identifiers. Scores, evidence references, findings, coverage states, action plans,
version differences and public-copy structure remain exact.

Golden fixtures are review artifacts. Tests may regenerate a candidate in a temporary
directory for inspection, but the test command never overwrites the accepted fixture.
An intentional report change requires a normal code review that shows the fixture diff.

### 4.4 Property and idempotency layer

Hypothesis generates bounded synthetic inputs and verifies invariants instead of merely
adding random examples. Initial properties cover:

- identical semantic input produces an identical normalized result;
- input ordering cannot alter canonical ordering or semantic digests;
- duplicate delivery, callback and task submission remain idempotent;
- conflicting replays fail closed;
- counters, attempt limits, report sizes and monetary calculations stay within declared
  bounds;
- NaN, infinity, coercion and undeclared fields are rejected;
- builders, adapters and providers cannot mutate caller-owned input;
- failure and retry sequences cannot double-consume or double-return a credit.

Hypothesis' minimized failures may be retained only when they contain generated test
data. Production examples and secrets are never imported into its example database.

### 4.5 Real Redis integration layer

A separate suite runs against Redis 7.4. Local runs use Docker; GitHub Actions uses a
Redis service container. It never reads `REDIS_URL` or `V22_REDIS_URL` from the user's
normal environment. The suite requires an explicit test-only URL and fails closed if
the resolved target is not loopback in local runs or the declared CI service host in
CI.

The suite uses a unique key prefix and isolated database, and cleans its own keys before
and after a run. It verifies:

- concurrent duplicate submission creates one logical task;
- duplicate physical delivery does not duplicate provider claims or final results;
- lock expiry permits safe takeover without stale-owner writes;
- checkpoints survive Worker termination;
- a replacement Worker resumes from the last valid checkpoint;
- retry counts and terminal states remain bounded;
- cost and credit accounting settle exactly once;
- reconciliation repairs abandoned work without restarting completed stages.

Worker interruption is simulated with controlled test processes. Killing a production
process, contacting Railway Redis or modifying production queue data is outside the
test design.

## 5. V2.1 removal boundary

Before deletion, the implementation records every backend and frontend import, route,
runtime reference and persistence dependency for V2.1. A helper is migrated only if an
active V2.2 path still needs its behavior; such code moves to a version-neutral module
with V2.2-owned tests.

Backend removal includes:

- `app/report_v21` report models, validation, scoring, copy, coverage and fixtures;
- the legacy `app.tasks.pipeline` report-generation path and its V2.1-only collaborators;
- `/api/v1/analyze`, task polling, SSE and deletion routes after their frontend consumers
  are removed;
- tests dedicated to V2.1 report behavior or compatibility;
- transformations and branches used only to read or produce V2.1 reports;
- documentation assertions that V2.1 report output remains supported.

Frontend removal includes:

- `src/lib/report-v21`, `src/components/report/v21` and V2.1 sample-report assets;
- the legacy report generation and test-report pages;
- V2.1 PDF rendering and exportability branches;
- `report_v2_1` normalization, fallback, display and save logic;
- frontend tests and fixtures dedicated to the retired report contract.

The V2.2 acquisition, Case, checkout, report generation, connection, verification,
sharing and PostHog boundaries remain unchanged. The existing frontend quality command
must pass after removal, and an architecture guard rejects new executable references to
`report-v21`, `report_v2_1`, `ReportV21` or the retired report schema.

`/api/v1/health` is retained as an infrastructure compatibility endpoint because
Railway currently uses it. It moves out of the legacy analysis router into a minimal
independent health router. This is the only deliberately retained executable `/api/v1`
path unless a consumer audit proves that another path is required by V2.2 itself.

The architecture guard scans both applications, tests and active fixtures. Historical
design and completion documents are excluded because they are records, not executable
dependencies.

There is no database column removal, historical report backfill or destructive data
operation in V22-091. Retired `report_v2_1` columns may remain unused until V22-092,
where their removal can be handled as a reviewed migration.

## 6. Network and data safety

All test processes install a deny-by-default outbound network guard. Loopback is allowed
only for the test application and test Redis. Any unregistered network attempt raises a
test failure before the socket connects.

Fixtures and failure artifacts must not contain API keys, OAuth tokens, authorization
headers, customer domains, email addresses, share credentials or production database
records. Error assertions use stable internal codes and sanitized public messages rather
than raw third-party bodies.

CI logs are scanned for credential patterns and sensitive URL parameters. The workflow
does not upload Redis dumps, raw provider payloads or unrestricted pytest caches. If a
diagnostic artifact is later required, it must pass the same allowlist and security scan
before upload.

Infrastructure failures are blocking. A missing Redis service, unavailable Docker
runtime, collection error or test timeout fails the corresponding job; the workflow
does not silently skip or downgrade required coverage.

## 7. CI and Railway deployment gate

GitHub Actions runs on pull requests and pushes to `main` with concurrency cancellation
for superseded commits. It contains two required jobs:

1. **Backend quality** installs pinned production and development dependencies, checks
   collection, runs the fast/unit, provider contract, golden and property suites, runs
   the architecture/network/security guards and verifies that generated contracts have
   no diff.
2. **Redis restart integration** starts Redis 7.4 as a service, waits for a health check,
   then runs only the real Redis queue/restart suite with bounded timeouts.

The frontend repository keeps its existing V22-090 workflow. Its full quality command
must pass on the commit that removes the V2.1 client path before the backend drops the
legacy analysis endpoints.

Both jobs are required checks. Railway remains connected to the repository, but Wait for
CI is enabled on both the Web and Worker services. Railway therefore creates no active
production rollout until the GitHub workflow for that commit succeeds. This uses
Railway's native Git revision and deployment status instead of adding a broad account
token to GitHub.

Reference: [Railway — Controlling GitHub Autodeploys](https://docs.railway.com/deployments/github-autodeploys).

After the first gated `main` run succeeds, deployment verification records:

- the Git commit and GitHub Actions run;
- both required job conclusions and test totals;
- Web and Worker deployment identifiers and commit revisions;
- Web health response;
- absence of new error-level logs during the observation window;
- proof that Railway remained waiting while CI was running.

A failed post-deployment health check marks the milestone unsuccessful and preserves
diagnostic evidence. V22-091 does not perform automatic destructive rollback or database
mutation.

## 8. Commands and suite ownership

The implementation plan will define exact command names after auditing current test
durations and fixture generation scripts. The resulting interface must provide:

- one fast backend quality command used locally and by the quality job;
- one explicit real Redis integration command;
- one full command that runs both for release verification;
- deterministic fixture consistency checks that never rewrite accepted files;
- pytest markers that prevent real Redis tests from entering the fast suite implicitly.

Test helpers remain close to their owning layer. Shared fixture loaders, network guards
and normalization functions may live under a focused `tests/support` package; provider-
specific fixtures remain grouped by provider, and golden reports remain grouped by
public report type.

## 9. Acceptance criteria

V22-091 is complete only when:

1. the pre-change backend regression baseline remains green after intentional V2.1 test
   removal;
2. every active V2.2 provider has strict request, response and failure contracts;
3. Prospect, Verified and version-change outputs have normalized full-report goldens;
4. property tests cover determinism, canonical ordering, idempotency, strict bounds,
   invalid numeric values and input immutability;
5. real Redis proves duplicate delivery, lock expiry, Worker interruption and restart
   recovery;
6. V2.1 backend and frontend product code, dedicated fixtures, tests and executable
   imports are gone;
7. `/api/v1/health` remains operational from an independent router and no retired
   analysis route remains;
8. the frontend quality gate passes after the legacy UI, PDF and persistence paths are
   removed;
9. unexpected external requests fail closed;
10. local fast, integration and full backend release commands pass;
11. both backend GitHub Actions jobs pass on the final `main` commit;
12. Railway Web and Worker both wait for CI and deploy the successful commit only after
    the required jobs finish;
13. production Web health and initial error-log checks pass;
14. both worktrees are clean and the completion report records reproducible evidence.

## 10. Scope boundary

V22-091 does not add product behavior, enable Google sync or Verified Generation, call
live providers, change commercial limits, perform schema migrations, backfill or delete
database records, or test against production Redis. Unused V2.1 database columns remain
until the reviewed V22-092 migration. Controlled rollout remains V22-093.
