# V22-090 Frontend test infrastructure design

Date: 2026-09-10  
Status: design approved, awaiting written-spec review

## 1. Goal

V22-090 adds a reliable frontend release gate for SearchTrust V2.2. It keeps the
existing fast Vitest suite, adds browser-realistic component and end-to-end coverage,
and prevents Vercel production deployment until every required frontend check passes.

The browser suite is fully local and stubbed. It must not call production payment,
Supabase, Railway, Google or provider services, and it must never consume a Case
entitlement or account credit.

## 2. Current state

- The frontend has 592 passing Vitest tests across 72 files.
- Type checking, contract checks, database migration tests, security scanning and a
  production build can be run locally.
- React Testing Library and Playwright are not installed.
- The current GitHub workflow invokes the Vercel deployment hook immediately after a
  push to `main`; it does not run test gates first.

## 3. Chosen approach

Use a balanced three-layer test architecture:

1. Vitest remains the fast unit and service-contract layer.
2. React Testing Library covers critical component interaction and accessible state.
3. Playwright covers six complete browser journeys against local deterministic stubs.

The rejected minimal approach would leave important cross-page behavior unproved. The
rejected full local-stack approach would duplicate V22-091 backend integration work and
make this frontend milestone unnecessarily slow and fragile.

## 4. Test architecture

### 4.1 Vitest

Retain existing coverage for view models, source health, evidence coverage, version
diffs, state machines, API handlers, database migrations and security boundaries. This
layer stays independent of a browser server and should remain the fastest feedback
loop.

### 4.2 React Testing Library

Add Testing Library with a browser-like Vitest environment for a small set of critical
React components. These tests exercise behavior through visible controls and accessible
roles rather than component internals.

The component layer covers:

- preflight and competitor-selection state transitions;
- payment handoff success, cancellation and failure states;
- report task progress, reconnection and terminal states;
- connection-center source health and identity confirmation;
- share creation and revocation controls.

### 4.3 Playwright

Run Chromium against a locally built or locally started Next.js application. Every test
gets an isolated browser context and a fresh deterministic synthetic Case identity.

Network interception supplies strict fixtures for SearchTrust APIs and external
redirects. A request allowlist fails the test if the browser attempts to contact real
Supabase, Railway, Dodo, Google, PostHog or provider endpoints.

## 5. Browser journeys

### 5.1 Prospect acquisition

Enter the goal, website and business identity; complete preflight; confirm at least one
eligible competitor; and reach the Coverage step. Cover the blocking zero-competitor
state separately.

### 5.2 Case checkout

Use a Dodo-shaped checkout stub to cover successful return and Case unlock. Also cover
user cancellation and provider failure without creating a real payment.

### 5.3 Prospect report

Move one synthetic task through queued, running and succeeded states. Interrupt the
status stream once, prove polling or reconnect recovery, then verify the final report
and exactly three top actions.

### 5.4 Google OAuth

Use a fake callback to cover authorization success, access denial and an identity
mismatch. Fixtures contain no usable OAuth token, authorization code or customer
account identifier.

### 5.5 Verified upgrade

Start from a Prospect Report, open the connection center, simulate GSC, GA4 and GBP
health states, and render the resulting Verified view and change explanation. Disabled
production feature flags remain unchanged.

### 5.6 Sharing

Create, open and revoke a synthetic report share. Prove that the share credential stays
out of HTTP paths and captured artifacts, revoked access fails, and PostHog is not
initialized on share pages.

## 6. Fixtures and isolation

- Fixtures use reserved synthetic UUIDs, `.invalid` domains and non-routable provider
  identities.
- API fixtures must validate against the same frontend contracts used by production
  handlers.
- No test reads local production credentials. CI receives only the values needed to
  build the application and invoke the deployment hook after all gates pass.
- Browser storage, cookies and mocks are reset between tests.
- Tests must be order-independent and safe to run concurrently where their local
  server state permits it.

## 7. CI release gate

On every pull request and push to `main`, GitHub Actions runs:

1. locked dependency installation;
2. TypeScript type checking;
3. the complete Vitest suite;
4. frontend/backend V2.2 contract consistency checks;
5. the sensitive-artifact scan;
6. the production Next.js build;
7. Playwright Chromium journeys against the local application.

Only a successful `main` run may invoke the existing Vercel deployment hook. Pull
requests never deploy production.

Railway deployment automation is outside V22-090. It will be evaluated with V22-091 so
backend tests and queue/restart integration checks can gate backend publication as one
coherent workflow.

## 8. Failure behavior and artifacts

- Browser infrastructure failures may retry once.
- Business assertion failures are not repeatedly retried.
- On final failure, CI uploads the failed test's screenshot, Playwright trace and bounded
  diagnostic output for seven days.
- Successful runs do not upload bulky browser traces.
- The existing sensitive-output scanner is extended to inspect generated browser
  artifacts before upload. A sensitive finding fails the workflow and prevents upload
  of the affected raw artifact.
- Analytics, test setup and artifact handling are best-effort side concerns and may not
  change the product result seen by a user.

## 9. Acceptance criteria

V22-090 is complete when:

1. existing Vitest behavior remains green;
2. critical component interaction is covered through Testing Library;
3. all six local Playwright journeys pass without any real external request;
4. checkout cancellation/failure, OAuth denial/mismatch, stream interruption and share
   revocation have explicit assertions;
5. share pages do not initialize PostHog or expose their credential in artifacts;
6. the CI workflow gates Vercel deployment on type checking, unit tests, contracts,
   security scanning, production build and Playwright;
7. failed browser runs produce safe, seven-day diagnostic artifacts;
8. a deliberate failing check proves the Vercel deployment step is skipped;
9. the frontend worktree is clean and the full production-equivalent local gate passes.

## 10. Scope boundary

This milestone does not enable Google sync or Verified Generation, call real payment or
provider services, add product analytics, change report behavior, or create database
migrations. Backend provider contracts, golden reports, property-based testing,
queue/restart integration and V1 regression expansion belong to V22-091.
