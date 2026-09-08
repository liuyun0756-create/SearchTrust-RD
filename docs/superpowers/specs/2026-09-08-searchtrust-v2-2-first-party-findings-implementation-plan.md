# V22-070 First-party Findings implementation plan

Design source: `2026-09-08-searchtrust-v2-2-first-party-findings-design.md`.
Production rollout keeps Google connection, sync and verified-analysis feature flags
disabled. No live Google, SerpAPI, payment or verified-generation request is part of
this milestone.

## 1. Freeze trusted inputs and result contracts

- Add strict source-specific input, target, evaluation, source-assessment, limits and
  `first_party_findings_v1` result models in `SearchTrust-RD`.
- Keep the frozen public `Finding` and evidence models unchanged; reject duplicate
  source types, non-finite numbers, inconsistent dates, invalid checksums and inputs
  without GSC plus GA4.
- Add safe fixed errors and deterministic identity helpers for first-party rules.
- Write contract tests first, including exact three-state invariants and stable JSON.

## 2. Add the service-only snapshot resolution boundary

- Add one incremental `search-trust` migration with a `service_role`-only RPC that
  resolves a Case, prospect parent report and selected GSC/GA4/optional GBP snapshot
  IDs under row locks.
- Revalidate active Case, current active bindings, matched identity, source/schema,
  resource context, snapshot dates, expiry, checksum shape and GBP raw-content state.
- Return only the fields required by the Railway engine. Revoke execution from
  `public`, `anon` and `authenticated`; cover permissions and every stale/cross-Case
  path in the existing database semantic test suite.
- Add a bounded Railway HTTP repository that calls the RPC, strictly validates its
  response and maps transient storage failures separately from deterministic rejects.

## 3. Build source-specific evidence and rule engines

- Parse stored GSC and GA4 normalized payloads with their existing strict snapshot
  models and verify their checksum before evaluation.
- Reconstruct official GBP only in memory from still-valid raw Content, verify its
  checksum and manifest, and emit durable categorical evidence rather than exact
  Performance numbers or keyword text.
- Implement shared percentage/minimum-sample helpers and stable outcome construction.
- Implement GSC query/page opportunities, declines, positive growth and measurement
  coverage; GA4 engagement/conversion/change and configuration; GBP impression/action
  change, demand bands and profile/measurement configuration.
- Validate every evidence/comparator reference and prevent unhealthy sources from
  contributing business Findings.

## 4. Add deterministic ordering, limits and checkpoint execution

- Sort evaluations and Findings independently of provider/input row order; apply the
  approved per-source business/measurement caps only after complete ordering.
- Preserve evaluations for valid targets excluded by output caps with an explicit
  limit reason, without generating orphan Findings.
- Add a checkpointed stage whose digest includes stage/ruleset versions and the full
  trusted request; validate stored result version, checksum, model and byte limit on
  every read.
- Cover cache hit, version change, corruption, ID conflict, oversized input/result and
  retry-safe behavior.

## 5. Verify the milestone and document completion

- Run focused rule, contract, repository, checkpoint and database tests while building.
- Run the complete backend suite, complete frontend suite, frontend typecheck and
  production build, plus both repositories' diff checks.
- Review logs/errors/checkpoints for token, OAuth subject, provider body, GBP exact
  metrics and keyword leakage.
- Update the main v2.2 plan and add a V22-070 completion record with exact test counts,
  deployment state and the V22-071 boundary.
- Commit and push each repository only after its checks pass. Apply the database
  migration before Railway code deployment and leave all verified-analysis flags off.

## Completion gate

V22-070 is complete when trusted snapshot resolution, all three single-source rule
families, deterministic evidence/evaluations, checkpointing, automated tests,
documentation and flag-off production rollout pass. It must not claim that a Verified
Client Action Plan or any cross-source conclusion has been generated.
