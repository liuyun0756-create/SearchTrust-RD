"""Internal owner-scoped observation storage. No provider, queue or credit I/O."""
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import model_validator

from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.capability import ImplementationReference, Label
from app.report_v22.comparison import compare_implementation
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_models import ComparisonRequest, ComparisonResult, ComparisonScope
from app.report_v22.comparison_windows import WindowDays
from app.report_v22.first_party_findings_errors import FirstPartyFindingsError

MAX_RESPONSE_BYTES = 20_000_000


class ComparisonSelection(StrictModel):
    implementation: ImplementationReference
    action_id: Label
    days: WindowDays
    scope: ComparisonScope
    before_snapshot_id: UUID | None
    after_snapshot_id: UUID | None
    concurrent_changes: Literal["unknown", "reported", "none_reported"]

    def validate_request(self, request: ComparisonRequest, *, user_id: UUID, case_id: UUID):
        record = request.implementation
        if (record.user_id != user_id or record.case_id != case_id
                or self.implementation.model_dump() != dict(id=record.id, revision=record.revision, checksum=record.checksum)
                or request.action_id != self.action_id or request.days != self.days or request.scope != self.scope
                or request.concurrent_changes != self.concurrent_changes
                or (request.before.snapshot_id if request.before else None) != self.before_snapshot_id
                or (request.after.snapshot_id if request.after else None) != self.after_snapshot_id):
            raise ValueError("comparison selection binding mismatch")
        return request


class FrozenComparison(StrictModel):
    schema_version: Literal["v22_frozen_comparison_v1"]
    id: UUID
    user_id: UUID
    case_id: UUID
    selection: ComparisonSelection
    request: ComparisonRequest
    result: ComparisonResult

    @model_validator(mode="after")
    def replay(self):
        self.selection.validate_request(self.request, user_id=self.user_id, case_id=self.case_id)
        # Replay at the frozen evaluation instant, never today's time. Checking
        # hashes alone would not reject a re-signed but incorrect computation.
        if compare_implementation(self.request) != self.result:
            raise ValueError("stored comparison does not reproduce from its frozen input")
        return self


def invalid():
    return DeterministicJobError("V22_COMPARISON_INVALID", "Comparison storage returned inconsistent information.")


class ComparisonRepository:
    """Service-only adapter. The caller must supply its authenticated owner ID.

    The owner entry exposes projections only, never source bodies or paid review. Reuse the
    same comparison_id after an uncertain response; use a new ID for a new check.
    """
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not service_role_key):
            raise DeterministicJobError("V22_COMPARISON_NOT_CONFIGURED", "Comparison storage is not configured.")
        self.url, self.key, self.client = url.rstrip("/"), service_role_key, http_client

    async def _rpc(self, name: str, args: dict) -> bytes:
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
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise TransientJobError("V22_COMPARISON_UNAVAILABLE", "Comparison storage is temporarily unavailable.") from error
        if status == 429 or status >= 500:
            raise TransientJobError("V22_COMPARISON_UNAVAILABLE", "Comparison storage is temporarily unavailable.")
        if status != 200:
            raise DeterministicJobError("V22_COMPARISON_REJECTED", "The selected comparison is unavailable or its sources have changed.")
        return bytes(raw)

    async def read(self, *, user_id: UUID, case_id: UUID, comparison_id: UUID) -> FrozenComparison | None:
        raw = await self._rpc("get_v22_comparison", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(comparison_id)))
        if raw.strip() == b"null":
            return None
        return self._checked(raw, user_id=user_id, case_id=case_id, comparison_id=comparison_id)

    @staticmethod
    def _checked(raw: bytes, *, user_id: UUID, case_id: UUID, comparison_id: UUID) -> FrozenComparison:
        try:
            value = FrozenComparison.model_validate_json(raw)
            if (value.user_id, value.case_id, value.id) != (user_id, case_id, comparison_id):
                raise ValueError("storage identity mismatch")
            return value
        except (ValueError, TypeError, OverflowError, FirstPartyFindingsError):
            raise invalid() from None

    async def resolve(self, *, user_id: UUID, case_id: UUID, selection: ComparisonSelection) -> ComparisonRequest:
        selection = ComparisonSelection.model_validate(selection)
        evaluated = datetime.now(timezone.utc)
        raw = await self._rpc("resolve_v22_comparison_input", dict(p_user_id=str(user_id), p_case_id=str(case_id),
            p_selection=selection.model_dump(mode="json"), p_evaluated_at=evaluated.isoformat()))
        try:
            value = ComparisonRequest.model_validate_json(raw)
            selection.validate_request(value, user_id=user_id, case_id=case_id)
            if datetime.fromisoformat(value.evaluated_at.replace("Z", "+00:00")) != evaluated:
                raise ValueError("evaluation instant mismatch")
            # Validate source bodies before exposing a trusted internal input.
            compare_implementation(value)
            return value
        except (ValueError, TypeError, OverflowError, FirstPartyFindingsError):
            raise invalid() from None

    async def freeze(self, *, user_id: UUID, case_id: UUID, comparison_id: UUID,
                     selection: ComparisonSelection) -> FrozenComparison:
        selection = ComparisonSelection.model_validate(selection)
        saved = await self.read(user_id=user_id, case_id=case_id, comparison_id=comparison_id)
        if saved:
            if saved.selection != selection:
                raise DeterministicJobError("V22_COMPARISON_CONFLICT", "This comparison ID already belongs to a different selection.")
            return saved
        request = await self.resolve(user_id=user_id, case_id=case_id, selection=selection)
        result = compare_implementation(request)
        raw = await self._rpc("freeze_v22_comparison", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(comparison_id),
            p_selection=selection.model_dump(mode="json"), p_request=request.model_dump(mode="json"),
            p_canonical_request=verified_canonical_json_bytes(request.model_dump(mode="json")).decode(),
            p_result=result.model_dump(mode="json"),
            p_canonical_result=verified_canonical_json_bytes(result.model_dump(mode="json", exclude={"checksum"})).decode()))
        frozen = self._checked(raw, user_id=user_id, case_id=case_id, comparison_id=comparison_id)
        # Concurrent identical saves legitimately return the first instant/result.
        if frozen.selection != selection:
            raise invalid()
        return frozen
