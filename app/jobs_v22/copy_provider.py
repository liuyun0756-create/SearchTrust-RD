"""V2.2-only Dify adapter for choosing bounded backend-owned copy patterns."""

from __future__ import annotations

from uuid import UUID

import httpx

from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.copy_models import CopyRequestV1


class DifyControlledCopyProvider:
    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        model_version: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.model_version = model_version
        self.http_client = http_client

    async def generate(self, *, job_id: UUID, request: CopyRequestV1) -> object:
        if not self.api_key:
            raise DeterministicJobError(
                "V22_COPY_PROVIDER_NOT_CONFIGURED",
                "The v2.2 copy service is not configured.",
            )
        try:
            response = await self.http_client.post(
                f"{self.api_url}/workflows/run",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "inputs": {"copy_request": request.model_dump_json()},
                    "response_mode": "blocking",
                    "user": f"v22-job-{job_id}",
                },
            )
        except httpx.HTTPError as exc:
            raise TransientJobError(
                "V22_COPY_PROVIDER_UNAVAILABLE",
                "The copy service is temporarily unavailable.",
            ) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientJobError(
                "V22_COPY_PROVIDER_UNAVAILABLE",
                "The copy service is temporarily unavailable.",
            )
        if response.status_code >= 400:
            raise DeterministicJobError(
                "V22_COPY_PROVIDER_REJECTED",
                "The copy service rejected the v2.2 request.",
            )
        try:
            payload = response.json()
            outputs = payload["data"]["outputs"]
        except (KeyError, TypeError, ValueError):
            return {}
        return outputs if isinstance(outputs, dict) else {}
