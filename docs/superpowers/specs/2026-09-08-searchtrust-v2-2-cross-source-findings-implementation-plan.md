# V22-071 Cross-source Findings implementation plan

Design source: `2026-09-08-searchtrust-v2-2-cross-source-findings-design.md`.
This milestone changes only `SearchTrust-RD`. It does not add a browser route,
database migration, final Verified report generation, or any live provider call.

## 1. Freeze contracts, identities and errors

- Add strict cross-source input, target, evaluation, pair-assessment, limits and
  `cross_source_findings_v1` result models.
- Require canonical source pairs and three-state/Finding one-to-one invariants.
- Add payload-free deterministic errors and prose-independent Finding identity.
- Write contract tests for source-pair order, input/result binding, duplicate IDs,
  unknown references, non-finite values and stable JSON.

## 2. Build page and time normalization primitives

- Add Case-host-scoped GSC URL and GA4 landing-path normalization with the exact
  tracking-parameter, percent-encoding, query sorting and path rules from the design.
- Add deterministic same-source consolidation that retains every constituent row
  reference and rejects ambiguous normalized identities.
- Add 90-day window compatibility, common complete ISO week construction, GA4
  two-day lag exclusion, average-rank Spearman and constant/incomplete states.
- Cover every accepted/rejected URL form, three/four-day window boundary, seven/eight
  week boundary, ties, constants and ±0.70 thresholds.

## 3. Build cross-source evidence and GSC↔GA4 rules

- Recompute and byte-compare V22-070 from the supplied trusted input before reading
  source data, then construct a bounded cross-source Evidence ledger.
- Implement matched-page growth, decline, confirmed search opportunity and direction
  conflict with inherited sample gates and exact two-source references.
- Implement aggregate current/prior alignment and weekly co-movement/conflict.
- Emit fixed causal-boundary limitations and fixed impact tiers; never use LLM copy.
- Test all threshold edges, missing/truncated rows, restrictions, third-source gaps,
  order independence and source isolation.

## 4. Add optional official GBP pair rules and privacy guards

- Reconstruct official GBP only in memory through the V22-070 decoder.
- Implement GSC↔GBP visibility and GA4↔GBP behavior alignment/conflict.
- Store only categorical GBP evidence and fixed impact tiers; reject expired/unhealthy
  GBP and never accept public SerpAPI GBP as official Performance.
- Add serialized-result/checkpoint/log/error scans proving exact GBP metrics, ratios,
  keyword text and raw Content are absent.

## 5. Add orchestration, limits and checkpoint execution

- Validate all Evidence/Trace/Finding/evaluation relationships and exact pair source
  sets before result construction.
- Apply independent deterministic ordering and the approved page, aggregate,
  measurement, total, evaluation, Evidence and byte caps.
- Preserve capped evaluations as `not_checked/output_limit` without orphan Findings.
- Add a versioned checkpoint stage whose digest binds the V22-070 input/result and
  whose stored value contains only the validated cross-source result.
- Cover cache hits, version changes, corruption, wrong job/input, retry-safe execution
  and resource limit failures.

## 6. Verify, document and release disabled capability

- Run focused contracts/page/time/rule/privacy/checkpoint suites and related V22-070
  regressions, followed by the complete backend suite.
- Run the unchanged frontend complete suite, TypeScript check, contract check and
  production build as a shared-contract regression.
- Update the main v2.2 plan and add a V22-071 completion record with exact counts.
- Commit and push only after all checks pass; wait for Railway API and production
  Worker success, verify health/logs, and keep Google sync plus Verified Generation
  absent or disabled.

## Completion gate

V22-071 is complete only when deterministic pair qualification, page normalization,
window/week alignment, all approved pair rules, exact two-source Evidence, privacy
guards, output caps, checkpointing, automated regression, documentation and disabled
Railway rollout pass. It must not claim reprioritization, version diff or a final
Verified Client Action Plan.
