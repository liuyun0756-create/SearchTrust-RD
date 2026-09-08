# V22-072 Verified reprioritization implementation plan

Design source: `2026-09-08-searchtrust-v2-2-verified-reprioritization-design.md`.
This milestone changes only `SearchTrust-RD`. It does not add a database migration,
browser route, final report assembly, version diff, roadmap, or live provider call.

## 1. Freeze contracts, identities and upstream binding

- Add strict input, qualified Finding reference, relation, ranked action, measurement
  action, selection audit, limits and `verified_reprioritization_v1` result models.
- Add payload-free deterministic failures and stable measurement-action identity.
- Bind Case, parent, evaluation time, planning date, ruleset/catalog versions and all
  upstream checksums; reject duplicate or conflicting qualified references.
- Write model tests for strict types, canonical order, exact three actions, core
  business Finding, reference closure, non-finite values and output bounds.

## 2. Recompute upstream results and rebuild public candidates

- Recompute public Findings from its input, public Actions with its original limits,
  V22-070, and reconstructed V22-071 before using any supplied result.
- Canonicalize only traversal-order-insensitive collections and byte-compare results.
- Rebuild every public candidate from the validated selection audit, public Findings
  and frozen action catalog without modifying the existing public action builder.
- Prove original action IDs, grouping, exact targets, templates and base priority are
  preserved, including candidates that were not in the original top three.

## 3. Build strict target mapping and relation classification

- Add the versioned rule allowlist for single-source support, growth reduction,
  cross-source support, measurement conflict and audit-only rules.
- Match page evidence only to the same V22-071-normalized URL; match queries only
  after NFKC/space/casefold normalization; map aggregate signals only to market
  visibility candidates.
- Protect HTTP/noindex and explicit identity blockers from growth reduction.
- Emit one complete deterministic relation or unmatched audit for every eligible new
  Finding, with no statement-text inference and no raw-metric scoring.
- Test exact/similar target isolation, unknown rules, duplicate evidence, source
  health gates, family caps and input-order independence.

## 4. Add deterministic ranking and measurement actions

- Compute capped ordinal verification levels, then preserve severity, confidence,
  classification, impact scope, template order, target and ID tie-breaks.
- Generate at most one forced `restore_verified_measurement` action for blocking
  GSC/GA4 issues; official GBP never forces it.
- Generate grouped `review_measurement_consistency` candidates for V22-071 conflicts,
  ranked below supported business work and above public-only work.
- Select exactly three actions, reapply selected-set dependencies, assign 30/60/90
  review dates, and select the highest business public anchor as the core problem.
- Test forced-first, ordinary conflict, growth demotion, hard-block protection,
  previously unselected promotion, dependency order and insufficient-action failure.

## 5. Add checkpoint execution and privacy/resource guards

- Validate all action, Finding, relation, evidence, audit and upstream references
  before constructing the result.
- Enforce candidate, Finding, relation, audit, issue-code and byte limits with no
  partial output.
- Add a versioned checkpoint whose digest binds versions and upstream checksums and
  whose stored value contains only the validated reprioritization result.
- Test cache reuse, semantic order stability, version changes, corruption, wrong
  job/input, retry safety and GBP exact value/keyword/raw Content absence.

## 6. Verify, document and release disabled capability

- Run focused V22-072 suites plus public Findings/Actions, V22-070 and V22-071
  regressions, then the complete backend suite.
- Run the unchanged frontend complete suite, TypeScript check, contract check and
  production build as a shared-contract regression.
- Update the main v2.2 plan and add a V22-072 completion record with exact counts.
- Commit and push only after all checks pass; verify Railway API and Worker success,
  health/logs, and keep Google sync plus Verified Generation absent or disabled.

## Completion gate

V22-072 is complete only when upstream recomputation, all-candidate recovery, strict
mapping, health-gated evidence levels, growth reduction, both measurement-action
boundaries, exact-three selection, core-problem binding, audit/reference closure,
privacy guards, caps, checkpointing, full regression and disabled Railway rollout
pass. It must not claim version diff, final copy, baselines or roadmap completion.
