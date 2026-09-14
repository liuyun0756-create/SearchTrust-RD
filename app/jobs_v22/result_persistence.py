"""Persist traceable v2.2 source snapshots and reports before job success."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from app.api.v2.models import AnalyzeRequest
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.jobs_v22.digest import request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient
from app.report_v22.models import ReportV22
from app.report_v22.public_gbp_models import CustomerPublicGbpReference, CustomerPublicGbpSnapshot


def result_snapshot_id(kind: str, case_id: UUID, checksum: str) -> UUID:
    """Return a Case-scoped deterministic ID for an immutable source payload."""

    return uuid5(NAMESPACE_URL, f"searchtrust:v22:{case_id}:{kind}:{checksum}")


class SupabaseResultPersister:
    """Use one service-role RPC so snapshots, report, and job linkage commit together."""

    def __init__(
        self,
        *,
        url: str,
        service_role_key: str,
        http_client: httpx.AsyncClient,
        max_response_bytes: int = 65_536,
    ) -> None:
        # Keep worker startup independent of optional storage configuration;
        # validation still happens before the first persistence request.
        self._rpc_args = dict(url=url, service_role_key=service_role_key,
            http_client=http_client, max_response_bytes=max_response_bytes,
            prefix="V22_RESULT_PERSISTENCE", invalid_retryable=True)
        self.rpc: VerifiedRpcClient | None = None

    def _rpc(self) -> VerifiedRpcClient:
        if self.rpc is None:
            self.rpc = VerifiedRpcClient(**self._rpc_args)
        return self.rpc

    async def persist(
        self,
        *,
        job_id: UUID,
        request: AnalyzeRequest,
        site_inventory: SiteInventorySnapshot,
        shared_market: SharedMarketSnapshot,
        competitor_collection: CompetitorCollectionSnapshot,
        report: ReportV22,
        run_generation: int | None = None,
        public_gbp_snapshot: CustomerPublicGbpSnapshot,
        public_gbp_reference: CustomerPublicGbpReference,
    ) -> None:
        site_checksum = request_digest(site_inventory)
        competitor_checksum = request_digest(competitor_collection)
        claimed_public_gbp = any(
            item.source_type == "gbp" and item.health_status == "healthy"
            for item in getattr(getattr(report, "data_coverage", None), "sources", [])
        )
        if not claimed_public_gbp:
            raise DeterministicJobError("V22_RESULT_PUBLIC_GBP_INVALID",
                "A Prospect report must contain its healthy public GBP evidence.")
        payload: dict[str, Any] = {
            "p_job_id": str(job_id),
            "p_case_id": str(request.case_id),
            "p_site_snapshot_id": str(result_snapshot_id("site", request.case_id, site_checksum)),
            "p_site_payload": site_inventory.model_dump(mode="json"),
            "p_site_checksum": site_checksum,
            "p_serp_snapshot_id": str(shared_market.snapshot_id),
            "p_serp_payload": shared_market.snapshot.model_dump(mode="json"),
            "p_serp_checksum": shared_market.snapshot_checksum,
            "p_serp_expires_at": shared_market.expires_at.isoformat(),
            "p_competitor_snapshot_id": str(
                result_snapshot_id("competitor", request.case_id, competitor_checksum)
            ),
            "p_competitor_payload": competitor_collection.model_dump(mode="json"),
            "p_competitor_checksum": competitor_checksum,
            "p_report_payload": report.model_dump(mode="json"),
        }
        if run_generation is not None:
            payload["p_run_generation"] = run_generation
        if (public_gbp_snapshot.health_status != "healthy"
                or public_gbp_snapshot.identity_match_status != "matched"
                or public_gbp_snapshot.subject_reference_checksum != request_digest(public_gbp_reference)
                or public_gbp_reference.case_id != request.case_id):
            raise DeterministicJobError("V22_RESULT_PUBLIC_GBP_INVALID",
                "The public GBP snapshot is not safely bound to this Case.")
        public_checksum = request_digest(public_gbp_snapshot)
        payload.update({
            "p_public_gbp_snapshot_id": str(result_snapshot_id(
                "public_gbp", request.case_id, public_checksum)),
            "p_public_gbp_payload": public_gbp_snapshot.model_dump(mode="json"),
            "p_public_gbp_checksum": public_checksum,
            "p_public_gbp_expires_at": public_gbp_snapshot.expires_at.isoformat(),
            "p_public_gbp_reference": public_gbp_reference.model_dump(mode="json"),
        })
        rpc = self._rpc()
        rows = await rpc.post("persist_v22_prospect_result", payload)
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
        if (not isinstance(row, dict) or set(row) != {"report_id", "idempotent"}
                or row["report_id"] != str(job_id) or type(row["idempotent"]) is not bool):
            raise rpc.invalid() from None
