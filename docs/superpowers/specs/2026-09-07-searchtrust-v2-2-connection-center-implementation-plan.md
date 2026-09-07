# V22-063 Connection Center implementation plan

Design source: `2026-09-07-searchtrust-v2-2-connection-center-design.md`.
Production rollout keeps Google connection, sync, official GBP and verified-analysis
feature flags disabled. This milestone has no database migration.

## 1. Freeze the safe aggregate contract and pure gate projector

- Add `src/lib/connection-center/contracts.ts` in `search-trust` with the strict
  `connection_center_v1` browser response, fixed source/user/action enums and narrow
  binding, job and snapshot summaries.
- Add a pure projector that accepts already-owned Case, parent report, connections,
  active bindings, latest jobs and latest candidate snapshots.
- Encode deterministic state precedence, effective snapshot expiry, stale identity
  rejection, public GBP requirements, ordered blockers, the single next action,
  Verified Core and Full Evidence gates.
- Keep official GBP optional and force verified generation disabled until the later
  frontend and Railway M7 flags are both implemented.
- Build the state matrix with tests first, including a failed refresh with a retained
  healthy snapshot and every public GBP/GSC/GA4 blocking branch.

## 2. Implement the owned, coherent read boundary

- Add a repository using narrow Supabase selects for the active Case, latest eligible
  v2.2 parent report, live Google connections, active bindings, related latest jobs
  and latest snapshots.
- Freeze `client_cases.updated_at` plus binding IDs, recheck them after dependent reads,
  retry one concurrent change, then fail with `CONNECTION_CENTER_BUSY`.
- Reject snapshots that do not belong to the frozen Case, source and binding; do not
  return provider payloads, metrics, token fields, OAuth subjects, checksums or leases.
- Add a service and private GET handler with UUID validation, Clerk ownership, archived
  Case concealment, `cache-control: no-store`, fixed safe errors and the existing
  `GOOGLE_CONNECTIONS_ENABLED` gate.
- Mount `src/app/api/v2/cases/[id]/connection-center/route.ts` and cover disabled,
  unauthenticated, cross-user, invalid, concurrent-change and storage-failure paths.

## 3. Build the unified Connection Center UI

- Replace the current resource-selector-first page shell with a client Connection
  Center container that loads the aggregate response and polls only while a source job
  is queued or running.
- Add the approved Verified Core summary, ordered progress, one primary next action,
  three source cards and the folded official GBP Performance section.
- Show task-oriented status first and safe technical details on expansion; never flash
  unknown loading state as disconnected.
- Reuse existing account authorization, resource discovery, identity review, binding
  removal and source-specific sync mutations. Refresh the aggregate response after
  each successful mutation rather than duplicating product state in those controls.
- Provide the three CTA states: blocked, ready while M7 is unavailable, and a future
  enabled state behind `GOOGLE_VERIFIED_ANALYSIS_ENABLED`; this milestone must not
  submit a verified job.
- Preserve the approved desktop three-column hierarchy and source order, with a
  single-column mobile layout and text/icon status cues independent of color.

## 4. Verify integration and regression boundaries

- Add component tests for source order, all user statuses, expandable details, blocker
  ordering, progress, polling stop, retained snapshots, optional official GBP and the
  three generation CTA states.
- Keep existing Google connection, resource identity, GSC, GA4 and GBP sync tests
  passing without weakening their authorization, confirmation or idempotency rules.
- Run the complete frontend test suite, typecheck, production build and `git diff --check`.
- Run the complete backend suite because the shared V22 report gate is consumed by the
  Connection Center design, even when backend files do not change.
- Review the response and rendered HTML for tokens, raw provider payloads, private
  metrics, provider errors, stale binding state and unbounded text.

## 5. Document and release safely

- Update the main v2.2 plan and add a V22-063 completion record with exact test counts,
  remaining M7 dependency and no-migration statement.
- Commit and push both repositories only after all checks pass; do not alter unrelated
  user files or feature flags.
- Let the existing Git integration publish Vercel and any documentation-only Railway
  update, then verify exact commit status, `trysearchtrust.com`, API/queue health and
  recent error logs.
- Keep `GOOGLE_CONNECTIONS_ENABLED`, GSC/GA4/GBP sync flags and all verified-analysis
  flags absent or non-`true`; do not perform real OAuth, SerpAPI, Google sync or payment.

## Completion gate

V22-063 is complete when the coherent aggregate contract, deterministic gates, unified
page, tests, documentation and flag-off production release are complete. A ready gate
must not be represented as a generated Verified Client Action Plan until V22-070～074
and both verified-analysis enforcement flags are implemented.
