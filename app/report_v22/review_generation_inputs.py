"""Resolve explicit immutable revisions using owner-scoped storage on every read.

Preparation is read-only. Neither this recipe nor its hash authorizes a charge,
freezes a task in storage, or permits the ordinary runtime's review-mode gate.
"""
from datetime import datetime, timezone
from uuid import UUID

from app.report_v22.review_plan_basis import PlanBasisReport
from app.jobs_v22.digest import verified_request_digest
from app.jobs_v22.models import utc_now
from app.report_v22.review_generation_models import (
    MAX_INPUT, MAX_REFERENCE, RULES, ReviewGenerationInput, ReviewGenerationRecipe,
    ReviewGenerationSelection, checked, invalid,
)


class ReviewGenerationPreparer:
    def __init__(self, decisions, *, clock=utc_now, allow_multi_round=False):
        self.decisions = decisions
        self.clock = clock
        self.allow_multi_round = allow_multi_round

    async def _resolve(self, *, user_id, case_id, report_id, selection, prepared_at):
        try:
            owner = {k: UUID(str(v)) for k, v in dict(user_id=user_id, case_id=case_id, report_id=report_id).items()}
        except (ValueError, TypeError, AttributeError):
            raise invalid() from None
        _, saved = await self.decisions.basis(**owner, review_id=selection.review_id)
        if isinstance(saved.bundle.report,PlanBasisReport) and not self.allow_multi_round:
            raise invalid("UNAVAILABLE")
        decision = await self.decisions.read(**owner, review_id=selection.review_id, decision_id=selection.decision_id)
        if (saved.id, saved.bundle.checksum, decision.id, decision.checksum) != (
                selection.review_id, selection.review_checksum, selection.decision_id, selection.decision_checksum):
            raise invalid()
        raw = dict(schema_version="v22_review_generation_input_v1", rules_version=RULES,
            job_id=str(selection.job_id), **{k: str(v) for k, v in owner.items()}, prepared_at=prepared_at,
            evidence=saved.model_dump(mode="json"), decision=decision.model_dump(mode="json"))
        return checked(ReviewGenerationInput, raw | {"checksum": verified_request_digest(raw)}, MAX_INPUT)

    async def prepare(self, *, user_id, case_id, report_id, selection):
        selection = checked(ReviewGenerationSelection, selection, MAX_REFERENCE)
        prepared_at = self.clock()
        if isinstance(prepared_at, datetime):
            if prepared_at.tzinfo is None:
                raise invalid()
            prepared_at = prepared_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return await self._resolve(user_id=user_id, case_id=case_id, report_id=report_id,
            selection=selection, prepared_at=prepared_at)

    async def resolve(self, *, user_id, case_id, report_id, recipe):
        recipe = checked(ReviewGenerationRecipe, recipe, MAX_REFERENCE)
        if (str(recipe.user_id), str(recipe.case_id), str(recipe.report_id)) != (str(user_id), str(case_id), str(report_id)):
            raise invalid()
        value = await self._resolve(user_id=user_id, case_id=case_id, report_id=report_id,
            selection=recipe, prepared_at=recipe.prepared_at)
        if value.checksum != recipe.input_checksum:
            raise invalid()
        return value
