"""Bounded storage contracts and controlled transport. No database or provider calls."""
from copy import deepcopy
import json
from uuid import UUID, uuid4

import httpx
import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.review_evidence_models import ReviewEvidenceCore
from app.report_v22.review_freeze_models import checked_selection, assemble_bundle, SavedReviewEvidence
from app.report_v22.review_freeze_storage import ReviewFreezeRepository
from tests.support.review_projection_fixtures import bundle_raw, add_decision


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fixture(sources=True):
    b = bundle_raw(sources=sources)
    s = dict(report=dict(id=b["report"]["id"], checksum=b["report_checksum"]),
        delivery=dict(id=b["delivery_id"], revision=b["delivery_revision"], checksum=b["delivery_checksum"]),
        implementation={k: b["implementation"][k] for k in ("id", "revision", "checksum")},
        actions=[dict(action_id=a["action_id"], page_current_id=a["page"]["current"]["id"] if a["page"] else None,
            page_previous_id=None, gsc_id=a["gsc"]["id"] if a["gsc"] else None,
            ga4_id=a["ga4"]["id"] if a["ga4"] else None) for a in b["selections"]])
    raw = dict(schema_version="v22_saved_review_evidence_v1", id=str(uuid4()), user_id=b["user_id"], case_id=b["case_id"],
        selection=s, bundle_text=canonical_json_bytes(b).decode(), frozen_at="2026-12-16T08:01:00Z")
    ids = dict(user_id=UUID(b["user_id"]), case_id=UUID(b["case_id"]), review_id=UUID(raw["id"]))
    return b, s, raw, ids


@pytest.mark.parametrize("sources", [True, False])
def test_saved_review_replays_sources_and_retains_legacy_hashes(sources):
    b, s, raw, ids = fixture(sources)
    saved = ReviewFreezeRepository.checked(canonical_json_bytes(raw), **ids)
    assert saved.bundle.model_dump(mode="json") == b
    checked_selection(s).check_bundle(saved.bundle)
    assert assemble_bundle({k: v for k, v in b.items() if k in ReviewEvidenceCore.model_fields}) == saved.bundle
    raw["selection"]["actions"][0]["action_id"] = "changed"
    assert saved.selection.actions[0].action_id != "changed"


@pytest.mark.parametrize("field", ["report", "delivery", "implementation"])
def test_selection_exact_versions(field):
    b, s, raw, ids = fixture()
    s[field]["checksum"] = "sha256:" + "0" * 64
    with pytest.raises(DeterministicJobError, match="INVALID"):
        ReviewFreezeRepository.checked(canonical_json_bytes(raw), **ids)


@pytest.mark.parametrize("change", ["extra", "decision", "previous_only", "duplicate", "too_many", "wrong_id", "bool_revision"])
def test_selection_rejects_ambiguous_or_browser_supplied_evidence(change):
    b, s, _, _ = fixture(False)
    if change == "extra": s["evidence"] = b
    if change == "decision": s["decisions"] = []
    if change == "previous_only": s["actions"][0]["page_previous_id"] = str(uuid4())
    if change == "duplicate": s["actions"].append(deepcopy(s["actions"][0]))
    if change == "too_many": s["actions"] *= 4
    if change == "wrong_id": s["report"]["id"] = "not-a-uuid"
    if change == "bool_revision": s["implementation"]["revision"] = True
    with pytest.raises(DeterministicJobError, match="INVALID"): checked_selection(s)


def test_selection_byte_limit():
    with pytest.raises(DeterministicJobError, match="TOO_LARGE"): checked_selection({"x":"a"*32769})


@pytest.mark.parametrize("change", ["owner", "case", "id", "decision", "checksum", "source", "duplicate", "time", "extra_bundle"])
def test_saved_review_revalidates_every_response(change):
    b, _, raw, ids = fixture()
    if change in {"owner", "case", "id"}: raw[{"owner":"user_id", "case":"case_id", "id":"id"}[change]] = str(uuid4())
    if change == "decision": raw["bundle_text"] = canonical_json_bytes(add_decision(b)).decode()
    if change == "checksum": b["checksum"] = "sha256:"+"0"*64; raw["bundle_text"] = canonical_json_bytes(b).decode()
    if change == "source": b["selections"][0]["gsc"]["request"]["before"]["normalized_payload"] = {}; raw["bundle_text"] = canonical_json_bytes(b).decode()
    if change == "duplicate": raw["bundle_text"] = raw["bundle_text"][:-1]+',"checksum":"forged"}'
    if change == "time": raw["frozen_at"] = "2026-01-01T00:00:00Z"
    if change == "extra_bundle": raw["bundle"] = b
    with pytest.raises(DeterministicJobError, match="INVALID"):
        ReviewFreezeRepository.checked(canonical_json_bytes(raw), **ids)


@pytest.mark.anyio
@pytest.mark.parametrize("status,message,code", [(400,"REVIEW_STALE","STALE"), (400,"REVIEW_CONFLICT","CONFLICT"),
    (400,"REVIEW_INVALID","INVALID"), (400,"REVIEW_TOO_LARGE","TOO_LARGE"), (400,"private SQL information","UNAVAILABLE"),
    (429,"private provider information","STORAGE_UNAVAILABLE"), (503,"private detail","STORAGE_UNAVAILABLE")])
async def test_storage_errors_are_bounded(status, message, code):
    def handler(req): return httpx.Response(status, json={"message": message})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo = ReviewFreezeRepository(url="https://local-test.invalid", service_role_key="test", http_client=client)
        with pytest.raises((DeterministicJobError, TransientJobError), match=code) as error:
            await repo.read(user_id=uuid4(), case_id=uuid4(), review_id=uuid4())
        assert "private" not in str(error.value) and "private" not in repr(vars(error.value))


@pytest.mark.anyio
async def test_saved_retry_does_not_resolve_or_write():
    b, s, raw, ids = fixture(); calls=[]
    def handler(req):
        calls.append(req.url.path)
        assert req.url.path.endswith("get_v22_review_evidence")
        return httpx.Response(200, content=canonical_json_bytes(raw))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo = ReviewFreezeRepository(url="https://local-test.invalid", service_role_key="test", http_client=client)
        assert (await repo.freeze(**ids, selection=checked_selection(s))).bundle.checksum == b["checksum"]
        s = deepcopy(s); s["actions"][0]["gsc_id"] = str(uuid4())
        with pytest.raises(DeterministicJobError, match="CONFLICT"): await repo.freeze(**ids, selection=s)
    assert len(calls) == 2


@pytest.mark.anyio
async def test_storage_refuses_oversized_response_and_private_timeout():
    for huge in (True, False):
        def handler(req):
            if huge: return httpx.Response(200, content=b"x"*6_000_001)
            raise httpx.ReadTimeout("private source", request=req)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            repo = ReviewFreezeRepository(url="https://local-test.invalid", service_role_key="test", http_client=client)
            with pytest.raises((DeterministicJobError, TransientJobError)) as error:
                await repo.read(user_id=uuid4(), case_id=uuid4(), review_id=uuid4())
            assert error.value.__cause__ is None
