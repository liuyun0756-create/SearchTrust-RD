"""Owner-bound page check persistence. No billing or implicit retry."""
import json
from typing import Literal
from uuid import UUID

import httpx
from pydantic import Field, TypeAdapter, model_validator

from app.report_v22.review_plan_basis import parse_plan_report,parse_plan_delivery,plan_report_checksum
from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.capability import BusinessScope, CapabilityReport, DeliveryRevision, ImplementationReference, Timestamp, instant
from app.report_v22.comparison_storage import ComparisonRepository
from app.report_v22.page_implementation_checks import prepare_page_check
from app.report_v22.page_check_limits import PageCheckLimits, PageCheckRateLimited
from app.report_v22.page_implementation_models import Conditions, FrozenModel, PageCheckPlan, PageObservation, clean_text, page_url


class PageCheckSelection(FrozenModel):
    implementation: ImplementationReference
    action_id: str = Field(min_length=1, max_length=200)
    target_url: str = Field(max_length=2000)
    conditions: Conditions

    @model_validator(mode="after")
    def consistent(self):
        clean_text(self.action_id)
        if self.target_url != page_url(self.target_url) or len({c.id for c in self.conditions}) != len(self.conditions):
            raise ValueError("normalized target and distinct conditions required")
        if len(verified_canonical_json_bytes(self.model_dump(mode="json"))) > 20 * 1024:
            raise ValueError("selection too large")
        return self


ErrorCode = Literal["PAGE_CHECK_SOURCE_CHANGED", "PAGE_CHECK_EXPIRED", "PAGE_CHECK_INTERRUPTED", "PAGE_CHECK_FAILED"]


class PageCheckSummary(FrozenModel):
    id: UUID
    user_id: UUID
    case_id: UUID
    report_id: UUID
    delivery_revision_id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    created_at: Timestamp
    completed_at: Timestamp | None
    error_code: ErrorCode | None

    @model_validator(mode="after")
    def state(self):
        if (self.status in ("completed", "failed")) != (self.completed_at is not None):
            raise ValueError("invalid terminal time")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("invalid failure state")
        if self.completed_at and instant(self.completed_at) < instant(self.created_at):
            raise ValueError("completion predates request")
        return self


class PageCheckJob(PageCheckSummary):
    schema_version: Literal["v22_page_check_job_v1"]
    selection: PageCheckSelection
    plan: PageCheckPlan
    case_updated_at: Timestamp
    lease_id: UUID | None
    claimed_at: Timestamp | None
    lease_expires_at: Timestamp | None
    observation: PageObservation | None

    @model_validator(mode="after")
    def consistent(self):
        b, s = self.plan.binding, self.selection
        if ((b.user_id, b.case_id, b.report_id, b.delivery_revision_id) !=
                (self.user_id, self.case_id, self.report_id, self.delivery_revision_id)
                or (b.implementation_id, b.implementation_revision, b.implementation_checksum) !=
                (s.implementation.id, s.implementation.revision, s.implementation.checksum)
                or (b.action_id, self.plan.target_url, self.plan.conditions) != (s.action_id, s.target_url, s.conditions)):
            raise ValueError("stored source differs from selection")
        created = instant(self.created_at)
        if created < max(instant(b.implementation_recorded_at), instant(self.case_updated_at)):
            raise ValueError("request predates source")
        lease = (self.lease_id, self.claimed_at, self.lease_expires_at)
        if any(v is not None for v in lease):
            if any(v is None for v in lease) or self.status == "queued":
                raise ValueError("partial or premature lease")
            if instant(self.claimed_at) < created or (instant(self.lease_expires_at) - instant(self.claimed_at)).total_seconds() != 120:
                raise ValueError("invalid lease time")
        elif self.status in ("running", "completed"):
            raise ValueError("missing lease")
        if self.completed_at and self.claimed_at and instant(self.completed_at) < instant(self.claimed_at):
            raise ValueError("completion predates claim")
        if self.status == "completed":
            o = self.observation
            if o is None or o.id != self.id or o.plan != self.plan:
                raise ValueError("missing bound observation")
            if not (instant(self.claimed_at) <= instant(o.observed_at) <= instant(self.completed_at) <= instant(self.lease_expires_at)):
                raise ValueError("observation outside execution window")
        elif self.observation is not None:
            raise ValueError("partial result published")
        return self


def invalid():
    return DeterministicJobError("V22_PAGE_CHECK_INVALID", "Page check storage returned inconsistent information.")


class PageCheckRepository(ComparisonRepository):
    """Trusted server caller supplies owner IDs; service credentials never reach UI."""

    async def _rpc(self, name, args):
        limit = 2 * 1024 * 1024 if name == "resolve_v22_page_check_input" else 128 * 1024
        try:
            async with self.client.stream("POST", f"{self.url}/rest/v1/rpc/{name}",
                    headers={"apikey": self.key, "authorization": f"Bearer {self.key}", "content-type": "application/json"},
                    content=canonical_json_bytes(args), follow_redirects=False, timeout=30) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > limit:
                        raise invalid()
                    raw.extend(chunk)
                status = response.status_code
        except (httpx.TimeoutException, httpx.NetworkError):
            raise TransientJobError("V22_PAGE_CHECK_UNAVAILABLE", "Page check storage is temporarily unavailable.") from None
        if status == 429:
            try:
                error = json.loads(raw)
                if error.get("code") == "PT429" and error.get("message") == "PAGE_CHECK_RATE_LIMIT":
                    limits = PageCheckLimits.model_validate_json(error["details"])
                    if limits.reason is not None:
                        raise PageCheckRateLimited(limits)
            except (ValueError, TypeError, KeyError):
                pass
        if status == 429 or status >= 500:
            raise TransientJobError("V22_PAGE_CHECK_UNAVAILABLE", "Page check storage is temporarily unavailable.")
        if status != 200:
            raise DeterministicJobError("V22_PAGE_CHECK_REJECTED", "Page check request could not be accepted.")
        return bytes(raw)

    @staticmethod
    def checked(raw, *, check_id, user_id=None, case_id=None):
        try:
            job = PageCheckJob.model_validate_json(raw)
            if job.id != check_id or (user_id is not None and job.user_id != user_id) or (case_id is not None and job.case_id != case_id):
                raise ValueError("identity mismatch")
            return job
        except (ValueError, TypeError, KeyError, OverflowError):
            raise invalid() from None

    async def read_check(self, *, user_id, case_id, check_id):
        raw = await self._rpc("get_v22_page_check", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(check_id)))
        return None if raw.strip() == b"null" else self.checked(raw, check_id=check_id, user_id=user_id, case_id=case_id)

    async def history(self, *, user_id, case_id):
        raw = await self._rpc("list_v22_page_checks", dict(p_user_id=str(user_id), p_case_id=str(case_id)))
        try:
            rows = TypeAdapter(list[PageCheckSummary]).validate_json(raw)
            if len(rows) > 20 or len({j.id for j in rows}) != len(rows) or any(j.user_id != user_id or j.case_id != case_id for j in rows):
                raise ValueError("invalid history")
            return rows
        except (ValueError, TypeError):
            raise invalid() from None

    async def resolve(self, *, user_id, case_id, selection):
        selection = PageCheckSelection.model_validate_json(selection.model_dump_json())
        raw = await self._rpc("resolve_v22_page_check_input", dict(p_user_id=str(user_id), p_case_id=str(case_id),
            p_selection=selection.model_dump(mode="json")))
        try:
            data = json.loads(raw)
            report = parse_plan_report(data["report"])
            business = BusinessScope.model_validate_json(json.dumps(data["business"]))
            delivery = parse_plan_delivery(data["delivery"])
            if plan_report_checksum(report) != data["report_checksum"]:
                raise ValueError("source digest mismatch")
            plan = prepare_page_check(implementation=data["record"], business=business,
                action_id=selection.action_id, target_url=selection.target_url, conditions=selection.conditions,
                report=report, user_id=user_id, case_id=case_id, delivery_revision_id=UUID(data["delivery_id"]),
                delivery_revision=data["delivery_revision"], delivery_content=delivery)
            b = plan.binding
            if (b.implementation_id, b.implementation_revision, b.implementation_checksum) != (
                    selection.implementation.id, selection.implementation.revision, selection.implementation.checksum):
                raise ValueError("resolved implementation differs")
            instant(data["case_updated_at"])
            return plan, data["case_updated_at"], verified_canonical_json_bytes(business.model_dump(mode="json")).decode()
        except (ValueError, TypeError, KeyError, OverflowError):
            raise invalid() from None

    async def request_check(self, *, user_id, case_id, check_id, selection, report_id=None, delivery_revision_id=None):
        selection = PageCheckSelection.model_validate_json(selection.model_dump_json())
        saved = await self.read_check(user_id=user_id, case_id=case_id, check_id=check_id)
        if saved:
            if (report_id is not None and saved.report_id != report_id) or (delivery_revision_id is not None and saved.delivery_revision_id != delivery_revision_id):
                raise DeterministicJobError("V22_PAGE_CHECK_CONFLICT", "This request belongs to another report.")
            if saved.selection != selection:
                raise DeterministicJobError("V22_PAGE_CHECK_CONFLICT", "This request has different selections.")
            return saved
        plan, marker, canonical_business = await self.resolve(user_id=user_id, case_id=case_id, selection=selection)
        if (report_id is not None and plan.binding.report_id != report_id) or (delivery_revision_id is not None and plan.binding.delivery_revision_id != delivery_revision_id):
            raise DeterministicJobError("V22_PAGE_CHECK_CONFLICT", "This selection belongs to another report.")
        raw = await self._rpc("request_v22_page_check", dict(p_user_id=str(user_id), p_case_id=str(case_id), p_id=str(check_id),
            p_selection=selection.model_dump(mode="json"), p_plan=plan.model_dump(mode="json"), p_case_updated_at=marker,
            p_canonical_business=canonical_business,
            p_canonical_plan=verified_canonical_json_bytes(plan.model_dump(mode="json", exclude={"checksum"})).decode()))
        job = self.checked(raw, check_id=check_id, user_id=user_id, case_id=case_id)
        # Concurrent identical requests recover the original plan, never rebind it.
        if job.selection != selection or job.plan != plan:
            raise invalid()
        return job

    async def claim_check(self, check_id):
        raw = await self._rpc("claim_v22_page_check", dict(p_id=str(check_id)))
        if raw.strip() == b"null":
            return None
        job = self.checked(raw, check_id=check_id)
        if job.status != "running":
            raise invalid()
        return job

    async def finish_check(self, job, observation):
        job = PageCheckJob.model_validate_json(job.model_dump_json())
        observation = PageObservation.model_validate_json(observation.model_dump_json())
        if job.status != "running" or observation.id != job.id or observation.plan != job.plan or instant(observation.observed_at) < instant(job.claimed_at):
            raise invalid()
        raw = await self._rpc("finish_v22_page_check", dict(p_id=str(job.id), p_lease_id=str(job.lease_id),
            p_payload=observation.model_dump(mode="json"),
            p_canonical_payload=verified_canonical_json_bytes(observation.model_dump(mode="json", exclude={"checksum"})).decode()))
        saved = self.checked(raw, check_id=job.id, user_id=job.user_id, case_id=job.case_id)
        if (saved.selection != job.selection or saved.plan != job.plan or saved.lease_id != job.lease_id
                or saved.status not in ("completed", "failed") or (saved.status == "completed" and saved.observation != observation)):
            raise invalid()
        return saved

    async def fail_check(self, job):
        await self._rpc("fail_v22_page_check", dict(p_id=str(job.id), p_lease_id=str(job.lease_id)))

    async def expire_check(self, check_id):
        # Explicit state transaction; never called by owner read/history.
        raw = await self._rpc("expire_v22_page_check", dict(p_id=str(check_id)))
        if raw.strip() not in (b"true", b"false"):
            raise invalid()
        return raw.strip() == b"true"
