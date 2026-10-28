"""Service-only bounded RPC transport. No route, provider, queue or billing."""
import httpx

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.comparison_storage import ComparisonRepository
from app.report_v22.review_decision_models import MAX_RECORD, MAX_HISTORY
from app.report_v22.review_decision_validation import error
from app.report_v22.review_freeze_storage import decode

ERRORS = {f"REVIEW_DECISION_{c}": c for c in ("INVALID", "UNAVAILABLE", "CONFLICT", "STALE", "TOO_LARGE")}


class ReviewDecisionRepository(ComparisonRepository):
    def __init__(self, **kwargs):
        try:
            super().__init__(**kwargs)
        except DeterministicJobError:
            raise error("UNAVAILABLE") from None

    async def rpc(self, name, args):
        if name not in {"save_v22_review_decision", "get_v22_review_decision", "list_v22_review_decisions"}:
            raise error()
        limit = MAX_HISTORY if name == "list_v22_review_decisions" else MAX_RECORD
        try:
            async with self.client.stream("POST", f"{self.url}/rest/v1/rpc/{name}",
                    headers={"apikey": self.key, "authorization": f"Bearer {self.key}", "content-type": "application/json"},
                    content=canonical_json_bytes(args), follow_redirects=False, timeout=30) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > limit:
                        if name == "save_v22_review_decision":
                            raise TransientJobError("V22_REVIEW_DECISION_STORAGE_UNAVAILABLE", "The save result could not be verified.")
                        raise error("TOO_LARGE")
                status = response.status_code
        except httpx.TransportError:
            raise TransientJobError("V22_REVIEW_DECISION_STORAGE_UNAVAILABLE", "Decision storage is temporarily unavailable.") from None
        if status == 429 or status >= 500:
            raise TransientJobError("V22_REVIEW_DECISION_STORAGE_UNAVAILABLE", "Decision storage is temporarily unavailable.")
        if status != 200:
            try:
                code = ERRORS.get(decode(raw).get("message", "").splitlines()[0], "UNAVAILABLE")
            except (ValueError, TypeError, AttributeError, IndexError):
                code = "UNAVAILABLE"
            raise error(code)
        return bytes(raw)
