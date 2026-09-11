"""Persist traceable v2.2 source snapshots and reports before job success."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from app.api.v2.models import AnalyzeRequest
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.jobs_v22.digest import request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.models import ReportV22


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
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.http_client = http_client
        self.max_response_bytes = max_response_bytes

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
    ) -> None:
        if not self.url or not self.service_role_key:
            raise DeterministicJobError(
                "V22_RESULT_PERSISTENCE_NOT_CONFIGURED",
                "Report storage is not configured for this analysis service.",
            )

        site_checksum = request_digest(site_inventory)
        competitor_checksum = request_digest(competitor_collection)
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
        try:
            response = await self.http_client.post(
                f"{self.url}/rest/v1/rpc/persist_v22_prospect_result",
                headers={
                    "apikey": self.service_role_key,
                    "authorization": f"Bearer {self.service_role_key}",
                    "content-type": "application/json",
                },
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientJobError(
                "V22_RESULT_PERSISTENCE_UNAVAILABLE",
                "Report storage is temporarily unavailable. The task will retry automatically.",
            ) from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise TransientJobError(
                "V22_RESULT_PERSISTENCE_UNAVAILABLE",
                "Report storage is temporarily unavailable. The task will retry automatically.",
            )
        if response.status_code >= 400:
            raise DeterministicJobError(
                "V22_RESULT_PERSISTENCE_REJECTED",
                "The completed report could not be saved safely.",
            )

        if len(response.content) > self.max_response_bytes:
            raise TransientJobError(
                "V22_RESULT_PERSISTENCE_INVALID_RESPONSE",
                "Report storage returned an invalid response. The task will retry automatically.",
            )

        try:
            rows = response.json()
            row = rows[0] if isinstance(rows, list) and rows else rows
            if not isinstance(row, dict) or row.get("report_id") != str(job_id):
                raise ValueError("unexpected persistence response")
        except (TypeError, ValueError) as exc:
            raise TransientJobError(
                "V22_RESULT_PERSISTENCE_INVALID_RESPONSE",
                "Report storage returned an invalid response. The task will retry automatically.",
            ) from exc
