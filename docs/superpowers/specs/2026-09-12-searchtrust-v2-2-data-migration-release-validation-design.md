# V22-092 Data migration release validation design

Date: 2026-09-12
Status: implemented and validated

## 1. Decision summary

V22-092 will validate the already-applied SearchTrust V2.2 schema against the existing
`searchtrust-production` Supabase project. There is no separate SearchTrust staging
project, and the owner has approved using the nearly empty production project as the
controlled validation target.

This milestone does not reapply migrations, backfill old reports, restore V2.1 product
code or delete existing data. It proves that the local and remote migration histories
match, that the deployed schema enforces its security and integrity contracts, and that
the application can safely stop new V2.2 intake without reversing schema changes.

## 2. Current state

- Supabase project: `searchtrust-production` (`cmdsbsvcxesnrrealftb`).
- The project is active and healthy in `us-east-2`.
- Nineteen local migrations, from `20260706000000` through `20260910100000`, have
  matching remote migration-history entries.
- The remote database contains only a small amount of test-era data. Initial table
  statistics estimate one user, one Case and eight orders; most V2.2 workflow tables are
  empty. These estimates must be replaced by exact count-only evidence during
  implementation without reading customer fields or payloads.
- The production frontend and backend are already V2.2-only. Executable V2.1 routes,
  adapters and report UI were retired in V22-091.

Implementation amendment (approved 2026-09-12): the first acceptance run found that
`anon` and `authenticated` still inherited `EXECUTE` on the V2.2 report-share rotation
function. The owner approved one forward-only security migration,
`20260912100000_restrict_v2_2_report_share_rotation.sql`. No recorded migration was
rewritten, repaired or reapplied. Final local/remote parity is therefore twenty ordered
migrations.

## 3. Goals

- Prove exact local-to-remote migration-history parity.
- Prove the remote V2.2 tables, columns, constraints, triggers, indexes, RLS state and
  service-role grants match the approved schema.
- Exercise legal and illegal data flows inside a transaction that always rolls back.
- Prove existing report rows are unchanged without reading or recording their contents.
- Prove a V2.2 Case-to-report read path works with correct user and Case ownership.
- Provide a dry-run-first operational control that closes only new V2.2 intake.
- Produce a redacted, reproducible completion record and leave both repositories clean.

## 4. Non-goals

- Creating a new Supabase staging project.
- Reapplying, repairing or rewriting an already-recorded migration.
- Restoring any V2.1 API, renderer, PDF, checkout or persistence adapter.
- Backfilling historical reports into Cases.
- Deleting existing users, orders, Cases, snapshots, reports or migration history.
- Dropping V2.2 tables, columns, functions, triggers, policies or indexes during rollback.
- Starting V22-093 internal or paid-user traffic.

## 5. Safety model

### 5.1 Target binding

Every remote command must resolve the linked project before it runs and must fail unless
the project ref is exactly `cmdsbsvcxesnrrealftb`. Remote validation must not accept an
arbitrary database URL on the command line, and logs must never print credentials,
connection strings, report payloads, user identifiers or provider data.

### 5.2 Read-only baseline

The baseline collector records only:

- project ref, region and health;
- ordered migration versions;
- table names and exact row counts;
- schema-object names, RLS booleans and privilege booleans;
- a keyed or one-way digest of the existing `reports` rows that cannot reveal their
  contents.

The before and after baseline must match exactly. If a report digest cannot be produced
without exposing data, the validator uses count plus server-side equality checks and
does not export any row content.

### 5.3 Transaction-only probes

All synthetic remote writes run in one explicit transaction and end with `ROLLBACK`.
The SQL acceptance file is rejected before execution if it contains `COMMIT`, `DROP`,
`TRUNCATE`, migration-history mutation, or an unscoped `DELETE` or `UPDATE`. Random UUIDs
namespace the synthetic records, but rollback remains the primary cleanup mechanism.

An interrupted session relies on PostgreSQL transaction rollback. The runner performs a
second exact count and residue check after disconnect before declaring success.

## 6. Components

### 6.1 Remote baseline collector

A repository script in `search-trust` invokes the authenticated Supabase CLI, verifies
the fixed project binding and returns a bounded JSON summary. It does not use browser
keys or application service-role secrets and does not retain a database dump.

### 6.2 Production acceptance SQL

A dedicated SQL file in `search-trust/supabase/tests/database` contains the remote-safe
transaction. It verifies:

- required V2.2 tables, columns, indexes, functions and triggers;
- RLS enabled on every V2.2 application table;
- no direct table privileges for `anon` or `authenticated`;
- only required table and function privileges for `service_role`;
- legal Case, binding, snapshot, job, entitlement, report and share operations;
- rejection of cross-user, cross-Case, duplicate, invalid-state and immutable-record
  mutations;
- GBP restricted-content retention and cleanup constraints;
- a legacy-shaped synthetic report can retain nullable V2.2 columns without restoring a
  V2.1 application reader;
- a complete V2.2 report can be read only through its correct owner and Case context.

Expected SQLSTATE values and constraint names are asserted. An unexpected success is a
test failure, not a warning.

### 6.3 Application report regression

The application regression uses existing repository adapters and synthetic local data
to prove V2.2 report persistence and owner-scoped reads. Against production it checks
only the before/after report count and server-side equality result. If the exact initial
report count is zero, the completion report states that no existing report row was
available and relies on the transaction-local legacy-shaped probe for schema
compatibility.

V2.1 routes and imports remain deletion invariants. No compatibility implementation is
added.

### 6.4 New-intake rollback control

The frontend gains a server-side `V22_PUBLIC_ENTRY_ENABLED` gate for new Case creation,
checkout and analysis submission. Existing Case and report reads remain available when
the gate is closed. Production is explicitly configured `true` only after validation;
missing or invalid values fail closed outside deterministic local tests.

The operational rollback command is dry-run by default. Its execute mode:

1. verifies the Vercel project and Railway production project/service IDs;
2. sets the frontend public-entry gate to false;
3. sets Railway `V22_ANALYZE_ENABLED`, `V22_PREFLIGHT_ENABLED` and
   `V22_COMPETITOR_DISCOVERY_ENABLED` to false for Web and Worker where applicable;
4. waits for gated deployments and confirms new intake is rejected while existing reads
   and `/api/v1/health` remain healthy.

It never runs SQL and never deletes persisted data. Reopening intake is a separate,
explicit operation after the incident is resolved.

## 7. Validation flow

1. Confirm both Git worktrees are clean and equal to `origin/main`.
2. Resolve and validate the linked Supabase project.
3. Capture the redacted before baseline.
4. Compare all twenty final local and remote migration versions in order.
5. Run the local migration, database, type, contract, build and browser gates.
6. Statistically inspect the remote SQL file for its transaction and forbidden-operation
   contract.
7. Execute the production acceptance transaction.
8. Capture the after baseline and prove exact equality plus zero synthetic residue.
9. Verify production frontend, backend health and logs remain healthy.
10. Record evidence, commit through the existing GitHub/Vercel and GitHub/Railway gates,
    and verify final deployment commits.

Any failure stops the sequence. The runner must not attempt automatic schema repair or
continue into the next phase.

## 8. Test layers

### 8.1 Static safety tests

- fixed project-ref allowlist;
- transaction begins and rolls back;
- forbidden SQL and unsafe parameter forms are rejected;
- output schema cannot contain secrets, row payloads or direct identifiers;
- rollback command defaults to dry-run and contains no database mutation path.

### 8.2 Local database tests

- apply all migrations in order to an empty PostgreSQL database;
- run existing PGlite and native Supabase/pgTAP coverage where the local stack is
  available;
- verify constraints, RLS declarations, grants and report compatibility;
- run TypeScript, Vitest, generated-contract, production-build, security and Playwright
  gates.

### 8.3 Remote transaction tests

- validate deployed schema objects and privileges;
- perform positive and negative data-flow probes;
- check exact expected SQLSTATE and named constraints;
- prove rollback and zero residue with an independent follow-up session;
- prove before/after report equality without exporting report content.

### 8.4 Cross-repository release tests

- the frontend GitHub gate and Vercel gated deployment must succeed;
- the backend 1,477-test gate must remain green if backend or central documentation
  changes;
- Railway Web and Worker must continue to wait for CI and deploy the exact successful
  commit;
- production health must return HTTP 200 and initial logs must contain no error-level
  application event.

## 9. Acceptance criteria

V22-092 is complete only when all of the following are true at once:

- twenty final local and remote migration versions match exactly in order;
- all required remote schema and permission assertions pass;
- the production acceptance transaction passes and leaves zero synthetic rows;
- exact before/after counts and report equality checks match;
- no existing row or migration-history entry is modified;
- V2.2 owner-scoped report persistence and reads pass;
- no executable V2.1 product path is restored;
- the new-intake rollback control is tested in dry-run and deterministic local modes;
- frontend and backend quality gates are green;
- Vercel and Railway deploy only their exact successful commits;
- production health and initial logs remain clean;
- the redacted completion report and runbook are committed;
- both worktrees are clean and equal to `origin/main`.

## 10. Unchanged release boundary

V22-093 remains separate. This milestone does not enroll internal accounts, Local SEO
consultants or paid validation users, and it does not make an expand-or-rollback traffic
decision. Successful V22-092 validation only establishes that the data layer and its
operational stop control are ready for that later staged release.
