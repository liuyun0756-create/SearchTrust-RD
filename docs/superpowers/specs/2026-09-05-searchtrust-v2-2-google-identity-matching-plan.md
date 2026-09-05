# V22-052 Google resource identity matching

Scope: extend V22-051 selection with server-side identity assessment and explicit
confirmation. No OAuth flag enablement, data sync, billing, or report changes.

## Decision rules

- GSC: exact domain or URL-prefix scope can be high confidence. Parent domains,
  broader/narrower paths and www/protocol variants need review; unrelated hosts
  or unrelated path scopes are rejected. Domain comparisons use label boundaries.
- GA4: inspect every web stream, including missing/invalid default URIs. Only
  complete, consistent exact-site evidence is high confidence. Mixed domains,
  related hosts, missing streams/URLs, and differing paths require confirmation.
  A complete set of unrelated website URLs is a mismatch.
- GBP: evaluate website, exact normalized business name, structured country,
  locality/postcode, and service-area names. Missing data never counts as a match.
  A shared brand homepage plus city is insufficient for automatic branch identity.
  The current Case model lacks street address or a validated unique branch ID;
  therefore GBP evidence requires manual review, even if a non-root URL and
  name/city/postcode agree. Non-root paths are not assumed to be branch-specific.
  Conflicting website/country is rejected. Name/address
  variations are review clues, not fuzzy automatic matches.
- Matched/high may be automatically confirmed when the user saves the resource.
  needs_confirmation/medium or low requires a separate explicit checkbox.
  mismatch cannot be overridden by the checkbox.

## Implementation and persistence

Preview returns safe reason codes/messages, confidence, Case comparison clues,
and a deterministic review digest. Save independently re-fetches owned Case and
Google resource, recomputes the assessment, and compares the review digest. A
changed resource or Case requires another review. No client score is accepted.

A new service-role-only RPC wraps existing selection in one transaction, locks
connection then Case, checks the Case updated_at and expected active binding,
and records status/evidence, automatic vs user confirmation, confirmer and time.
No raw provider payloads or tokens are stored in matching evidence. Existing
binding columns suffice; no new tables/columns. A Case identity-change trigger
invalidates active confirmations without deleting selection or report history.

Migration: 20260905200000_add_v2_2_google_identity_matching.sql. Apply and verify
before deploying the frontend. Existing V22-051 RPC remains for compatibility;
it still cannot assert a matched identity. Rollback disables new routes before
dropping the new RPC/trigger, retaining data.

## Verification

Pure matcher boundary/IDN/missing/mixed-stream/branch/service-area tests; service
authorization, forged input, missing explicit confirmation, preview/save drift,
and safe projection tests; PostgreSQL atomic replacement, stale Case, audit,
invalid combinations, grants and Case-edit invalidation tests; full frontend
tests/typecheck/build; production migration catalog, Vercel commit/READY and
Railway queue read-only checks. Live Google acceptance remains pending credentials.

## Primary references

- https://support.google.com/webmasters/answer/34592?hl=en
- https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.dataStreams
- https://developers.google.com/my-business/reference/businessinformation/rest/v1/locations

Conservative confirmation thresholds are SearchTrust product rules, not Google's
claim of verified ownership or data health.
