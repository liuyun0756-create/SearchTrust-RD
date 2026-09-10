# V22-081 Security hardening implementation plan

Design source: `2026-09-10-searchtrust-v2-2-security-hardening-design.md`.
This milestone hardens the V2.2 OAuth, token, URL-fetch, report-share, logging and
account-deletion boundaries across `SearchTrust-RD` and `search-trust`. Google sync
and Verified Generation remain disabled in production.

## 1. Freeze the threat model and security contracts

- Add a threat-control-test matrix covering OAuth session binding, scope and subject
  substitution, broker replay, ciphertext substitution, key rotation, SSRF, report
  sharing, sensitive logging and account deletion.
- Record the existing controls and the exact tests that prove them; every uncovered
  threat must point to a task below rather than an assumed guarantee.
- Add stable security error/result codes before implementation so provider text,
  database details and secret values never become part of a public contract.

## 2. Add deletion receipts and atomic user-graph deletion

- Add an additive Supabase migration for service-role-only identity deletion receipts
  containing only SHA-256 event/subject digests, trusted event time, completion time
  and a fixed result code.
- Add database functions to make a user's Google connections unavailable before
  revocation and to atomically persist the receipt plus delete the local user graph.
- Add the 90-day completed-receipt cleanup function, strict constraints, RLS, grants
  and database integration tests for duplicate, cross-user and cascade behavior.
- Make the create-user path consult deletion receipts so an older `user.created`
  delivery cannot restore a deleted identity.

## 3. Implement safe Clerk account deletion

- Move verified Clerk event handling behind a focused identity-webhook service and
  repository while keeping signature verification at the route boundary.
- For `user.deleted`, locate the user only by the verified Clerk subject, freeze token
  brokerage, decrypt each available Google credential in memory, attempt bounded
  provider revocation, clear credentials and complete the atomic local deletion.
- Treat missing users and duplicate events as successful; return a retryable server
  failure only when the local deletion transaction fails.
- Redact payloads and provider failures and cover valid signatures, invalid signatures,
  replay, late create, provider timeout, partial connection state and user isolation.

## 4. Build resumable dual-key token rotation

- Extend the token vault/repository boundary with context-safe re-encryption and
  compare-and-swap batch updates for Google connections and live OAuth sessions.
- Add an operator-only rotation command supporting `dry-run`, `execute` and `verify`,
  configurable batch size, bounded failures and machine-readable secret-free output.
- Require old and new keys to coexist, require the new key to be active for execution,
  reject mixed connection token state and retain failed records on the old version.
- Test fresh IVs, wrong AAD/tag/key, concurrent refresh conflicts, interruption/resume,
  repeat execution and the old-version-zero removal gate.

## 5. Unify SSRF protection for V2.2 URL fetches

- Extract the proven V2.2 safe-URL and pinned-connection rules into a shared security
  module and preserve compatibility imports for current preflight consumers.
- Apply the shared result to preflight homepage/GBP fetches, site inventory and
  competitor public-profile fetches, plus any legacy fetch entry actually called by
  a V2.2 job.
- Revalidate every redirect; reject mixed DNS answers, IPv4-mapped IPv6, cloud metadata,
  URL userinfo, unsafe ports and non-HTTP schemes; preserve response, redirect and
  total-time limits.
- Add deterministic resolver/transport tests proving a blocked or rebound target never
  receives the application request.

## 6. Lock report-share permissions and response policy

- Keep the existing 32-byte token, SHA-256 database hash, one-active-link transaction
  and 30-day expiry contract.
- Make the page and PDF routes share the same resolver and consistent 404 behavior for
  invalid, expired, revoked, wrong-view and wrong-report records.
- Add `noindex`, no-referrer and no-store protections to every successful and failed
  share response without exposing Case, evidence, Google connection, task or payment data.
- Add route and service tests for cross-owner management, rotation, expiry boundaries,
  PDF access and client-view field exclusion.

## 7. Add repository-wide sensitive-output gates

- Generalize the V2.2 safe logger so TypeScript route/service errors and Python V2.2
  worker/provider errors retain only approved IDs, stages, codes, status and timing.
- Add sentinel-driven tests for nested headers, URLs, exception text, provider bodies,
  OAuth values, API keys and representative PII.
- Add a deterministic security scan command for captured test output and `.next`
  browser artifacts; allow only explicitly identified fake fixture secrets.
- Run the scan in the local release gate and fail closed when scan inputs are missing or
  any sentinel value is found.

## 8. Verify, deploy and drill safely

- Run focused migration, OAuth, rotation, webhook, SSRF, share and logging suites before
  the complete backend and frontend test/type/contract/build gates.
- Apply the additive production migration before deploying Vercel and Railway code;
  verify Google OAuth, all Google sync flags and Verified Generation remain disabled.
- Create one isolated synthetic production graph, run rotation dry-run/execute/resume/
  verify, rotate and revoke its share, then deliver a correctly signed synthetic delete
  event and prove the graph is no longer reachable.
- Remove all synthetic data and temporary local configuration, scan Vercel/Railway logs,
  verify API/Worker/queue health and record exact non-sensitive evidence in a completion
  document and the main V2.2 development plan.

## Completion gate

V22-081 is complete only when every threat has a tested control, dual-key rotation is
resumable and leaves no synthetic old-version records, all V2.2-reachable URL fetches
pass SSRF adversarial tests, report shares enforce the approved anonymous read-only
contract, sensitive-output scans are clean, signed account deletion removes the full
local user graph without cross-user impact, full regressions and production health pass,
the synthetic drill is cleaned up, and every Google/Verified production flag remains off.
