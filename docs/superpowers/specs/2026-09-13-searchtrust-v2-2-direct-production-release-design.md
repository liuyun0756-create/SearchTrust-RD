# SearchTrust V2.2 direct production release design

Date: 2026-09-13
Milestone: V22-093
Status: approved; Step 9 complete, Step 10 infrastructure complete and real acceptance blocked on external configuration

## 1. Decision

V22-093 will be a direct SearchTrust V2.2 production release rather than a staged
rollout. SearchTrust has no active production customers whose traffic needs cohort
separation. V22-090 through V22-092 established the earlier production baseline, and
Verified Generation productization has now passed its local, fully offline release
gates. This does not mean that the new migrations, commits, configuration or switches
are already present in production. The earlier sequence of internal accounts, three
controlled consultants and 10–15 paid validation users is removed.

Direct release changes traffic policy, not safety policy. GitHub quality gates,
Vercel/Railway wait-for-CI controls, cost limits, failure-credit refunds, durable jobs,
provider circuit breakers and the non-destructive new-intake pause remain mandatory.

## 2. Current production state

- The complete frontend release commit
  `47476a07088cace350192d705a4d5347f716a550` is on `origin/main` and is the exact
  commit behind Vercel production deployment `dpl_2c7AEnRyeGo7cgVZHAvvLoget9P8`.
  `https://trysearchtrust.com` resolves to that Ready deployment and returned HTTP 200.
- The complete backend application release commit
  `bd028729e6f7e083954d6956c41d236686811bfc` is on `origin/main` and deployed to both
  Railway production Web and Worker. Both deployments reached `SUCCESS`/`RUNNING`;
  `https://searchtrust-rd-production.up.railway.app/api/v1/health` returned HTTP 200
  with status `ok`.
- The production Supabase project `searchtrust-production` now applies exactly 29
  ordered migrations through `20260914160000`. All nine migrations after the
  `20260912100000` baseline were applied in the approved order. Rollback-only release
  validation passed all 47 assertions and the residue validation found zero synthetic
  rows.
- Executable V2.1 routes, persistence adapters, report rendering and compatibility paths
  are retired and remain deletion invariants.
- `GOOGLE_VERIFIED_ANALYSIS_ENABLED` and `V22_VERIFIED_ANALYSIS_ENABLED` remain closed
  by their default-false configuration. `GOOGLE_GBP_SYNC_ENABLED` and
  `V22_GBP_SYNC_ENABLED` also remain closed.
- Production configuration now contains generated broker, OAuth-cookie and token-vault
  secrets plus the exact production broker origin and OAuth redirect URI. These writes
  are intentionally pending the single final redeploy after the remaining external
  values are supplied. No secret value is recorded here.
- `DODO_VERIFIED_CREDIT_PRODUCT_ID`, `GOOGLE_OAUTH_CLIENT_ID` and
  `GOOGLE_OAUTH_CLIENT_SECRET` are not present in any approved reusable production,
  preview, staging or local configuration. They remain release blockers.
- The production readiness inventory contains one active Case, but zero Prospect
  reports, zero healthy/matched GSC bindings, zero healthy/matched GA4 bindings and zero
  healthy unexpired customer-public-GBP snapshots. No Case is currently eligible for
  real Verified acceptance.

## 3. Public capability boundary

The direct release will make the following V2.2 journey generally available after the
production sequence and acceptance below succeed:

1. public new-Case intake and free preflight;
2. automatic competitor discovery with the existing requirement that at least one real
   competitor must be found or supplied by the user;
3. one-time Dodo Payments checkout attached to one Case;
4. durable report generation, failed-attempt credit refund and explicit regeneration;
5. GSC and GA4 connection/sync when their complete OAuth and worker configuration is
   present and passes preflight;
6. public GBP evidence through the existing SerpAPI-backed discovery path;
7. verified generation, evidence changes and report-version comparison;
8. owner-scoped report viewing, PDF and revocable sharing.

Official Google Business Profile OAuth sync is not a release requirement because the
owner does not have the official GBP account/backend needed for real acceptance.
`GOOGLE_GBP_SYNC_ENABLED` and `V22_GBP_SYNC_ENABLED` remain closed unless a later,
separately validated official integration is available. This does not block SerpAPI GBP
evidence used by the normal prospect-report flow.

## 4. Configuration model

The release uses explicit production-only switches. It does not create a user allowlist,
percentage rollout, invitation code or cohort table.

- Vercel new intake: `V22_PUBLIC_ENTRY_ENABLED=true`.
- Railway request paths: `V22_PREFLIGHT_ENABLED=true`,
  `V22_COMPETITOR_DISCOVERY_ENABLED=true` and `V22_ANALYZE_ENABLED=true` on the service
  where each setting is consumed.
- GSC/GA4: frontend `GOOGLE_CONNECTIONS_ENABLED`, `GOOGLE_GSC_SYNC_ENABLED` and
  `GOOGLE_GA4_SYNC_ENABLED`, paired with Worker `V22_GSC_SYNC_ENABLED` and
  `V22_GA4_SYNC_ENABLED`, open only if every required server-side OAuth, broker,
  encryption and callback dependency is already present and a non-destructive
  configuration preflight passes.
- Official GBP sync stays false as defined above.
- Verified Generation uses the paired frontend
  `GOOGLE_VERIFIED_ANALYSIS_ENABLED` and backend/Worker
  `V22_VERIFIED_ANALYSIS_ENABLED` switches. Both remain false during migration and
  deployment, and may be opened only together after database, Web and Worker preflight.

Secrets are never copied into documentation or command output. Validation records only
whether each required key exists, its environment scope and whether paired values pass a
safe equality/format check.

## 5. Release sequence

1. Independently review the release candidates and confirm both repositories are clean.
2. Capture a key-name/scope-only inventory for Vercel production and Railway production
   Web/Worker. Abort on an unknown project, environment or service ID.
3. Run configuration preflight. Do not open a capability whose required secret,
   callback, provider budget or durable-job dependency is incomplete.
4. Reconfirm the frontend quality/database/browser gates and the backend release gate.
5. Apply only the approved forward migrations through `20260914160000` and run the
   non-destructive production schema/transaction/residue validation. Do not rewrite a
   historical migration or delete business data.
6. Push the exact reviewed commits and let GitHub trigger the normal Vercel and Railway production paths; verify production
   does not deploy before CI succeeds and that every deployment matches the successful
   commit.
7. With both Verified switches still closed, verify database, Railway Web/Worker and
   frontend health; then apply the explicit paired switch change only if all required
   configuration exists.
8. Run non-destructive production smoke checks for page availability, preflight,
   competitor discovery readiness, checkout UI/configuration readiness, task health,
   Google connection availability and report reads. Do not create a provider checkout,
   submit a real payment or start a paid analysis solely for release testing.
9. Inspect Vercel, Railway Web and Railway Worker error logs, confirm backend health and
   record bounded evidence.

### 5.1 Execution record, 2026-09-15

Steps 1 through 7 have completed for the repository, database and closed-switch
infrastructure. Both GitHub `main` quality workflows report passing. Vercel production,
Railway Web and Railway Worker match the reviewed release commits above. The first-hour
error-level scan returned zero Vercel errors, zero Railway Web errors, zero Railway
Worker errors and zero Railway Web 5xx requests.

Step 8 cannot execute the real purchase/generation acceptance yet. The Dodo API key is
stored as a non-exportable Vercel Secret, so this execution could not safely enumerate
provider products or select a `$19` product. The named Verified product ID is absent.
Google OAuth client credentials are also absent, and the current production data has no
eligible Case. No checkout was created, no payment was submitted, no paid generation
was started and no Verified or official GBP switch was opened.

The backend is verified before the public frontend is declared released so the browser
cannot advertise a capability whose server path is still closed.

## 6. Failure behavior and rollback

- Provider or cost-control failure returns a stable public error and must not produce a
  partial report presented as complete.
- A failed paid generation returns its charged credit exactly once. A user-requested
  retry is a new attempt and consumes one credit.
- Durable task state survives Web/Worker restarts and duplicate callbacks remain
  idempotent.
- A severe incident closes new intake with the V22-092 fixed-target controller. Existing
  Cases, completed reports, PDFs, shares, payment webhooks and already-started task
  settlement remain available.
- A provider-specific incident may close only preflight, competitor discovery, analysis
  or Google sync. No rollback command deletes data, reverses migrations or reopens V2.1.
- Reopening after an incident is always a separate explicit operation after health and
  logs are clean.

## 7. Observability

PostHog remains the source for approximate funnel conversion. V22-093 does not add a new
fine-grained event taxonomy or customer cohort system. Release evidence is operational:

- CI conclusion and exact commit;
- deployment status and exact commit for Vercel, Railway Web and Worker;
- health status, queue readiness and error-level logs;
- key-name/scope configuration matrix without values;
- HTTP/status-only smoke results with no customer payloads.

## 8. Acceptance criteria

The criteria below remain the definition of full completion. As of the execution record
above they are **not yet all satisfied**: real Dodo purchase settlement, GSC/GA4-backed
Verified success, controlled failure/refund and explicit retry remain outstanding.

V22-093 is complete when all of the following are true:

- the prior staged-user requirements are removed from the development plan;
- the complete public V2.2 prospect journey is open without a cohort gate;
- at least one competitor remains mandatory for progression;
- Dodo checkout is reachable, but release testing creates no real charge;
- analysis, refund and regeneration contracts remain intact;
- GSC/GA4 open only if complete paired configuration passes preflight;
- the SerpAPI GBP path is available and official GBP OAuth sync remains safely closed;
- no V2.1 path is restored; only the reviewed forward migrations are applied and no
  existing business row is rewritten or deleted by acceptance;
- frontend, backend and database gates pass;
- Vercel and Railway deploy only exact successful commits;
- production health checks pass and initial error-level logs are empty;
- the intake pause dry-run still resolves only the approved production targets;
- both repositories finish clean and production deployments equal the exact successful
  commits;
- the completion record states that V2.2 is normally released, not in gray rollout.

## 9. Explicit non-goals

- user cohort, percentage or invitation-based rollout;
- official GBP OAuth activation without an account and real acceptance;
- real-card release testing;
- precise new analytics instrumentation;
- V2.1 compatibility or historical report backfill;
- migration rewriting, historical backfill, data cleanup or deletion work; the reviewed
  forward release migrations remain required.
