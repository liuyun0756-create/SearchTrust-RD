# SearchTrust V2.2 direct production release completion

Date: 2026-09-16

Milestone: V22-093

Status: complete; normally released to production without gray rollout

## 1. Outcome

SearchTrust V2.2 is released directly to the normal production environment. There is
no internal-account allowlist, consultant cohort, invitation code or percentage gate.
The public preflight, competitor discovery, Case checkout, GSC/GA4 connections and
Verified Generation capability are enabled. Official GBP OAuth remains intentionally
disabled; the supported public GBP evidence path continues to use SerpAPI.

The final customer-facing Dodo product is named `SearchTrust Prospect Report`. Its
existing product ID, one-time pricing model and `$19.00` amount were preserved. The
live hosted checkout displayed the new name and amount. No customer or card data was
entered and no payment was submitted during this final verification.

## 2. Exact production deployments

### Frontend

- reviewed source commit:
  `38f2a1be4a0e7678a5821614d718beaeeb324a60`;
- source deployment before the switch change:
  `dpl_2iPLdBvdaoVTtXFm4scUR5FzaoTz`;
- final production redeployment with Verified Generation enabled:
  `dpl_7pgM8Sj7DbBCEmBYWfAvf19smBBb`;
- final instance:
  `https://search-trust-62qy3l9yr-liuyuns-projects-9eb2d9a4.vercel.app`;
- `https://trysearchtrust.com` points to the final `READY` deployment and returned
  HTTP 200; `/cases/new` also returned HTTP 200.

The final Vercel deployment is a redeployment of the reviewed source deployment, so
the environment change does not introduce a different source tree.

### Backend

- reviewed source commit:
  `bdc5a3353df675e24b1228d7fb9f981b96c9cb4f`;
- Railway production Web deployment after enabling Verified Generation:
  `bbb601de-9c99-43bf-8787-6ea2ab25b147`;
- Railway production Worker deployment after enabling Verified Generation:
  `b600c5db-c076-4896-81da-d777ea5133c4`;
- both deployments reached `SUCCESS` and report the exact reviewed backend commit;
- `/api/v1/health` returned HTTP 200 with `status=ok`;
- `/api/v2/health/queue` returned `status=ok`, Redis connected, Worker alive and
  `pending_callbacks=0`.

## 3. Production capability matrix

Enabled in Vercel production:

- `V22_PUBLIC_ENTRY_ENABLED=true`;
- `GOOGLE_CONNECTIONS_ENABLED=true`;
- `GOOGLE_GSC_SYNC_ENABLED=true`;
- `GOOGLE_GA4_SYNC_ENABLED=true`;
- `GOOGLE_VERIFIED_ANALYSIS_ENABLED=true`.

Enabled in Railway production on the consuming services:

- `V22_PREFLIGHT_ENABLED=true`;
- `V22_COMPETITOR_DISCOVERY_ENABLED=true`;
- `V22_ANALYZE_ENABLED=true`;
- `V22_GSC_SYNC_ENABLED=true`;
- `V22_GA4_SYNC_ENABLED=true`;
- `V22_VERIFIED_ANALYSIS_ENABLED=true` on both Web and Worker.

The required Dodo, Google OAuth, Supabase, broker, encryption and durable-job
configuration names are present in their production scopes. Values are not recorded in
this document.

`GOOGLE_GBP_SYNC_ENABLED` and `V22_GBP_SYNC_ENABLED` remain absent/default-false. This
is intentional because official GBP OAuth is outside the V2.2 release requirement.

## 4. Acceptance evidence

- frontend repository and backend repository were clean and equal to `origin/main`
  before the release switch operation;
- the latest frontend quality run passed 100 test files with 994 checks and 115
  deterministic browser journeys;
- the reviewed backend quality and deployment gates passed before Railway released the
  exact commit;
- the previously approved 29-migration production database sequence remains intact;
- the public journey recovered a missing competitor-discovery task, found six candidates
  and allowed the top three to be confirmed before reaching Coverage and checkout;
- the hosted Dodo checkout displayed `SearchTrust Prospect Report` at `$19.00`;
- Vercel, Railway Web and Railway Worker error-level log scans after the final switch
  deployment returned no error entries;
- the fixed-target intake pause controller still provides the non-destructive rollback
  path. No V2.1 route or compatibility path was restored.

The release did not create a real charge, submit a paid analysis or modify historical
application rows. Existing payment settlement, single-credit consumption, failure
refund and explicit retry behavior remain protected by the completed deterministic
release suites.

## 5. Closure

V22-093 is closed as a normal production release, not a gray rollout. Future work is
post-release observation through the existing PostHog coarse funnel and immutable
database payment/credit facts. Any official GBP OAuth integration remains a separate
milestone and must not reuse this completion as its acceptance.
