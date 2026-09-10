# V22-090 Frontend test infrastructure implementation plan

Design source: `2026-09-10-searchtrust-v2-2-frontend-test-infrastructure-design.md`.

This plan adds a production-blocking frontend quality gate without contacting real
payment, identity, database, backend, analytics or provider services from browser tests.

## Task 1: Install and configure the test layers

Frontend files:

- modify `package.json` and `package-lock.json`;
- add `vitest.config.ts`;
- add `src/test/setup.ts`;
- add `playwright.config.ts`;
- add `e2e/fixtures/ids.ts`;
- add `e2e/support/external-request-guard.ts`.

Implementation:

1. add React Testing Library, DOM matchers, user-event, jsdom and Playwright as locked
   development dependencies;
2. keep Vitest's default environment as Node and opt interaction test files into jsdom;
3. add scripts for unit tests, component interaction tests, Playwright, CI browser tests
   and the complete local quality gate;
4. configure one Chromium project, deterministic locale/timezone, local Next.js
   `webServer`, one infrastructure retry in CI, trace/screenshot only on final failure
   and bounded output directories;
5. define reserved synthetic UUIDs and `.invalid` domains shared by browser fixtures;
6. abort and record every browser request whose origin is not the local application or
   an explicitly safe browser-internal scheme.

Tests first:

- prove the external-request guard allows localhost and blocks Supabase, Railway, Dodo,
  Google, PostHog and arbitrary Internet origins;
- prove fixture identifiers are deterministic and visibly synthetic.

Verification:

- `npm run typecheck`;
- focused Vitest tests for the new support modules;
- `npx playwright test --list`.

Commit: `test(v2.2): establish frontend browser test layers`.

## Task 2: Add a production-safe E2E identity seam

Frontend files:

- add `src/lib/e2e-v22/config.ts` and tests;
- add `src/lib/client-auth.tsx` and tests;
- modify `src/app/layout.tsx`;
- modify `src/lib/use-authenticated-fetch.ts`;
- modify the existing components that import Clerk client hooks directly;
- modify `src/lib/auth.ts` only through the shared server-side E2E guard.

Implementation:

1. centralize browser identity behind app-owned `useAppUser` and `useAppAuth` hooks;
2. use Clerk unchanged outside E2E mode;
3. in local E2E mode, expose one fixed signed-in synthetic identity and a non-secret
   fixed test bearer accepted only by intercepted local requests;
4. allow the server identity resolver to return the matching synthetic internal user
   only for reserved E2E IDs;
5. refuse E2E mode whenever `VERCEL_ENV` is present or the configured base URL is not
   loopback;
6. ensure analytics stays disabled in E2E mode and share-page analytics remains disabled
   independently of the test flag.

Tests first:

- normal mode delegates to Clerk;
- local E2E mode returns the fixed synthetic identity;
- production or non-loopback E2E configuration fails closed;
- authenticated fetch still rejects cross-origin destinations;
- no real token or credential is introduced into fixtures.

Verification:

- focused config/auth/fetch tests;
- existing auth, analytics and component tests;
- `npm run typecheck`.

Commit: `test(v2.2): isolate local browser test identity`.

## Task 3: Add strict browser API fixtures

Frontend files:

- add `e2e/fixtures/preflight.ts`;
- add `e2e/fixtures/checkout.ts`;
- add `e2e/fixtures/report.ts`;
- add `e2e/fixtures/google.ts`;
- add `e2e/fixtures/share.ts`;
- add `e2e/support/api-router.ts`;
- add fixture-contract Vitest tests under `src/lib/e2e-v22/`.

Implementation:

1. model local responses for preflight, business confirmation, competitor discovery,
   Case creation, checkout state, analysis state, report loading, Google connections,
   source sync and report sharing;
2. validate fixture payloads with the same production parsers or schemas used by the
   frontend handlers;
3. implement per-test scenario state in Playwright routing so polling and retry sequences
   are deterministic without a shared database;
4. support explicit success, cancellation, failure, denial, mismatch, interruption and
   revocation scenarios;
5. fail on every unexpected API path or method instead of returning a permissive default;
6. keep every identifier synthetic and every URL under localhost or `.invalid`.

Tests first:

- each fixture validates against its production contract;
- unknown routes fail closed;
- scenario transitions are monotonic and idempotent;
- no fixture contains blocked credential/PII markers.

Verification:

- focused fixture-contract tests;
- existing report, payment, OAuth, connection and share contract suites.

Commit: `test(v2.2): add strict local journey fixtures`.

## Task 4: Convert critical components to interaction tests

Frontend files:

- extend `src/components/cases/case-payment-handoff.test.tsx`;
- extend competitor/preflight component tests or add focused `*.interaction.test.tsx`
  files beside the components;
- extend connection-center and Google source-control tests;
- extend report progress and share-control tests;
- add reusable render providers under `src/test/render.tsx` if needed.

Implementation and assertions:

1. drive controls with user-event rather than invoking callbacks directly;
2. assert accessible names, disabled/busy states and live status messages;
3. verify checkout success, cancellation and retryable error actions;
4. verify zero competitors blocks progress and one confirmed competitor permits it;
5. verify source identity confirmation and sync actions expose correct states;
6. verify report reconnection, technical failure compensation copy and share revocation;
7. retain existing static rendering tests where they provide distinct structural value.

Verification:

- `npm run test:components`;
- full `npm test`;
- `npm run typecheck`.

Commit: `test(v2.2): cover critical component interactions`.

## Task 5: Implement the six Playwright journeys

Frontend files:

- add `e2e/prospect-acquisition.spec.ts`;
- add `e2e/case-checkout.spec.ts`;
- add `e2e/prospect-report.spec.ts`;
- add `e2e/google-oauth.spec.ts`;
- add `e2e/verified-upgrade.spec.ts`;
- add `e2e/report-sharing.spec.ts`;
- add narrowly scoped local fixture loading to report and connection pages where their
  data is resolved during server rendering.

Implementation and assertions:

1. prospect acquisition completes goal, site, business, competitor and Coverage steps;
2. a separate zero-competitor scenario proves the user cannot continue;
3. checkout covers local success return, cancellation and provider error with no external
   navigation;
4. report generation advances queued to running to succeeded, interrupts the stream once,
   recovers via the existing fallback and renders exactly three top actions;
5. OAuth covers success, user denial and identity mismatch with fake callback state;
6. verified upgrade moves through connection-center source health and renders the change
   explanation without enabling production flags;
7. sharing creates, resolves and revokes a fragment-only credential, confirms the
   credential never appears in request URLs or artifacts, and confirms PostHog is absent;
8. every journey asserts that the external-request guard observed zero allowed exceptions
   and zero real external calls.

Verification:

- run every spec individually while stabilizing it;
- run the complete Playwright suite repeatedly with one and multiple workers;
- run `npm run test:e2e:ci` from a clean checkout-equivalent environment.

Commit: `test(v2.2): cover local end-to-end journeys`.

## Task 6: Scan and retain safe failure artifacts

Frontend files:

- modify `src/lib/security-v22/artifact-scan.ts` if directory selection needs expansion;
- modify `scripts/scan-security-artifacts.ts`;
- extend `src/lib/security-v22/artifact-scan.test.ts`;
- add `scripts/prepare-playwright-artifacts.ts` if redacted copying is required;
- update `.gitignore` for Playwright output.

Implementation:

1. scan `.next/static` and generated Playwright screenshots, traces and bounded logs;
2. reject OAuth codes/tokens, authorization headers, share fragments, configured secret
   values and synthetic sentinel markers without echoing them;
3. delete or exclude unsafe raw artifacts before the upload step;
4. preserve only final-failure artifacts and cap retention at seven days in CI;
5. keep generated test output outside git.

Tests first:

- clean screenshot/trace fixtures pass;
- each blocked marker fails with redacted output;
- a finding cannot be printed verbatim;
- an unsafe artifact is absent from the prepared upload directory.

Verification:

- `npm run security:scan` after a successful browser run;
- deliberate local sentinel failure followed by cleanup;
- full Vitest security suite.

Commit: `test(v2.2): secure browser failure artifacts`.

## Task 7: Gate Vercel deployment in GitHub Actions

Frontend files:

- replace `.github/workflows/vercel-auto-deploy.yml` with a gated workflow;
- update `README.md` with local gate commands only if contributor instructions exist;
- add no new long-lived deployment credential.

Implementation:

1. run on pull requests and pushes to `main` with concurrency cancellation;
2. use a pinned Node major and `npm ci`;
3. run type checking, Vitest, contract consistency, production build and security scan;
4. install only Playwright Chromium and run the local stubbed journeys in a separate job;
5. scan browser artifacts before uploading final-failure diagnostics for seven days;
6. make the deployment-hook job depend on both quality and browser jobs and restrict it
   to successful `main` pushes;
7. preserve the existing `VERCEL_HOOK_URL` secret and never print its value;
8. add a workflow-structure test that proves a failed dependency prevents the deploy job
   and pull requests cannot deploy.

Verification:

- lint or parse the workflow locally;
- run the production-equivalent local gate;
- push a branch with a deliberately failing check and verify no production deploy occurs;
- remove the deliberate failure, rerun green and verify one production deployment;
- inspect Vercel runtime errors and the production alias after deployment.

Commit: `ci(v2.2): gate Vercel on frontend quality`.

## Task 8: Complete V22-090

Backend documentation files:

- add `docs/superpowers/specs/2026-09-10-searchtrust-v2-2-frontend-test-infrastructure-completion.md`;
- update `docs/superpowers/specs/2026-08-26-searchtrust-v2-2-development-plan.md`;
- update this plan and the approved design status.

Final verification:

1. run TypeScript, complete Vitest, component interaction, contracts, database tests,
   security scan, production build and full Playwright;
2. confirm all browser requests remained local;
3. confirm checkout and Google fixtures created no production records or provider calls;
4. confirm share-page analytics and credential-boundary tests pass;
5. confirm both repositories are clean and pushed;
6. record test totals, CI run, Vercel deployment ID, artifact-retention behavior and
   unchanged Railway/Google/Verified boundaries in the completion document.

Completion gate:

V22-090 is complete only when the six deterministic local journeys and critical
component interactions pass; all external requests fail closed; safe diagnostics are
available for final browser failures; and Vercel production deployment is structurally
and operationally impossible until every frontend quality job succeeds.
