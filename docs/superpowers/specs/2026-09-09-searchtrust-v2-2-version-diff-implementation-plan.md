# V22-073 Version diff implementation plan

Design source: `2026-09-09-searchtrust-v2-2-version-diff-design.md`.
This milestone changes only `SearchTrust-RD`. It adds a disabled, deterministic
internal stage and does not add a database migration, browser route, public API,
provider call, final verified report, baseline, roadmap, or client copy generation.

## 1. Freeze contracts, identities and safe failures

- Add strict limits, request, audit and `version_diff_result_v1` result models.
- Add payload-free deterministic error codes, complete parent-Finding fingerprints,
  stable audit identities and a versioned reason catalog.
- Enforce canonical ordering, unique qualified references, unchanged/new partitioning,
  reference closure, non-finite rejection and output bounds in the models.
- Write focused contract tests before implementing the builder.

## 2. Bind the immutable parent and reproduce V22-072

- Validate the parent prospect report, Case/report identity, initial diff and checksum.
- Prove parent Findings, Evidence and public Top Actions match the public inputs bound
  into V22-072.
- Validate both V22-072 checksums, rerun the stage and byte-compare the result.
- Reject re-signed parent or upstream mutations with deterministic safe errors.

## 3. Build current indexes and strict old-Finding ownership

- Merge public, V22-070 and V22-071 Findings and Evidence with conflict detection.
- Reconstruct public candidates from V22-072's validated public inputs.
- Map each V22-072 relation through its exact candidate targets to explicit parent
  Finding IDs without statement or semantic matching.
- Record unmatched, audit-only and measurement Findings without forcing old ownership.

## 4. Classify only visible changes

- Emit at most one old-Finding entry using `refined > reprioritized > confirmed`.
- Keep unchanged old Findings only in internal audit; never expose them as differences.
- Select direct or boundary-crossing decision Evidence for reprioritization, including
  the documented public-Evidence fallback for assessment-only measurement gates.
- Emit every unconsumed new Finding once as `new`; never emit `replaced` in v1.
- Apply stable ordering and fixed, non-causal reasons.

## 5. Add integrity guards and checkpoint execution

- Validate fingerprints, parent/current/evidence references, relation ownership,
  visible-change completeness and the consumed/new partition.
- Enforce all collection, per-entry and serialized-byte limits with no partial output.
- Add `version_diff_checkpoint_v1`, binding every version, input checksum and limit,
  and persist only the validated result.
- Test cache reuse, corruption, wrong input/job binding and privacy exclusions.

## 6. Verify, document and release disabled capability

- Run focused V22-073 suites and public report/V22-070/071/072 regressions.
- Run the complete backend suite and unchanged frontend contract, type, test and build
  checks.
- Update the main v2.2 plan and add a V22-073 completion record with exact results.
- Commit and push only after all checks pass; then verify Railway API and Worker while
  keeping Google sync and Verified Generation absent or disabled.

## Completion gate

V22-073 is complete only when parent immutability, V22-072 reproduction, exact target
ownership, changed-only classification, evidence honesty, new-Finding partitioning,
deterministic ordering, privacy/resource guards, checkpointing, full regressions and
disabled Railway rollout all pass. It must not claim V22-074 final plan completion.
