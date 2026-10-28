"""Service-role boundary for the reserved first-service discovery, not intake."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.api.v2.competitor_models import CompetitorDiscoveryRequest
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient
from app.report_v22.models import StrictModel


class DiscoveryWork(StrictModel):
    outcome: Literal["dispatch", "observe", "claimed"]
    workflow_id: UUID
    user_id: UUID
    case_id: UUID
    discovery_job_id: UUID
    request: CompetitorDiscoveryRequest
    deadline_at: datetime
    attempts: int = Field(ge=0, le=3)
    lease_until: datetime | None
    dispatch_token: UUID
    cost_ledger_job_id: UUID
    cost_ledger_created_at: datetime


class FirstServiceDiscoveryRepository:
    def __init__(self, *, url, service_role_key, http_client):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key,
            http_client=http_client, max_response_bytes=1_000_000,
            prefix="V22_FIRST_SERVICE_DISCOVERY")

    async def pending(self) -> list[UUID]:
        raw = await self.rpc.post("list_v22_first_service_discovery_work", {"p_limit": 25})
        try:
            if not isinstance(raw, list) or len(raw) > 25:
                raise ValueError()
            ids = [UUID(row["workflow_id"]) for row in raw if set(row) == {"workflow_id"}]
            if len(ids) != len(raw) or len(set(ids)) != len(ids):
                raise ValueError()
            return ids
        except (ValueError, TypeError, KeyError, AttributeError):
            raise self.rpc.invalid() from None

    async def expire(self):
        # SQL alone decides the deadline and exactly-once compensation.
        await self.rpc.post("expire_v22_first_service_discoveries", {"p_limit": 100})

    async def claim(self, workflow_id, token, *, discovery_job_id=None) -> DiscoveryWork | None:
        running = discovery_job_id is not None
        rpc = "claim_v22_first_service_discovery_" + ("run" if running else "dispatch")
        args = {"p_workflow_id": str(workflow_id)}
        args["p_lease_token" if running else "p_dispatch_token"] = str(token)
        if running:
            args["p_discovery_job_id"] = str(discovery_job_id)
        raw = await self.rpc.post(rpc, args)
        if raw in [{"outcome": state} for state in (("closed", "busy", "exhausted") if running else ("closed",))]:
            return None
        try:
            work = DiscoveryWork.model_validate_json(canonical_json_bytes(raw))
            if (work.workflow_id != workflow_id or work.request.case_id != work.case_id
                    or work.request.search_device != "mobile" or work.request.search_language != "en"
                    or work.deadline_at.tzinfo is None
                    or work.cost_ledger_created_at.tzinfo is None
                    or work.cost_ledger_created_at >= work.deadline_at
                    or work.cost_ledger_job_id == work.workflow_id
                    or (work.cost_ledger_job_id == work.discovery_job_id and work.request.supplemental_website_urls)
                    or (work.cost_ledger_job_id != work.discovery_job_id and work.attempts < 1)
                    or (work.lease_until is not None and work.lease_until.tzinfo is None)):
                raise ValueError()
            if running:
                if (work.outcome != "claimed" or work.discovery_job_id != discovery_job_id
                        or work.attempts < 1 or work.lease_until is None):
                    raise ValueError()
            elif work.outcome not in ("dispatch", "observe") or (work.outcome == "dispatch" and work.dispatch_token != token):
                raise ValueError()
            return work
        except (ValueError, TypeError):
            raise self.rpc.invalid() from None

    async def lease(self, workflow_id, token, *, release=False) -> bool:
        raw = await self.rpc.post(("release" if release else "heartbeat") + "_v22_first_service_discovery",
            {"p_workflow_id": str(workflow_id), "p_lease_token": str(token)})
        if type(raw) is not bool:
            raise self.rpc.invalid()
        return raw

    async def finish(self, work: DiscoveryWork, result=None):
        args = {"p_user_id": str(work.user_id), "p_case_id": str(work.case_id),
                "p_workflow_id": str(work.workflow_id)}
        if result is not None:
            args["p_result"] = result.model_dump(mode="json")
            await self.rpc.post("record_v22_first_service_discovery", args)
        else:
            args.update(p_discovery_job_id=str(work.discovery_job_id), p_reason="DISCOVERY_FAILED")
            await self.rpc.post("fail_v22_first_service_discovery", args)
