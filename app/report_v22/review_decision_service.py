"""Explicit free decision revisions over a fixed saved evidence snapshot."""
from uuid import UUID

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.review_decision_models import (
    SaveDecision, DecisionRevision, DecisionHistory, MAX_INPUT, MAX_RECORD, MAX_HISTORY,
)
from app.report_v22.review_decision_validation import error, parse, normalize, validate_revision


class ReviewDecisionService:
    def __init__(self, repository, evidence_repository):
        self.repository = repository
        self.evidence_repository = evidence_repository

    async def basis(self, *, user_id, case_id, report_id, review_id):
        try:
            ids = {k: UUID(str(v)) for k, v in dict(user_id=user_id, case_id=case_id, report_id=report_id, review_id=review_id).items()}
        except (ValueError, TypeError, AttributeError):
            raise error() from None
        try:
            saved = await self.evidence_repository.read(user_id=ids["user_id"], case_id=ids["case_id"], review_id=ids["review_id"])
        except TransientJobError:
            raise TransientJobError("V22_REVIEW_DECISION_STORAGE_UNAVAILABLE", "Decision evidence could not be read.") from None
        except DeterministicJobError:
            raise error("UNAVAILABLE") from None
        if saved is None or saved.bundle.report.id != ids["report_id"]:
            raise error("UNAVAILABLE")
        return ids, saved

    @staticmethod
    def args(ids):
        return {"p_" + k: str(v) for k, v in ids.items()}

    def checked(self, raw, ids, saved):
        r = parse(DecisionRevision, raw, MAX_RECORD)
        return validate_revision(r, saved, **{k: v for k, v in ids.items() if k != "review_id"})

    async def save(self, *, user_id, case_id, report_id, request):
        request = parse(SaveDecision, request, MAX_INPUT)
        ids, saved = await self.basis(user_id=user_id, case_id=case_id, report_id=report_id, review_id=request.review_id)
        request = normalize(request, saved)
        # Database performs the idempotent read and first-write checks atomically.
        raw = await self.repository.rpc("save_v22_review_decision", self.args(ids) | {
            "p_request_text": canonical_json_bytes(request.model_dump(mode="json")).decode()})
        try:
            result = self.checked(raw, ids, saved)
            if result.request() != request:
                raise error()
        except DeterministicJobError:
            # A write may have committed. Never label an unverifiable receipt as
            # a rejected input: callers must recover the same request identity.
            raise TransientJobError("V22_REVIEW_DECISION_STORAGE_UNAVAILABLE", "The save result could not be verified.") from None
        return result

    async def read(self, *, user_id, case_id, report_id, review_id, decision_id):
        try:
            decision_id = UUID(str(decision_id))
        except (ValueError, TypeError, AttributeError):
            raise error() from None
        ids, saved = await self.basis(user_id=user_id, case_id=case_id, report_id=report_id, review_id=review_id)
        raw = await self.repository.rpc("get_v22_review_decision", self.args(ids) | {"p_id": str(decision_id)})
        result = self.checked(raw, ids, saved)
        if result.id != decision_id:
            raise error()
        return result

    async def history(self, *, user_id, case_id, report_id, review_id, cursor=None):
        if cursor is not None and (type(cursor) is not int or not 1 <= cursor < 2147483647):
            raise error()
        ids, _ = await self.basis(user_id=user_id, case_id=case_id, report_id=report_id, review_id=review_id)
        raw = await self.repository.rpc("list_v22_review_decisions", self.args(ids) | {"p_cursor": cursor})
        result = parse(DecisionHistory, raw, MAX_HISTORY)
        if (result.review_id, result.report_id) != (ids["review_id"], ids["report_id"]) or (cursor is not None and any(i.revision >= cursor for i in result.items)):
            raise error()
        return result
