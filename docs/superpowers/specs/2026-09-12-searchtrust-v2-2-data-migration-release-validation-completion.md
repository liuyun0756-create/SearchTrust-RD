# V22-092 Data migration release validation completion

Date: 2026-09-12
Status: complete

## Outcome

SearchTrust V2.2's data layer and non-destructive intake pause are validated in the
approved production environment. All synthetic database writes were transaction-local
and rolled back. Existing data remained unchanged, V2.1 product paths stayed retired,
and V22-093 traffic enrollment was not started.

## Migration and database evidence

- Approved Supabase target: `searchtrust-production` (`cmdsbsvcxesnrrealftb`), healthy
  in `us-east-2`.
- Final local/remote migration parity: 20/20, ordered from `20260706000000` through
  `20260912100000`.
- The initial privilege proof found inherited `EXECUTE` for `anon` and `authenticated`
  on `rotate_v22_report_share`. The owner approved the forward-only migration
  `20260912100000_restrict_v2_2_report_share_rotation.sql`; it revokes those grants and
  changes no application rows. No existing migration was rewritten, repaired or
  reapplied.
- Linked database lint returned no error-level finding.
- Remote schema suite: 74/74 passed.
- Remote rollback-only acceptance: 28/28 passed. The validator accepted 37 SQL contract
  statements with digest
  `87980580a6d8b0ab7452a8ee76811c66dab668b86aac0353757f325cf05b2a20`.
- Independent residue proof: 1/1 passed. Its 10 statements had digest
  `f09ccb510da1d1e163c0674700d5135648ba9a58625052879dc6c220ca8c4757`.
- Exact before/after counts were identical: 17 users, 9 orders, 28 reports, 1
  `client_cases` row, and zero rows in the other 14 V2.2 workflow tables checked.
- The server-side digest of all report rows was identical before and after:
  `5a267e6588aaf9ef3e0c6c16bc62f645bef9be8de7a982df9469dbb89989c613`.
  No report payload, identity, token, payment field or provider content was exported.

## Local verification

- All 20 migrations applied to a fresh isolated Supabase PostgreSQL 17.6 database.
  Native pgTAP passed 103/103: 74 schema, 28 release acceptance and 1 residue check.
- The normal local Supabase port set was already owned by another project. The isolated
  database was used so that project was not stopped or altered; the temporary container
  was removed afterward and no configuration diff remained.
- PGlite migration and safety coverage passed 30/30.
- Frontend quality gate passed: typecheck, 89 Vitest files with 655 tests, generated
  contracts, production build, security scan of 38 files with zero findings, and 12
  deterministic browser journeys (11 normal plus 1 paused-intake journey).
- Backend release coverage passed 1,477/1,477: 1,456 fast tests and 21 Redis integration
  tests.

## Intake pause and release evidence

- `V22_PUBLIC_ENTRY_ENABLED` is server-only, fail-closed, and explicitly `true` only in
  Vercel production. Closing it blocks new Case, preflight, competitor discovery,
  checkout and analysis writes while preserving existing Case/report reads and internal
  settlement callbacks.
- The operational controller is dry-run by default, fixed to the approved Vercel and
  Railway project/service IDs, and contains no SQL or data-deletion path. Both close and
  open previews resolved the expected controls; production remained open throughout
  acceptance.
- Frontend GitHub Actions run
  `https://github.com/liuyun0756-create/search-trust/actions/runs/34703097855` passed. Its
  production trigger ran only after the quality and browser jobs succeeded.
- Vercel deployment `dpl_AZ57nJtFDtvqH4MsAMLD45KvNojQ` reached `READY` at exact commit
  `1056e57e8e8e6816baed96c1bc17445401e2158a` and serves `trysearchtrust.com`.
- Production smoke returned HTTP 200 for `/cases/new` and backend `/api/v1/health`.
  Retired `/api/analyze` and V2.1 report paths remained 404. No payment or analysis was
  submitted during smoke testing.

## Safety and release boundary

- No production row was deleted or modified by the acceptance probes.
- No migration-history row was edited, and no reverse migration was used.
- No V2.1 API, renderer, persistence adapter or report contract was restored.
- V22-093 remains a separate staged-release decision. This completion does not enroll
  internal accounts, consultants or paid validation users.

## Final repository and deployment closure

The frontend/database repository is clean at
`1056e57e8e8e6816baed96c1bc17445401e2158a`, equal to `origin/main`. The central
documentation/backend release commit is
`04118c48eb420d2e43ebf60c04bb3b2dd9e79967`. GitHub Actions run
`https://github.com/liuyun0756-create/SearchTrust-RD/actions/runs/34703865253` completed
successfully: Backend quality ran from `15:59:31Z` to `16:01:32Z`, and Redis integration
ran from `15:59:31Z` to `16:00:03Z`. Railway kept both production deployments in
`WAITING` until that gate passed, then released the exact commit as `SUCCESS`:

- Web deployment `78153551-5d79-4102-923c-f1147600cec6`;
- Worker deployment `19d69f6d-c8ce-4e3d-8746-5a63e9fc38c8`.

The post-deployment backend health check returned HTTP 200 with status `ok`; the first
100 error-level log entries for both deployments were empty. This completion-evidence
update is documentation-only and follows the same GitHub/Railway gate before repository
closure.
