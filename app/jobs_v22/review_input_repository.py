"""Immutable review input storage; no reservation, dispatch or completion call."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID
from weakref import WeakKeyDictionary

import httpx
from pydantic import Field

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.capability import Timestamp, instant
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_storage import ComparisonRepository
from app.report_v22.review_freeze_storage import decode
from app.report_v22.review_generation_models import (
    MAX_INPUT, ReviewGenerationInput, ReviewGenerationRecipe, checked, invalid, recipe_for,
)

MAX_RECEIPT = 5_000_000
ERRORS = {f"REVIEW_GENERATION_{code}": code for code in
    ("INVALID", "UNAVAILABLE", "CONFLICT", "INACTIVE", "NOT_RESERVED", "TOO_LARGE")}


def unavailable():
    return TransientJobError("V22_REVIEW_GENERATION_STORAGE_UNAVAILABLE",
        "The stored review input could not be verified. Recover the same task before retrying.")


class ReviewInputReceipt(StrictModel):
    schema_version: Literal["v22_frozen_review_generation_v1"]
    job_id: UUID
    run_generation: int = Field(ge=1, lt=2147483647)
    frozen_at: Timestamp
    recipe: ReviewGenerationRecipe
    input_text: str = Field(max_length=MAX_INPUT)


@dataclass(frozen=True, eq=False, slots=True, weakref_slot=True)
class FrozenReviewGeneration:
    value: ReviewGenerationInput
    recipe: ReviewGenerationRecipe
    frozen_at: str


_SEALS: WeakKeyDictionary[FrozenReviewGeneration, tuple[int, str]] = WeakKeyDictionary()


def _digest(frozen):
    return verified_request_digest(dict(value=frozen.value.model_dump(mode="json"),
        recipe=frozen.recipe.model_dump(mode="json"), frozen_at=frozen.frozen_at))


def require_frozen_review_input(value, *, run_generation):
    try:
        valid = (type(run_generation) is int and type(value) is FrozenReviewGeneration
            and _SEALS.get(value) == (run_generation, _digest(value)))
    except (ValueError, TypeError, AttributeError, RecursionError):
        valid = False
    if not valid:
        raise invalid("UNTRUSTED_INPUT")
    return value.value


class ReviewInputRepository(ComparisonRepository):
    RPC_NAMES = {"freeze_v22_review_generation_input", "resolve_v22_review_generation_input"}
    ERROR_CODES = ERRORS
    response_limit = MAX_RECEIPT
    storage_error = staticmethod(unavailable)
    input_error = staticmethod(invalid)
    def __init__(self, **kwargs):
        try:
            super().__init__(**kwargs)
        except DeterministicJobError:
            raise self.input_error("UNAVAILABLE") from None

    async def _rpc(self, name, args):
        if name not in self.RPC_NAMES:
            raise self.input_error()
        try:
            async with asyncio.timeout(30):
                async with self.client.stream("POST", f"{self.url}/rest/v1/rpc/{name}",
                    headers={"apikey": self.key, "authorization": f"Bearer {self.key}", "content-type": "application/json"},
                    content=canonical_json_bytes(args), follow_redirects=False, timeout=30) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > (self.response_limit if response.status_code == 200 else 8192):
                            raise self.storage_error()
                        raw.extend(chunk)
                    status = response.status_code
        except (httpx.TransportError, TimeoutError):
            raise self.storage_error() from None
        if status == 429 or status >= 500:
            raise self.storage_error()
        if status != 200:
            try:
                code = self.ERROR_CODES.get(decode(raw).get("message", "").splitlines()[0], "UNAVAILABLE")
            except (ValueError, TypeError, AttributeError, IndexError):
                code = "UNAVAILABLE"
            raise self.input_error(code)
        return bytes(raw)

    @staticmethod
    def identity(*, user_id, case_id, job_id, run_generation):
        try:
            if type(run_generation) is not int or not 1 <= run_generation < 2147483647:
                raise ValueError("invalid generation")
            return {"p_" + k: str(UUID(str(v))) for k, v in dict(user_id=user_id, case_id=case_id, job_id=job_id).items()} | {
                "p_run_generation": run_generation}
        except (ValueError, TypeError, AttributeError):
            raise invalid() from None

    @staticmethod
    def _receipt(raw, args, expected_checksum=None):
        try:
            r = checked(ReviewInputReceipt, raw, MAX_RECEIPT)
            value = checked(ReviewGenerationInput, r.input_text, MAX_INPUT)
            if (str(r.job_id) != args["p_job_id"] or r.run_generation != args["p_run_generation"]
                or str(value.job_id) != args["p_job_id"] or str(value.user_id) != args["p_user_id"]
                or str(value.case_id) != args["p_case_id"] or recipe_for(value) != r.recipe
                or (expected_checksum is not None and value.checksum != expected_checksum)
                or instant(r.frozen_at) + timedelta(minutes=1) < instant(value.prepared_at)):
                raise invalid()
        except (DeterministicJobError, ValueError, TypeError, AttributeError):
            # Successful write may already have committed. Never treat a bad
            # receipt as grounds to refund, replace inputs, or create a new job.
            raise unavailable() from None
        frozen = FrozenReviewGeneration(value, r.recipe, r.frozen_at)
        _SEALS[frozen] = (r.run_generation, _digest(frozen))
        return frozen

    async def freeze(self, *, value, run_generation):
        value = checked(ReviewGenerationInput, value, MAX_INPUT)
        args = self.identity(user_id=value.user_id, case_id=value.case_id, job_id=value.job_id, run_generation=run_generation)
        raw = await self._rpc("freeze_v22_review_generation_input", args | {
            "p_input_text": canonical_json_bytes(value.model_dump(mode="json")).decode()})
        return self._receipt(raw, args, value.checksum)

    async def resolve(self, *, user_id, case_id, job_id, run_generation, expected_checksum=None):
        args = self.identity(user_id=user_id, case_id=case_id, job_id=job_id, run_generation=run_generation)
        raw = await self._rpc("resolve_v22_review_generation_input", args)
        return self._receipt(raw, args, expected_checksum)
