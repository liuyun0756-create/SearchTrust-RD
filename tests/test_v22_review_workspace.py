"""Workspace identity, safe projection and explicit confirmation. No provider I/O."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.review_freeze_models import ResolvedReview, SavedReviewEvidence, checked_selection
from app.report_v22.review_freeze_storage import ReviewFreezeRepository
from app.report_v22.review_workspace import ReviewWorkspace, fingerprint
from app.report_v22.review_workspace_models import command_adapter
from tests.test_v22_review_freeze import fixture


@pytest.fixture
def anyio_backend(): return "asyncio"


class Store:
    def __init__(self):
        b, s, raw, ids = fixture()
        self.saved = ReviewFreezeRepository.checked(canonical_json_bytes(raw), **ids)
        self.selection = checked_selection(s)
        self.resolved = ResolvedReview.model_validate_json(canonical_json_bytes(dict(bundle=b, case_updated_at="2026-12-16T08:00:00Z")))
        self.identity = dict(user_id=str(ids["user_id"]), case_id=str(ids["case_id"]), report_id=b["report"]["id"])
        self.exists = False
        self.resolves = self.writes = 0

    async def read(self, **kwargs): return self.saved if self.exists else None
    async def resolve(self, **kwargs):
        self.resolves += 1
        return self.resolved
    async def save(self, **kwargs):
        self.writes += 1
        self.exists = True
        return self.saved

    def command(self, operation, **kw):
        base = dict(operation=operation, **self.identity)
        if operation in ("preview", "save"): base["selection"] = self.selection.model_dump(mode="json")
        return command_adapter.validate_json(json.dumps(base | kw))


@pytest.mark.anyio
async def test_preview_save_and_recover_keep_private_evidence_internal():
    repo = Store(); service = ReviewWorkspace(repo)
    preview = await service.run(repo.command("preview"))
    assert preview.state == "preview" and preview.review_id is None
    raw = preview.model_dump_json()
    for forbidden in ('"user_id"', '"bundle"', '"normalized_payload"', '"implementation_entry"', '"lease_id"', '"connection_id"'):
        assert forbidden not in raw
    assert preview.actions[0].effect is None
    saved = await service.run(repo.command("save", review_id=str(repo.saved.id), confirmation_fingerprint=preview.confirmation_fingerprint))
    assert saved.state == "saved" and saved.confirmation_fingerprint is None
    assert repo.writes == 1
    # Lost acknowledgement: recovery does not resolve latest data or compare a new marker.
    repo.resolved = repo.resolved.model_copy(update={"case_updated_at":"2026-12-17T08:00:00Z"})
    assert await service.run(repo.command("save", review_id=str(repo.saved.id), confirmation_fingerprint=preview.confirmation_fingerprint)) == saved
    assert repo.writes == 1 and repo.resolves == 2


@pytest.mark.anyio
async def test_stale_confirmation_rejects_without_write():
    repo = Store(); service = ReviewWorkspace(repo)
    preview = await service.run(repo.command("preview"))
    repo.resolved = repo.resolved.model_copy(update={"case_updated_at":"2026-12-17T08:00:00Z"})
    with pytest.raises(DeterministicJobError, match="STALE"):
        await service.run(repo.command("save", review_id=str(repo.saved.id), confirmation_fingerprint=preview.confirmation_fingerprint))
    assert repo.writes == 0


def test_fingerprint_ignores_only_evaluation_instant():
    r = Store(); before = fingerprint(r.selection, r.resolved)
    copy = r.resolved.model_copy(deep=True)
    copy.bundle = copy.bundle.model_copy(update={"evaluated_at":"2026-12-16T08:00:30Z"})
    assert fingerprint(r.selection, copy) == before
    copy.bundle = copy.bundle.model_copy(update={"rules_version":"changed"})
    assert fingerprint(r.selection, copy) != before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["preview", "save", "read"])
async def test_route_report_is_checked_before_write(operation):
    r = Store(); r.exists = operation == "read"; r.identity["report_id"] = str(uuid4())
    kw = {} if operation == "preview" else dict(review_id=str(r.saved.id))
    if operation == "save": kw["confirmation_fingerprint"] = fingerprint(r.selection, r.resolved)
    with pytest.raises(DeterministicJobError, match="UNAVAILABLE"):
        await ReviewWorkspace(r).run(r.command(operation, **kw))
    assert r.writes == r.resolves == 0


@pytest.mark.anyio
async def test_same_request_different_selection_conflicts():
    r = Store(); r.exists = True
    changed = r.selection.model_dump(mode="json"); changed["actions"][0]["gsc_id"] = None
    with pytest.raises(DeterministicJobError, match="CONFLICT"):
        await ReviewWorkspace(r).run(r.command("save", selection=changed, review_id=str(r.saved.id), confirmation_fingerprint=fingerprint(r.selection,r.resolved)))
    assert r.writes == r.resolves == 0


@pytest.mark.parametrize("patch", [dict(operation="generate"), dict(raw_evidence={}), dict(decisions=[]), dict(report_id="bad"), dict(operation="read"), dict(operation="save",review_id=str(uuid4()))])
def test_commands_reject_unknown_mutations(patch):
    r = Store(); value = r.command("preview").model_dump(mode="json") | patch
    with pytest.raises(ValueError): command_adapter.validate_json(json.dumps(value))


@pytest.mark.parametrize("kind", ["deliveries", "implementations", "page", "ga4", "gsc"])
def test_history_cannot_be_used_as_arbitrary_directory(kind):
    r = Store()
    with pytest.raises(ValueError): r.command("history",kind=kind)


@pytest.mark.anyio
async def test_oversized_safe_view_rejected_before_write(monkeypatch):
    r=Store();service=ReviewWorkspace(r)
    preview=await service.run(r.command('preview'))
    monkeypatch.setattr('app.report_v22.review_workspace.MAX_VIEW_BYTES',1024)
    with pytest.raises(DeterministicJobError,match='TOO_LARGE'):
        await service.run(r.command('save',review_id=str(r.saved.id),confirmation_fingerprint=preview.confirmation_fingerprint))
    assert r.writes==0
