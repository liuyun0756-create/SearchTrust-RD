# V22-074 Verified execution plan implementation plan

Design source: `2026-09-09-searchtrust-v2-2-execution-plan-design.md`.
This milestone changes only `SearchTrust-RD`. It adds a disabled internal capability
and does not add a database migration, public API, browser route, live provider call,
task orchestration, or feature-flag activation.

## 1. Freeze contracts, catalogs and identities

- Add strict execution limits, metric, gate, executable action, roadmap, audit, request
  and `execution_plan_result_v1` models.
- Add a versioned metric/copy catalog, stable audit identities and payload-free errors.
- Enforce exactly three actions and phases, one primary and at most one guardrail,
  action/phase/report consistency, canonical ordering and non-finite rejection.
- Write contract tests before implementing metric selection.

## 2. Bind and reproduce V22-072 and V22-073

- Validate Case, parent, verified report, evaluation/planning dates and all checksums.
- Rebuild V22-072 and V22-073 and byte-compare both supplied results.
- Build conflict-safe current Finding and Evidence indexes from validated upstreams.
- Reject re-signed action, Finding, Evidence, diff and ranking mutations.

## 3. Build deterministic action metrics and gates

- Select one target-bound primary metric per action from verified relations, falling
  back to the frozen public structural metric when no eligible numerical metric exists.
- Add only rule-required sample/denominator or GBP-band guardrails.
- Implement GSC, GA4, public GBP, official GBP-band, measurement-repair and
  measurement-consistency evaluators with fixed baseline/success text.
- Keep exact GBP Performance and keywords out of durable output.

## 4. Build strict 30/60/90 roadmap and action presentation

- Preserve V22-072 sequence and validated dependencies with one action per phase.
- Add measurement-repair gating to later actions and stable exit criteria.
- Adapt public and measurement actions into final TopAction contracts with deterministic
  why-now/client explanations and V22-074 metrics.
- Test ordinary, promoted-public, measurement-repair and consistency paths.

## 5. Assemble the final verified ReportV22

- Merge current Findings/Evidence, rebuild source coverage/performance envelopes, retain
  validated public context, and attach the exact V22-073 version diff.
- Bind executive decision, top actions, roadmap, client summary and limitations.
- Enforce Verified Core versus Full Evidence boundaries and complete reference closure.
- Prove the parent report remains byte-identical.

## 6. Add checkpoint, privacy and resource guards

- Add `execution_plan_checkpoint_v1` binding every version, ID, time, checksum and limit.
- Validate supplied checksums before lookup and persist only the final validated result.
- Test reuse, corruption, wrong job/input, version changes, all limits and forbidden
  GBP/raw/token/guarantee content.

## 7. Verify, document and release disabled capability

- Run V22-074 focused tests and ReportV22/V22-070/071/072/073 regressions.
- Run the complete backend suite and unchanged frontend contract, type, test and build
  checks.
- Update the main v2.2 plan and add an exact V22-074 completion record.
- Commit and push only after all checks pass; verify Railway API and Worker health while
  leaving sync and Verified Generation disabled or unreachable.

## Completion gate

V22-074 is complete only when upstream reproduction, target-bound metrics, rule-clearing
success criteria, necessary guardrails, measurement gating, deterministic 30/60/90
ordering, final verified ReportV22 assembly, privacy/resource guards, checkpointing,
full regressions and disabled Railway rollout all pass.
