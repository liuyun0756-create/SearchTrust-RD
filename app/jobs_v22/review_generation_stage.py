"""Internal checkpoint only: deliberately not registered with paid job dispatch."""
from typing import Literal
from uuid import UUID

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import Checksum
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_generation_content import (
    ReviewGenerationContent, build_review_generation_content, validate_review_generation_content,
)
from app.report_v22.review_generation_models import MAX_CHECKPOINT, MAX_INPUT, RULES, ReviewGenerationInput, checked, invalid


class ReviewGenerationCheckpoint(StrictModel):
    schema_version: Literal["v22_review_generation_checkpoint_v1"]
    job_id: UUID
    input_checksum: Checksum
    result_checksum: Checksum
    result: ReviewGenerationContent


class _ReviewCheckpoints(JobCheckpoints):
    async def get(self, job_id, checkpoint_key):
        raw = await self.redis.get(self.keys.checkpoint(job_id, checkpoint_key))
        if raw is None:
            return None
        # Decode before the generic checkpoint JSON loader can discard duplicate
        # keys. Size, nonfinite values and unknown fields are checked here too.
        return checked(ReviewGenerationCheckpoint, raw, MAX_CHECKPOINT).model_dump(mode="json")


class CheckpointedReviewGenerationStage:
    async def build_frozen(self, *, job_id: UUID, frozen, run_generation: int, checkpoints):
        from app.jobs_v22.review_input_repository import require_frozen_review_input
        value = require_frozen_review_input(frozen, run_generation=run_generation)
        if type(checkpoints.run_generation) is not int or checkpoints.run_generation != run_generation:
            raise invalid("UNTRUSTED_INPUT")
        return await self.build(job_id=job_id, value=value, checkpoints=checkpoints)

    async def build(self, *, job_id: UUID, value, checkpoints):
        value = checked(ReviewGenerationInput, value, MAX_INPUT)
        if job_id != value.job_id:
            raise invalid()
        key = f"{RULES}:result:{value.checksum[7:]}"
        checkpoints = _ReviewCheckpoints(checkpoints.redis, prefix=checkpoints.keys.prefix,
            ttl_seconds=checkpoints.ttl_seconds, run_generation=checkpoints.run_generation)

        async def generate():
            result = build_review_generation_content(value).model_dump(mode="json")
            raw = dict(schema_version="v22_review_generation_checkpoint_v1", job_id=str(job_id),
                input_checksum=value.checksum, result_checksum=verified_request_digest(result), result=result)
            return checked(ReviewGenerationCheckpoint, raw, MAX_CHECKPOINT).model_dump(mode="json")

        await checkpoints.run_once(job_id, key, generate)
        # A superseded run is not successful merely because it computed content.
        raw = await checkpoints.get(job_id, key)
        if raw is None:
            raise invalid("CHECKPOINT_INVALID")
        envelope = checked(ReviewGenerationCheckpoint, raw, MAX_CHECKPOINT)
        if (envelope.job_id != job_id or envelope.input_checksum != value.checksum or
                envelope.result_checksum != verified_request_digest(envelope.result.model_dump(mode="json"))):
            raise invalid("CHECKPOINT_INVALID")
        return validate_review_generation_content(envelope.result, value)
