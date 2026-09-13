# SearchTrust V2.2 direct production release design

Date: 2026-09-13
Milestone: V22-093
Status: approved design, awaiting written-spec review

## 1. Decision

V22-093 will be a direct SearchTrust V2.2 production release rather than a staged
rollout. SearchTrust has no active production customers whose traffic needs cohort
separation, and the complete V2.2 frontend, backend, database and rollback boundary
have already passed V22-090 through V22-092. The earlier sequence of internal accounts,
three controlled consultants and 10–15 paid validation users is removed.

Direct release changes traffic policy, not safety policy. GitHub quality gates,
Vercel/Railway wait-for-CI controls, cost limits, failure-credit refunds, durable jobs,
provider circuit breakers and the non-destructive new-intake pause remain mandatory.

## 2. Current production state

- The V2.2 frontend is deployed on `trysearchtrust.com` and the production public-entry
  gate is explicitly open.
- Railway production Web and Worker are healthy and deploy only after the backend
  GitHub quality gate succeeds.
- Supabase contains 20 ordered V2.2 migrations with local/remote parity. V22-092 proved
  74 schema assertions, 28 rollback-only acceptance assertions and zero residue without
  changing existing data.
- Executable V2.1 routes, persistence adapters, report rendering and compatibility paths
  are retired and remain deletion invariants.
- The production environment still has feature-specific switches inherited from safe
  disabled rollouts. V22-093 must inventory these by key and scope without printing
  secret values before deciding whether any production change is required.

## 3. Public capability boundary

The direct release makes the following V2.2 journey generally available:

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

Secrets are never copied into documentation or command output. Validation records only
whether each required key exists, its environment scope and whether paired values pass a
safe equality/format check.

## 5. Release sequence

1. Confirm both repositories are clean and equal to `origin/main`.
2. Capture a key-name/scope-only inventory for Vercel production and Railway production
   Web/Worker. Abort on an unknown project, environment or service ID.
3. Run configuration preflight. Do not open a capability whose required secret,
   callback, provider budget or durable-job dependency is incomplete.
4. Run the existing frontend quality/database/browser gates and the backend 1,477-test
   release gate.
5. Apply the minimum approved production switch changes. No schema or business-row
   mutation belongs to V22-093.
6. Let GitHub trigger the normal Vercel and Railway production paths; verify production
   does not deploy before CI succeeds and that every deployment matches the successful
   commit.
7. Run non-destructive production smoke checks for page availability, preflight,
   competitor discovery readiness, checkout UI/configuration readiness, task health,
   Google connection availability and report reads. Do not create a provider checkout,
   submit a real payment or start a paid analysis solely for release testing.
8. Inspect Vercel, Railway Web and Railway Worker error logs, confirm backend health and
   record bounded evidence.

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

V22-093 is complete when all of the following are true:

- the prior staged-user requirements are removed from the development plan;
- the complete public V2.2 prospect journey is open without a cohort gate;
- at least one competitor remains mandatory for progression;
- Dodo checkout is reachable, but release testing creates no real charge;
- analysis, refund and regeneration contracts remain intact;
- GSC/GA4 open only if complete paired configuration passes preflight;
- the SerpAPI GBP path is available and official GBP OAuth sync remains safely closed;
- no V2.1 path is restored and no database migration or existing row is changed;
- frontend, backend and database gates pass;
- Vercel and Railway deploy only exact successful commits;
- production health checks pass and initial error-level logs are empty;
- the intake pause dry-run still resolves only the approved production targets;
- both repositories finish clean and equal to `origin/main`;
- the completion record states that V2.2 is normally released, not in gray rollout.

## 9. Explicit non-goals

- user cohort, percentage or invitation-based rollout;
- official GBP OAuth activation without an account and real acceptance;
- real-card release testing;
- precise new analytics instrumentation;
- V2.1 compatibility or historical report backfill;
- migration, data cleanup or deletion work.
