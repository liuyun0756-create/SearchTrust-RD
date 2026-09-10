# V22-081 Security hardening completion

Completed: 2026-09-10  
Status: implemented, verified and deployed to production

## Delivered

- Added the V22-081 threat matrix and linked every accepted threat to a concrete code
  control and automated test.
- Added dual-key Google token rotation with dry-run, bounded execute batches, fresh IVs,
  compare-and-swap writes, resume support and an old-key-zero verification gate.
- Unified V2.2 outbound URL validation and pinned-IP transport. Private, loopback,
  metadata, mixed-DNS, mapped-IPv6, rebinding and redirect pivots are rejected.
- Moved anonymous report bearer tokens into the URL fragment. The public request path is
  fixed at `/share`, token resolution uses a bounded POST body, and share pages suppress
  analytics while enforcing no-store, no-referrer and noindex headers.
- Preserved one active 256-bit share token per report, a fixed 30-day expiry, revocation,
  client-only rendering and uniform not-found responses. Legacy path-token routes now
  return 404 with the same privacy headers.
- Added shared allowlist logging and browser artifact scans for credentials, OAuth tokens,
  cookies, email addresses, provider bodies and sentinel values.
- Added a Clerk deletion fence, bounded best-effort Google revocation, atomic graph
  deletion, digested idempotency receipts and a late-create guard.
- Kept Google OAuth, GSC, GA4, official GBP synchronization and Verified Generation
  disabled in production.

## Verification

- Backend full suite: 1,592 tests passed.
- Frontend full suite: 587 tests passed across 72 files.
- Frontend TypeScript check: passed.
- Frontend production build: passed.
- Cross-repository v2.2 contract generation/check: passed.
- Browser artifact scan: 44 files scanned with a unique sentinel and zero findings.
- Production SSRF, share, invalid-signature, health and log checks passed.
- Vercel Runtime Logs scan returned no application errors or sensitive-value matches.
- Railway API logs returned no application errors or credential/email matches. Worker
  logs contained only the platform-classified ARQ stderr stream, with zero failed jobs,
  tracebacks or sensitive-value matches.

## Production release

- Supabase migration: `20260910000000_add_v2_2_security_hardening.sql`, verified present
  in the remote migration ledger.
- Backend commit: `148634d`.
- Railway production API deployment: `54c57f09-a940-4b0d-ae73-f042b53e834b`.
- Railway production Worker deployment: `0f332f74-d33e-4b1c-9ff1-59cbe304f7ab`.
- Frontend commits: `34812ba`, `debdd18`.
- Vercel production deployment: `dpl_FELWYD2ShAkrjerKaBzxmZWEqmcj`.
- Production alias: `https://trysearchtrust.com`.

## Production drills

### Database and token lifecycle

- Created one isolated synthetic graph and proved rotation dry-run, execute, resume and
  verify behavior. The old key version reached zero before any retirement step.
- Rotated and revoked a synthetic share, confirmed the former bearer was unusable, and
  removed the synthetic user graph atomically.
- Replayed deletion and late-create paths to prove idempotency and tombstone fencing.
- Confirmed no drill-prefixed users, Google connections, OAuth sessions or reports
  remained after cleanup.

### Signed Clerk deletion

- Confirmed the production Clerk endpoint targets
  `https://trysearchtrust.com/api/webhook/clerk` and uses the deployed signing secret.
- Corrected the endpoint subscription boundary from `user.created` only to
  `user.created` plus `user.deleted`.
- Created the clearly labelled Clerk production user `Security Drill V22-081-20260910`.
  Its identifier-free `user.created` event was correctly signature-verified and then
  rejected by the application email invariant as `WEBHOOK_INVALID`; no local user was
  created from that intentionally incomplete event.
- Seeded one synthetic local user, Case, error-state Google connection, report and active
  share for the same Clerk subject, then deleted the Clerk user from the production
  dashboard.
- Clerk/Svix recorded the resulting `user.deleted` delivery as `Succeeded`. The
  application recorded a `deleted` receipt and removed the complete local graph; the
  user, Case, connection and report marker counts were all zero and the report share was
  removed by the verified report/user cascade.
- Removed the exact synthetic deletion receipt after recording the result. The Clerk
  user and every synthetic database artifact now have zero remaining rows.

## Release boundary and next step

V22-081 does not enable any Google or Verified product capability and does not use real
customer tokens or customer records. Its production changes are additive except for the
intentional retirement of legacy path-token share URLs.

Next: V22-082 cost control. Record provider calls, tokens, elapsed time and retries for
each job, then enforce the approved non-core truncation order without weakening promised
report modules.
