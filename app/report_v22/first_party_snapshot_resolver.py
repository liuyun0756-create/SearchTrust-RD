"""Bounded service-role resolver for trusted first-party snapshot inputs."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.first_party_findings_models import FirstPartyFindingsInput


MAX_RESPONSE_BYTES = 20_000_000


class FirstPartySnapshotResolver:
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient) -> None:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not service_role_key):
            raise DeterministicJobError("V22_FIRST_PARTY_INPUT_NOT_CONFIGURED",
                "First-party snapshot storage is not configured.")
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.http_client = http_client

    async def resolve(
        self,
        *,
        case_id: UUID,
        parent_report_id: UUID,
        gsc_snapshot_id: UUID,
        ga4_snapshot_id: UUID,
        gbp_snapshot_id: UUID | None,
        evaluated_at: datetime,
    ) -> FirstPartyFindingsInput:
        body = {
            "p_case_id": str(case_id),
            "p_parent_report_id": str(parent_report_id),
            "p_gsc_snapshot_id": str(gsc_snapshot_id),
            "p_ga4_snapshot_id": str(ga4_snapshot_id),
            "p_gbp_snapshot_id": str(gbp_snapshot_id) if gbp_snapshot_id else None,
            "p_evaluated_at": evaluated_at.isoformat(),
        }
        try:
            async with self.http_client.stream(
                "POST",
                f"{self.url}/rest/v1/rpc/resolve_v22_first_party_findings_input",
                headers={
                    "apikey": self.service_role_key,
                    "authorization": f"Bearer {self.service_role_key}",
                    "content-type": "application/json",
                },
                content=json.dumps(body, separators=(",", ":")),
                follow_redirects=False,
                timeout=30,
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DeterministicJobError("V22_FIRST_PARTY_INPUT_INVALID",
                            "First-party snapshot storage returned an oversized response.")
                status = response.status_code
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientJobError("V22_FIRST_PARTY_INPUT_UNAVAILABLE",
                "First-party snapshot storage is temporarily unavailable.") from exc
        if status == 429 or status >= 500:
            raise TransientJobError("V22_FIRST_PARTY_INPUT_UNAVAILABLE",
                "First-party snapshot storage is temporarily unavailable.")
        if status >= 400:
            raise DeterministicJobError("V22_FIRST_PARTY_INPUT_REJECTED",
                "The selected first-party snapshots are no longer eligible for this Case.")
        try:
            result = FirstPartyFindingsInput.model_validate_json(bytes(raw))
        except (ValidationError, ValueError, TypeError):
            raise DeterministicJobError("V22_FIRST_PARTY_INPUT_INVALID",
                "First-party snapshot storage returned an invalid response.") from None
        expected = {"gsc": gsc_snapshot_id, "ga4": ga4_snapshot_id, "gbp": gbp_snapshot_id}
        if (result.case_id != case_id or result.parent_report_id != parent_report_id
                or result.evaluated_at != evaluated_at
                or any(snapshot.snapshot_id != expected[snapshot.source_type] for snapshot in result.snapshots)
                or {snapshot.source_type for snapshot in result.snapshots}
                != ({"gsc", "ga4", "gbp"} if gbp_snapshot_id else {"gsc", "ga4"})):
            raise DeterministicJobError("V22_FIRST_PARTY_INPUT_INVALID",
                "First-party snapshot storage returned an inconsistent response.")
        if len(canonical_json_bytes(result)) > MAX_RESPONSE_BYTES:
            raise DeterministicJobError("V22_FIRST_PARTY_INPUT_INVALID",
                "First-party snapshot storage returned an oversized response.")
        return result
