"""Frozen review + exact later decision; no real accounts or external calls."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

import fakeredis.aioredis
import pytest

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_generation_stage import CheckpointedReviewGenerationStage
from app.report_v22.review_decision_models import DecisionRevision
from app.report_v22.review_generation_content import build_review_generation_content, validate_review_generation_content
from app.report_v22.review_generation_diff import compare_review_generation_inputs
from app.report_v22.review_generation_inputs import ReviewGenerationPreparer
from app.report_v22.review_generation_models import (
    MAX_INPUT, MAX_REFERENCE, RULES, ReviewGenerationInput, ReviewGenerationRecipe,
    ReviewGenerationSelection, checked, recipe_for,
)
from tests.test_v22_review_decisions import example, item, request, receipt, zero_evidence, evidence_for
from tests.test_v22_review_projection_boundaries import multi_bundle


@pytest.fixture
def anyio_backend():
    return "asyncio"


def resign(raw):
    raw = deepcopy(raw)
    raw["checksum"] = verified_request_digest({k: v for k, v in raw.items() if k != "checksum"})
    return raw


def generation(*, saved=None, decisions=None, revision=None):
    saved = saved or example()[0]
    # Stable IDs allow cross-repository fixture drift checks.
    saved = saved.model_copy(update={"id": UUID("71000000-0000-4000-8000-000000000001")})
    revision = revision or receipt(saved, request(saved, decisions,
        id="71000000-0000-4000-8000-000000000002"))
    raw = dict(schema_version="v22_review_generation_input_v1", rules_version=RULES,
        job_id="71000000-0000-4000-8000-000000000003", user_id=str(saved.user_id), case_id=str(saved.case_id),
        report_id=str(saved.bundle.report.id), prepared_at="2026-12-18T08:00:00Z",
        evidence=saved.model_dump(mode="json"), decision=revision)
    return checked(ReviewGenerationInput, resign(raw), MAX_INPUT)


def decision_change(value, *, kind="adjust", reason=None, new=False):
    saved = value.evidence
    d = item(saved, "new" if new else kind, "new-work" if new else None)
    if reason:
        d["reason"] = reason
    req = request(saved, [d], previous_id=str(value.decision.id), previous_checksum=value.decision.checksum)
    revision = receipt(saved, req, revision=value.decision.revision + 1, recorded_at="2026-12-18T08:00:00Z")
    raw = value.model_dump(mode="json") | dict(job_id=str(uuid4()), decision=revision)
    return checked(ReviewGenerationInput, resign(raw), MAX_INPUT)


def test_content_preserves_frozen_evidence_and_baseline():
    value = generation(); original = value.model_dump_json()
    content = build_review_generation_content(value)
    assert value.model_dump_json() == original
    assert value.evidence.bundle.decisions == []
    assert content.report_type == "verified_execution"
    assert content.proposed_version == value.evidence.bundle.report.version + 1
    assert content.delivery_state == "requires_delivery_review"
    assert content.seo_actions[0].selected_decision.disposition == "continue"
    assert content.client_actions[0].facts.gsc.metrics
    assert content.client_actions[0].facts.ga4.state == "available"
    assert content.client_actions[0].facts.page.comparison == "first_observation"
    assert content.effect is None and content.causal_attribution is False
    assert compare_review_generation_inputs(value).state == "first_baseline"
    assert validate_review_generation_content(content, value) == content


@pytest.mark.parametrize("kind", ["continue", "adjust", "observe", "defer", "new"])
def test_private_decisions_do_not_become_client_copy(kind):
    saved = example()[0]; d = item(saved, kind, "new-work" if kind == "new" else None)
    d["reason"] = "PRIVATE_REASON_ABC"
    for key in ("target", "work", "completion_statement", "reconsider_when"):
        if d[key] is not None:
            d[key] = "PRIVATE_" + key.upper()
    value = generation(saved=saved, decisions=[d]); content = build_review_generation_content(value)
    client = json.dumps(content.model_dump(mode="json", include={"client_actions", "client_new_decisions"}))
    assert "PRIVATE_" not in client
    assert "PRIVATE_REASON_ABC" in content.model_dump_json()
    assert all(not a.next_decision.system_completion_verified for a in content.client_actions)
    assert all(not a.facts.recorded_work.system_completion_verified for a in content.client_actions)
    if kind == "new":
        assert content.client_actions[0].next_decision.decision_state == "needs_review"
        assert content.client_new_decisions[0].disposition == "new"


def test_empty_decisions_do_not_implicitly_continue():
    content = build_review_generation_content(generation(decisions=[]))
    assert content.seo_actions[0].selected_decision is None
    assert content.client_actions[0].next_decision.disposition is None
    assert content.prior_diagnosis.findings


def test_zero_actions_preserves_diagnosis_and_three_actions_keep_approved_order():
    content = build_review_generation_content(generation(saved=zero_evidence(), decisions=[]))
    assert content.prior_diagnosis.action_state == "insufficient_basis"
    assert content.prior_diagnosis.findings and content.client_actions == []
    saved = evidence_for(multi_bundle())
    decisions = [item(saved, "defer", a.id) | dict(references=[]) for a in reversed(saved.bundle.delivery.actions)]
    decisions += [item(saved, "new", "new-2"), item(saved, "new", "new-1")]
    content = build_review_generation_content(generation(saved=saved, decisions=decisions))
    assert [a.facts.action_id for a in content.seo_actions] == ["ac-1", "ac-2", "ac-3"]
    assert [d.action_id for d in content.seo_new_decisions] == ["new-2", "new-1"]


def test_fact_diff_is_separate_and_absent_sources_do_not_create_metrics():
    from app.report_v22.review_freeze_models import SavedReviewEvidence
    from tests.support.review_projection_fixtures import bundle_raw
    from tests.test_v22_review_freeze import fixture
    from app.report_v22.review_freeze_storage import ReviewFreezeRepository
    first = generation()
    raw = first.evidence.model_dump(mode="json")
    raw["bundle"] = bundle_raw(after=150)
    saved = SavedReviewEvidence.model_validate_json(canonical_json_bytes(raw))
    changed = generation(saved=saved)
    diff = compare_review_generation_inputs(changed, first)
    assert diff.actions[0].changed_sections == ["ga4"]
    assert diff.actions[0].evidence_changed and not diff.actions[0].decision_changed
    _, _, raw, ids = fixture(False)
    saved = ReviewFreezeRepository.checked(canonical_json_bytes(raw), **ids)
    content = build_review_generation_content(generation(saved=saved))
    facts = content.client_actions[0].facts
    assert facts.page is None and facts.gsc is None and facts.ga4 is None
    assert "NO_SELECTED_METRICS" in [check.code for check in facts.checks]


def test_different_delivery_basis_is_not_a_progress_comparison():
    first = generation()
    # This valid fixture has a different approved action set/report checksum.
    other = generation(saved=evidence_for(multi_bundle()), decisions=[])
    assert compare_review_generation_inputs(other, first).state == "not_comparable"


@pytest.mark.parametrize("field", ["user_id", "case_id", "report_id", "job_id", "time", "decision", "bundle", "hash", "extra", "boolean"])
def test_reject_mutated_inputs_even_with_recomputed_outer_hash(field):
    raw = generation().model_dump(mode="json")
    if field in {"user_id", "case_id", "report_id"}:
        raw[field] = str(uuid4())
    elif field == "job_id": raw[field] = raw["report_id"]
    elif field == "time": raw["prepared_at"] = "2026-12-16T08:01:00Z"
    elif field == "decision": raw["decision"]["decisions"][0]["reason"] = "changed"
    elif field == "bundle": raw["evidence"]["bundle"]["evaluated_at"] = "2026-12-17T08:00:00Z"
    elif field == "extra": raw["charge"] = True
    elif field == "boolean": raw["decision"]["revision"] = True
    raw = resign(raw)
    if field == "hash": raw["checksum"] = "sha256:" + "0" * 64
    with pytest.raises(DeterministicJobError): checked(ReviewGenerationInput, raw, MAX_INPUT)


def test_duplicate_nonfinite_oversize_and_mutated_models():
    value = generation(); raw = value.model_dump_json()
    for bad in (raw[:-1]+',"checksum":"duplicate"}', raw.replace('"completion_statement":null', '"completion_statement":NaN')):
        with pytest.raises(DeterministicJobError): checked(ReviewGenerationInput, bad, MAX_INPUT)
    with pytest.raises(DeterministicJobError, match="TOO_LARGE"):
        checked(ReviewGenerationInput, b" " * (MAX_INPUT + 1), MAX_INPUT)
    value.decision.decisions[0].reason = "mutated instance"
    with pytest.raises(DeterministicJobError): build_review_generation_content(value)


def test_semantic_diff_ignores_revision_identity_but_not_work():
    first = generation()
    repeated = decision_change(first, kind="continue")
    diff = compare_review_generation_inputs(repeated, first)
    assert diff.state == "compared" and diff.actions[0].changed_sections == []
    changed = decision_change(first)
    diff = compare_review_generation_inputs(changed, first)
    assert diff.actions[0].changed_sections == ["decision"]
    assert diff.actions[0].decision_changed and not diff.actions[0].evidence_changed
    assert compare_review_generation_inputs(decision_change(first, new=True), first).new_decisions_changed
    changed_again = decision_change(changed, reason="Different reason")
    assert compare_review_generation_inputs(changed_again, changed).actions[0].decision_changed


def test_reference_only_changes_do_not_mark_work_changed():
    first = generation()
    raw = decision_change(first, kind="continue").model_dump(mode="json")
    raw["decision"]["decisions"][0]["references"] = []
    request_fields = {k: raw["decision"][k] for k in ("id", "review_id", "previous_id", "previous_checksum", "decisions")}
    raw["decision"]["request_checksum"] = verified_request_digest(request_fields)
    raw["decision"] = resign(raw["decision"])
    current = checked(ReviewGenerationInput, resign(raw), MAX_INPUT)
    assert compare_review_generation_inputs(current, first).actions[0].changed_sections == []


class MemoryDecisions:
    def __init__(self, value):
        self.value = value; self.read_ids = []; self.allow = True

    async def basis(self, **kwargs):
        if not self.allow:
            raise DeterministicJobError("UNAVAILABLE", "Unavailable")
        return kwargs, self.value.evidence

    async def read(self, **kwargs):
        await self.basis(**kwargs)
        self.read_ids.append(kwargs["decision_id"])
        return self.value.decision


@pytest.mark.anyio
async def test_preparation_exact_selection_recipe_replay_and_permission_recheck():
    value = generation(); service = MemoryDecisions(value)
    preparer = ReviewGenerationPreparer(service, clock=lambda: datetime(2026, 12, 18, 8, tzinfo=timezone.utc))
    recipe = recipe_for(value)
    selection = recipe.model_dump(mode="json", include=set(ReviewGenerationSelection.model_fields))
    owner = dict(user_id=value.user_id, case_id=value.case_id, report_id=value.report_id)
    prepared = await preparer.prepare(**owner, selection=selection)
    assert prepared == value
    assert await preparer.resolve(**owner, recipe=recipe) == value
    assert service.read_ids == [value.decision.id] * 2
    assert "evidence" not in recipe.model_dump_json() and "reason" not in recipe.model_dump_json()
    bad = selection | dict(decision_checksum="sha256:"+"0"*64)
    with pytest.raises(DeterministicJobError): await preparer.prepare(**owner, selection=bad)
    with pytest.raises(DeterministicJobError): await preparer.resolve(**(owner | dict(user_id=uuid4())), recipe=recipe)
    service.allow = False
    with pytest.raises(DeterministicJobError): await preparer.resolve(**owner, recipe=recipe)


@pytest.mark.anyio
async def test_preparation_revalidates_bad_storage_receipts_and_recipe_checksum():
    value = generation(); service = MemoryDecisions(value)
    preparer = ReviewGenerationPreparer(service)
    owner = dict(user_id=value.user_id, case_id=value.case_id, report_id=value.report_id)
    recipe = recipe_for(value).model_dump(mode="json")
    with pytest.raises(DeterministicJobError):
        await preparer.resolve(**owner, recipe=recipe | dict(input_checksum="sha256:"+"0"*64))
    service.value.decision.decisions[0].reason = "mutated stored receipt"
    with pytest.raises(DeterministicJobError): await preparer.resolve(**owner, recipe=recipe)


@pytest.mark.anyio
async def test_checkpoint_replay_without_generation_and_wrong_job(monkeypatch):
    import app.jobs_v22.review_generation_stage as module
    value = generation(); redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="review-generation", ttl_seconds=600)
    stage = CheckpointedReviewGenerationStage()
    try:
        with pytest.raises(DeterministicJobError): await stage.build(job_id=uuid4(), value=value, checkpoints=checkpoints)
        first = await stage.build(job_id=value.job_id, value=value, checkpoints=checkpoints)
        monkeypatch.setattr(module, "build_review_generation_content", lambda *a: pytest.fail("must replay saved content"))
        assert await stage.build(job_id=value.job_id, value=value, checkpoints=checkpoints) == first
    finally:
        await redis.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["job", "input", "checksum", "extra", "resigned_claim", "resigned_copy", "json", "duplicate", "oversize", "nonfinite"])
async def test_checkpoint_corruption_and_resigned_semantic_forgery(change):
    value = generation(); redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="review-generation", ttl_seconds=600)
    stage = CheckpointedReviewGenerationStage()
    try:
        await stage.build(job_id=value.job_id, value=value, checkpoints=checkpoints)
        keys = [key async for key in redis.scan_iter(match="*:checkpoint:*")]
        assert len(keys) == 1
        raw = json.loads(await redis.get(keys[0]))
        if change == "job": raw["job_id"] = str(uuid4())
        elif change == "input": raw["input_checksum"] = "sha256:" + "0" * 64
        elif change == "checksum": raw["result_checksum"] = "sha256:" + "0" * 64
        elif change == "extra": raw["extra"] = True
        elif change.startswith("resigned"):
            if change == "resigned_claim": raw["result"]["client_actions"][0]["next_decision"]["text"] = "This work caused more leads."
            else: raw["result"]["client_actions"][0]["facts"]["title"] = "PRIVATE_NOTE_LEAK"
            raw["result"] = resign(raw["result"])
            raw["result_checksum"] = verified_request_digest(raw["result"])
        encoded = canonical_json_bytes(raw)
        if change == "json": encoded = b"invalid json"
        elif change == "duplicate": encoded = encoded[:-1] + b',"result_checksum":"duplicate"}'
        elif change == "oversize": encoded = b" " * 1_100_001
        elif change == "nonfinite": encoded = encoded.replace(b'"effect":null', b'"effect":NaN')
        await redis.set(keys[0], encoded)
        with pytest.raises(DeterministicJobError):
            await stage.build(job_id=value.job_id, value=value, checkpoints=checkpoints)
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_superseded_run_cannot_claim_unsaved_checkpoint():
    value = generation(); redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    checkpoints = JobCheckpoints(redis, prefix="review-generation", ttl_seconds=600, run_generation=2)
    try:
        await redis.set(checkpoints.keys.state(value.job_id), canonical_json_bytes(dict(run_generation=3, status="running")))
        with pytest.raises(DeterministicJobError, match="CHECKPOINT_INVALID"):
            await CheckpointedReviewGenerationStage().build(job_id=value.job_id, value=value, checkpoints=checkpoints)
        assert [key async for key in redis.scan_iter(match="*:checkpoint:*")] == []
    finally:
        await redis.aclose()


@pytest.mark.anyio
async def test_ordinary_paid_input_resolver_still_rejects_review_mode():
    from app.report_v22.capability_examples import example_report
    from app.report_v22.capability import CapabilityReport
    from app.jobs_v22.capability_runtime_models import CapabilityTaskRequest
    from tests.test_v22_capability_input_repository import resolve
    report = CapabilityReport.model_validate_json(json.dumps(example_report("review", 1)))
    m = report.input_manifest
    task = CapabilityTaskRequest(case_id=m.case_id, manifest_id=m.id, input_checksum=m.input_checksum, report_type=report.report_type)
    raw = dict(schema_version="v22_capability_resolved_input_v1", manifest=m.model_dump(mode="json"),
        run_generation=1, request=None, previous_report=None, completed_report=report.model_dump(mode="json"))
    with pytest.raises(TransientJobError, match="V22_CAPABILITY_INPUT_INVALID_RESPONSE"):
        await resolve(raw, task, m.job_id)


@pytest.mark.anyio
async def test_ordinary_completion_cannot_bill_review_or_internal_candidate():
    from app.report_v22.capability_examples import example_report
    from app.report_v22.capability import CapabilityReport
    from tests.test_v22_capability_result_persistence import persist
    report = CapabilityReport.model_validate_json(json.dumps(example_report("review", 1)))
    m = report.input_manifest
    params = dict(user_id=m.user_id, case_id=m.case_id, job_id=m.job_id, run_generation=1, manifest=m, report=report)
    def unexpected(_): pytest.fail("Review must not reach billing/completion")
    with pytest.raises(DeterministicJobError): await persist(None, params=params, handler=unexpected)
    params["report"] = build_review_generation_content(generation())
    with pytest.raises(DeterministicJobError): await persist(None, params=params, handler=unexpected)
