"""Service-only persistence and a short-lived access-token broker client."""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import httpx

from .broker_signature import sign_google_broker_body
from .ga4 import GA4_SCOPE
from .gsc import GSC_SCOPE, SyncError, bounded_json

SYNC_SCOPES = {"gsc": GSC_SCOPE, "ga4": GA4_SCOPE}


class TokenBroker:
    def __init__(self, origin: str, secret: str, client: httpx.AsyncClient, source: str = "gsc"):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or len(secret) < 32:
            raise SyncError("SYNC_BROKER_NOT_CONFIGURED")
        if source not in SYNC_SCOPES:
            raise SyncError("SYNC_BROKER_NOT_CONFIGURED")
        self.origin, self.secret, self.client, self.source = origin.rstrip("/"), secret, client, source

    async def access_token(self, connection_id: str) -> str:
        connection_id = str(UUID(connection_id))
        body = json.dumps({"connection_id": connection_id, "purpose": "source_sync", "source": self.source}, separators=(",", ":"))
        timestamp, request_id, nonce = int(time.time()), str(uuid4()), secrets.token_urlsafe(32)
        signature = sign_google_broker_body(self.secret, timestamp=timestamp, request_id=request_id, nonce=nonce, body=body)
        status, payload = await bounded_json(self.client, "POST", f"{self.origin}/api/internal/v2/google/connections/{connection_id}/access-token",
            headers={"content-type": "application/json", "x-searchtrust-timestamp": str(timestamp), "x-searchtrust-request-id": request_id,
                     "x-searchtrust-nonce": nonce, "x-searchtrust-signature-version": "v1", "x-searchtrust-signature": signature}, content=body)
        if status == 429 or status >= 500:
            raise SyncError("SYNC_BROKER_UNAVAILABLE", True)
        if status != 200:
            raise SyncError("SYNC_AUTH_REQUIRED")
        try:
            if not isinstance(payload, dict) or payload.get("token_type") != "Bearer":
                raise ValueError()
            scopes = payload.get("granted_scopes")
            if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes) or SYNC_SCOPES[self.source] not in scopes:
                raise ValueError()
            expiry = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
            token = payload["access_token"]
            if expiry.tzinfo is None or (expiry - datetime.now(timezone.utc)).total_seconds() < 60 or not isinstance(token, str) or not token or len(token) > 16384 or any(ord(c)<33 for c in token):
                raise ValueError()
            return token
        except (ValueError, TypeError, KeyError, AttributeError):
            raise SyncError("SYNC_INVALID_BROKER_RESPONSE") from None


class SyncRepository:
    def __init__(self, url: str, key: str, client: httpx.AsyncClient, source: str = "gsc"):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or not key:
            raise SyncError("SYNC_STORAGE_NOT_CONFIGURED")
        if source not in SYNC_SCOPES:
            raise SyncError("SYNC_STORAGE_NOT_CONFIGURED")
        self.url, self.key, self.client, self.source = url.rstrip("/"), key, client, source

    async def request(self, method: str, path: str, *, body=None, params=None):
        status, data = await bounded_json(self.client, method, f"{self.url}/rest/v1/{path}",
            headers={"apikey": self.key, "authorization": f"Bearer {self.key}", "content-type": "application/json"}, body=body, params=params)
        if status == 429 or status >= 500:
            raise SyncError("SYNC_STORAGE_UNAVAILABLE", True)
        if status >= 400:
            raise SyncError("SYNC_STORAGE_REJECTED")
        return data

    async def pending(self):
        now = datetime.now(timezone.utc).isoformat()
        data = await self.request("GET", "google_sync_jobs", params={"select": "id", "source_type": f"eq.{self.source}", "limit": "20", "order": "created_at.asc",
            "or": f"(and(status.eq.queued,available_at.lte.{now}),and(status.eq.running,lease_expires_at.lte.{now}))"})
        if not isinstance(data, list):
            raise SyncError("SYNC_INVALID_STORAGE_RESPONSE")
        return [str(UUID(row["id"])) for row in data]

    async def claim(self, job_id: str):
        data = await self.request("POST", f"rpc/claim_v22_{self.source}_sync", body={"p_job_id": job_id})
        if data is not None and (not isinstance(data, dict) or data.get("id") != job_id or data.get("status") != "running"):
            raise SyncError("SYNC_INVALID_STORAGE_RESPONSE")
        return data

    async def finish(self, job: dict, payload: dict, checksum: str, health: str, reasons: list[str]):
        return await self.request("POST", f"rpc/finish_v22_{self.source}_sync", body={"p_job_id": job["id"], "p_lease_id": job["lease_id"],
            "p_payload": payload, "p_checksum": checksum, "p_health": health, "p_reasons": reasons})

    async def fail(self, job: dict, error: SyncError):
        await self.request("POST", f"rpc/fail_v22_{self.source}_sync", body={"p_job_id": job["id"], "p_lease_id": job["lease_id"],
            "p_code": error.code, "p_retryable": error.retryable})
