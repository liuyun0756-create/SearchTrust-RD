# V22-092 Data migration release validation implementation plan

Date: 2026-09-12
Status: completed

Design source:
`2026-09-12-searchtrust-v2-2-data-migration-release-validation-design.md`.

This plan validates the already-applied V2.2 Supabase schema in the approved production
project without reapplying migrations or retaining synthetic data. Database probes are
transactional and roll back. The only new runtime behavior is a server-side control for
pausing new V2.2 intake while preserving existing Case and report reads.

## Baseline and repositories

- Frontend/database repository:
  `/Users/liuyun3/Documents/SearchTurst前后端/search-trust`
- Central plan/backend repository:
  `/Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD`
- Supabase target: `searchtrust-production` (`cmdsbsvcxesnrrealftb`), the only
  SearchTrust Supabase project.
- Remote state at design time: active and healthy; all 19 local migration versions have
  matching remote entries.
- Frontend baseline: `4220ff4d5a718f5b317d4e28a48f2407690d28c5`, clean and equal to
  `origin/main`.
- Backend baseline before the local design commit:
  `c9a0ab70fe8ba0ebac606866201cdbb446ea3626`.
- V2.1 product code remains retired. No task may restore a V2.1 route, adapter, renderer
  or report contract.

## Approved implementation amendment

The design-time baseline contained 19 migrations. The production privilege acceptance
test then identified inherited browser-role execution on the V2.2 report-share rotation
function. With the owner's approval, implementation added and applied one forward-only
security migration, `20260912100000_restrict_v2_2_report_share_rotation.sql`. It only
revokes the unintended function grants; it does not change application rows. No existing
migration was rewritten, repaired or reapplied. All final parity references below are
therefore 20 migrations.

## Task 1: Lock the remote-validation safety contract

Frontend files:

- Create: `scripts/verify-v22-data-migration.mjs`
- Create: `scripts/v22-data-migration-safety.mjs`
- Create: `src/lib/database/v22ReleaseValidationContract.test.ts`
- Modify: `package.json`
- Modify: `.gitignore`

Steps:

1. Add failing tests for a fixed project-ref allowlist, read-only baseline mode,
   transaction-only remote mode, bounded output and forbidden SQL operations.
2. Make the runner accept only explicit modes: `baseline`, `local`, `remote` and
   `residue`. `remote` must require a typed confirmation containing the fixed project
   ref; other modes never mutate the linked project.
3. Resolve the linked project through the Supabase CLI and fail unless its ref, name and
   health match the approved target. Do not accept an arbitrary `--db-url`.
4. Parse `supabase migration list --linked` as data and compare the ordered local and
   remote versions. Fail on missing, duplicate, reordered or remote-only versions.
5. Capture only project metadata, migration versions, table names, exact count/digest
   assertions and pass/fail status. Apply the existing security scanner to saved
   diagnostics and delete successful raw output.
6. Reject a remote SQL file containing `COMMIT`, `DROP`, `TRUNCATE`, mutation of
   `supabase_migrations`, or unscoped data changes. Require exactly one top-level
   `BEGIN` and final `ROLLBACK`.
7. Add package scripts for the stable local and remote entrypoints. Remote validation is
   manual and must not run in GitHub Actions.
8. Run the focused test and the existing frontend database suite.
9. Commit as `test(v2.2): lock migration validation safety`.

Completion gate: the runner cannot issue a remote write until both the fixed-target and
transaction-safety contracts pass.

## Task 2: Add local and remote-safe pgTAP acceptance coverage

Frontend files:

- Create: `supabase/tests/database/v22_release_validation.test.sql`
- Create: `supabase/tests/database/v22_release_residue.test.sql`
- Modify: `supabase/tests/database/v22_schema.test.sql` only if an existing assertion is
  incomplete or stale.
- Modify: `src/lib/database/v22ReleaseValidationContract.test.ts`

Steps:

1. Start the acceptance file with `BEGIN`, enable pgTAP only inside that transaction and
   finish with `ROLLBACK`.
2. Assert all 20 final migration versions and the complete deployed object manifest.
3. Assert RLS on the 18 application tables currently protected by migrations. Verify
   `anon` and `authenticated` have no direct access to server-only tables and
   `service_role` has only the table/function operations the application calls.
4. Create two synthetic users and Cases under a unique release-validation marker.
   Exercise legal Case, connection, binding, snapshot, sync, entitlement, job, report
   and share paths.
5. Add negative assertions for cross-user and cross-Case access, duplicate active
   resources, invalid state transitions, immutable snapshots/reports and incorrect GBP
   raw-content retention. Require the expected SQLSTATE and safe constraint/function
   error identifier.
6. Insert a transaction-local legacy-shaped report with all V2.2 columns null and prove
   the deployed schema accepts it. Do not call or recreate a V2.1 application reader.
7. Insert a complete V2.2 report and prove the correct owner/Case relationship succeeds
   while the wrong owner or Case is rejected.
8. Make the residue file run in an independent session and assert zero rows contain the
   release-validation marker.
9. Run both files against a fresh local Supabase stack and keep the existing PGlite
   migration suite green.
10. Commit as `test(v2.2): add transactional migration acceptance`.

Completion gate: the SQL passes locally, the safety contract recognizes its rollback,
and interruption cannot leave committed test data.

## Task 3: Add the new-intake pause boundary

Frontend files:

- Create: `src/lib/release-v22/public-entry.ts`
- Create: `src/lib/release-v22/public-entry.test.ts`
- Create: `src/components/cases/intake-paused.tsx`
- Modify: `src/app/cases/new/page.tsx`
- Modify: `src/app/api/v2/cases/route.ts`
- Modify: `src/app/api/v2/preflight/route.ts`
- Modify: `src/app/api/v2/competitors/route.ts`
- Modify: `src/app/api/v2/analyze/route.ts`
- Modify: `src/app/api/v2/cases/[id]/checkout/route.ts`
- Modify: local E2E environment configuration and relevant handler/page tests.

Steps:

1. Add failing tests for `V22_PUBLIC_ENTRY_ENABLED`: exact `true` opens intake; missing,
   malformed or false values close intake outside the explicit loopback E2E fixture.
2. Implement one server-only parser and one stable 503 response contract. Never expose
   the environment value to the browser bundle.
3. When closed, render a clear paused state on `/cases/new` and reject new preflight,
   competitor discovery, Case creation, checkout creation and analysis submission.
4. Keep Case listing/reading, checkout status, task status/stream, connection management,
   report reading, PDF and share resolution available. Webhooks and internal callbacks
   remain available so already-started work can settle safely.
5. Add component and route tests proving the closed boundary and unaffected read paths.
6. Extend the deterministic Playwright fixture with a paused-intake journey. Keep all
   browser traffic local and stubbed.
7. Run typecheck, focused Vitest, database tests, production build, security scan and
   Playwright.
8. Commit as `feat(v2.2): add non-destructive intake pause`.

Completion gate: closing intake cannot hide or mutate existing persisted reports, and no
browser route can bypass the new-write boundary.

## Task 4: Add a dry-run-first operational control

Frontend files:

- Create: `scripts/control-v22-public-entry.mjs`
- Create: `src/lib/release-v22/publicEntryControlContract.test.ts`
- Create: `docs/operations/v22-public-entry-rollback.md`
- Modify: `package.json`

Steps:

1. Make dry-run the default and print only project/service names, intended boolean
   changes and required post-change health checks.
2. Require `--execute`, a direction (`close` or `open`) and exact confirmations for the
   Vercel project ID and Railway project/environment IDs before any mutation.
3. In close mode, set `V22_PUBLIC_ENTRY_ENABLED=false` in Vercel production and set
   Railway `V22_ANALYZE_ENABLED`, `V22_PREFLIGHT_ENABLED` and
   `V22_COMPETITOR_DISCOVERY_ENABLED` to false on the approved Web/Worker services where
   each variable applies.
4. Do not read or print other environment values. Never accept a Supabase credential or
   execute SQL.
5. Wait for gated deployments, verify the paused intake contract, confirm existing
   reports remain readable and require backend `/api/v1/health` HTTP 200.
6. Keep reopening as a distinct `open` operation with the same confirmations; do not
   reopen automatically after a failed close check.
7. Unit-test command construction with fake executables. Run only dry-run mode during
   V22-092 acceptance; do not cause a production outage to prove the control.
8. Commit as `ops(v2.2): add intake rollback control`.

Completion gate: tests prove the command cannot mutate by default or target an
unapproved project, and its code contains no database-deletion path.

## Task 5: Complete all local database and application gates

Frontend verification:

1. Confirm Docker is healthy and start only the repository's local Supabase stack.
2. Run `supabase db reset --local` from an empty local database.
3. Run `supabase db lint --local --schema public --level error --fail-on error`.
4. Run the local pgTAP schema, release-validation and residue files.
5. Run the PGlite migration suite and migration-safety contract twice, confirming stable
   collection and output.
6. Run `npm run quality`, including typecheck, Vitest, generated-contract checks,
   production build, security scan and deterministic Playwright.
7. Stop only the local Supabase stack created for this task. Confirm no generated schema,
   fixture or artifact diff remains.
8. Run the backend fast, Redis and release entrypoints without changing backend code.
9. Confirm both worktrees contain only the planned commits.

Completion gate: all local database, frontend and backend checks pass before a remote
transaction is attempted.

## Task 6: Run the controlled production database proof

External target:

- Supabase `searchtrust-production` (`cmdsbsvcxesnrrealftb`)

Steps:

1. Read project health and migration state again. Abort on any change from the approved
   target or any migration drift.
2. Run linked database lint at error severity.
3. Capture the count-only/digest before baseline. Do not download a data dump.
4. Run the release-validation pgTAP file through `supabase test db --linked` with the
   runner's exact typed confirmation.
5. Run the residue pgTAP file in a fresh linked session.
6. Capture the after baseline and prove exact equality with the before baseline.
7. Confirm migration history contains exactly the same 20 final ordered versions.
8. Read Supabase health/log summaries and record no migration or database error event.
9. Save only the redacted JSON result under the completion-report evidence boundary;
   delete raw successful command output.

Completion gate: all assertions pass, before/after evidence matches and there is no
synthetic residue. Apart from the explicitly approved forward security migration above,
do not run `db push`, `migration repair`, a reverse migration or an automatic schema
fix.

## Task 7: Configure and release the frontend gate

External configuration and frontend repository:

1. Add `V22_PUBLIC_ENTRY_ENABLED=true` to the Vercel production environment before the
   code containing the fail-closed guard can deploy. Do not add it to preview or
   development environments; their tests use explicit local fixtures.
2. Verify Vercel connected-Git deployment remains disabled and the existing GitHub
   quality/browser gate is the only production trigger.
3. Push the approved frontend commits to `main`.
4. While GitHub Actions is running, verify no production deployment starts.
5. Verify frontend quality, database and browser jobs pass and record totals/durations.
6. Verify the gated Vercel deployment is `Ready` at the exact successful commit and the
   production alias is healthy.
7. Confirm open intake works, existing report reads work and retired V2.1 paths remain
   404. Confirm no payment or analysis is submitted during smoke testing.
8. Run the operational control in production dry-run mode and record its bounded output.

Completion gate: the environment is explicitly open, deployment occurs only after CI,
and the tested rollback preview targets only the approved new-intake controls.

## Task 8: Close V22-092 with cross-repository evidence

Backend/central documentation files:

- Modify: `docs/superpowers/specs/2026-08-26-searchtrust-v2-2-development-plan.md`
- Create:
  `docs/superpowers/specs/2026-09-12-searchtrust-v2-2-data-migration-release-validation-completion.md`

Steps:

1. Record local Supabase, PGlite, pgTAP, frontend and backend test totals and durations.
2. Record the exact 20-version migration parity result, schema/RLS/service-role assertion
   totals, before/after equality and zero-residue proof.
3. Record the frontend GitHub Actions run, Vercel deployment, production smoke results
   and rollback dry-run evidence.
4. Record that existing report contents were never exported, no V2.1 path was restored
   and no production row or migration entry was changed.
5. Record the unchanged V22-093 boundary.
6. Mark V22-092 complete in the main development plan.
7. Commit the approved design, this plan and completion evidence as
   `docs(v2.2): complete data migration validation`.
8. Push the backend documentation commit and verify its GitHub gate, Railway Wait for CI,
   exact-commit Web/Worker deployments, health and clean logs.
9. Confirm both local heads equal `origin/main` and both worktrees are clean.

## Final acceptance gate

Do not mark V22-092 complete unless all of the following are simultaneously true:

- local and remote migration versions match exactly;
- local/native and linked transaction tests pass;
- remote schema, RLS and service-role contracts pass;
- before/after data evidence matches and synthetic residue is zero;
- no production report content, identity, token or payment detail entered logs/artifacts;
- no migration was reapplied or repaired and no existing row was mutated;
- V2.2 report persistence/reads pass and V2.1 remains retired;
- the pause boundary blocks every new intake path while preserving existing reads;
- rollback tooling is dry-run-first, fixed-target and contains no SQL path;
- frontend and backend quality gates are green;
- Vercel and Railway wait for CI and deploy exact successful commits;
- production health and initial logs are clean;
- the completion report contains reproducible redacted evidence;
- both repositories are clean and equal to `origin/main`.
