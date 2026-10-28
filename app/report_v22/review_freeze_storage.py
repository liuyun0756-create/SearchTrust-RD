"""Service-only review freeze. No browser, Google, job, model or billing entry."""
import json
from uuid import UUID

import httpx

from app.report_v22.review_plan_basis import PlanBasisReport
from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.models import utc_now
from app.report_v22.capability import instant
from app.report_v22.comparison_storage import ComparisonRepository
from app.report_v22.review_evidence_models import MAX_BYTES, ReviewEvidenceCore
from app.report_v22.review_freeze_models import (
    ResolvedReview, SavedReviewEvidence, assemble_bundle, checked_selection, invalid,
)
from app.report_v22.review_projection import build_review_projection, parse_review_bundle

MAX_RESPONSE_BYTES = 3 * MAX_BYTES
ERRORS = {"REVIEW_INVALID": "INVALID", "REVIEW_UNAVAILABLE": "UNAVAILABLE", "REVIEW_CONFLICT": "CONFLICT",
    "REVIEW_STALE": "STALE", "REVIEW_TOO_LARGE": "TOO_LARGE"}


def decode(raw):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate key")
            result[k] = v
        return result
    return json.loads(raw, object_pairs_hook=unique)


class ReviewFreezeRepository(ComparisonRepository):
    """Caller identity must come from trusted authentication, not request JSON."""

    def __init__(self, *, allow_multi_round=False, **kwargs):
        super().__init__(**kwargs)
        self.allow_multi_round=allow_multi_round

    def allow_bundle(self,bundle):
        if isinstance(bundle.report,PlanBasisReport) and not self.allow_multi_round:
            raise invalid()

    async def _rpc(self, name, args):
        try:
            async with self.client.stream("POST", f"{self.url}/rest/v1/rpc/{name}",
                    headers={"apikey": self.key, "authorization": f"Bearer {self.key}", "content-type": "application/json"},
                    content=canonical_json_bytes(args), follow_redirects=False, timeout=30) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise invalid()
                status = response.status_code
        except httpx.TransportError:
            raise TransientJobError("V22_REVIEW_STORAGE_UNAVAILABLE", "Review storage is temporarily unavailable.") from None
        if status == 429 or status >= 500:
            raise TransientJobError("V22_REVIEW_STORAGE_UNAVAILABLE", "Review storage is temporarily unavailable.")
        if status != 200:
            try:
                # Never echo the provider/SQL message. Only recognize the bounded first line.
                message = json.loads(raw).get("message", "").splitlines()[0]
                code = ERRORS.get(message, "UNAVAILABLE")
            except (ValueError, AttributeError, IndexError, TypeError):
                code = "UNAVAILABLE"
            raise DeterministicJobError(f"V22_REVIEW_{code}", "The review request is unavailable, invalid or has changed.")
        return bytes(raw)

    @staticmethod
    def checked(raw, *, user_id, case_id, review_id):
        try:
            value = decode(raw)
            if set(value) != {"schema_version", "id", "user_id", "case_id", "selection", "bundle_text", "frozen_at"}:
                raise ValueError("unexpected saved fields")
            # RPC transports the evidence as a JSON string to preserve legacy float lexemes through JSONB.
            body = value.pop("bundle_text")
            value["bundle"] = parse_review_bundle(body).model_dump(mode="json")
            saved = SavedReviewEvidence.model_validate_json(canonical_json_bytes(value))
            if (saved.user_id, saved.case_id, saved.id) != (UUID(str(user_id)), UUID(str(case_id)), UUID(str(review_id))):
                raise ValueError("saved identity mismatch")
            return saved
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, DeterministicJobError):
            raise invalid() from None

    async def read(self, *, user_id, case_id, review_id):
        raw = await self._rpc("get_v22_review_evidence", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(review_id)))
        if raw.strip()==b"null": return None
        saved=self.checked(raw,user_id=user_id,case_id=case_id,review_id=review_id)
        self.allow_bundle(saved.bundle)
        return saved

    async def resolve(self, *, user_id, case_id, selection):
        selection = checked_selection(selection)
        evaluated_at = utc_now()
        raw = await self._rpc("resolve_v22_review_evidence", dict(p_user_id=str(user_id), p_case_id=str(case_id),
            p_selection=selection.model_dump(mode="json"), p_evaluated_at=evaluated_at.isoformat()))
        try:
            value = decode(raw)
            if set(value) != {"core", "case_updated_at"}:
                raise ValueError("unexpected resolved fields")
            bundle = assemble_bundle(value["core"])
            self.allow_bundle(bundle)
            if (bundle.user_id, bundle.case_id) != (UUID(str(user_id)), UUID(str(case_id))) or instant(bundle.evaluated_at) != evaluated_at:
                raise ValueError("resolved identity or instant mismatch")
            selection.check_bundle(bundle)
            build_review_projection(bundle)
            return ResolvedReview.model_validate_json(canonical_json_bytes(dict(bundle=bundle.model_dump(mode="json"),
                case_updated_at=value["case_updated_at"])))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, DeterministicJobError):
            raise invalid() from None

    async def save(self, *, user_id, case_id, review_id, selection, resolved):
        selection = checked_selection(selection)
        try:
            resolved = ResolvedReview.model_validate_json(canonical_json_bytes(resolved))
            b = resolved.bundle
            self.allow_bundle(b)
            if (b.user_id, b.case_id) != (UUID(str(user_id)), UUID(str(case_id))):
                raise ValueError("resolved owner mismatch")
            selection.check_bundle(b)
            build_review_projection(b)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, DeterministicJobError):
            raise invalid() from None
        body = b.model_dump(mode="json")
        core = {k: v for k, v in body.items() if k in ReviewEvidenceCore.model_fields}
        raw = await self._rpc("freeze_v22_review_evidence", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(review_id),
            p_selection=selection.model_dump(mode="json"), p_case_updated_at=resolved.case_updated_at,
            p_bundle_text=canonical_json_bytes(body).decode(), p_canonical_core=verified_canonical_json_bytes(core).decode(),
            p_canonical_bundle=verified_canonical_json_bytes({k: v for k, v in body.items() if k != "checksum"}).decode()))
        saved = self.checked(raw, user_id=user_id, case_id=case_id, review_id=review_id)
        if saved.selection != selection:
            raise invalid()
        return saved

    async def freeze(self, *, user_id, case_id, review_id, selection):
        selection = checked_selection(selection)
        saved = await self.read(user_id=user_id, case_id=case_id, review_id=review_id)
        if saved:
            if saved.selection != selection:
                raise DeterministicJobError("V22_REVIEW_CONFLICT", "This request ID belongs to a different review selection.")
            return saved
        resolved = await self.resolve(user_id=user_id, case_id=case_id, selection=selection)
        return await self.save(user_id=user_id, case_id=case_id, review_id=review_id, selection=selection, resolved=resolved)
