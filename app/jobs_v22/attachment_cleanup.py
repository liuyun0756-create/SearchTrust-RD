"""Bounded, exact-object cleanup using the existing service-role SQL boundary."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID

import httpx

from app.jobs_v22.verified_input_resolver import VerifiedRpcClient

BUCKET = "implementation-files"
MAX_BYTES = 2_097_152
JSON_LIMIT = 131_072
MIMES = {"image/png", "image/jpeg", "application/pdf"}


class CleanupUnavailable(Exception):
    """Deliberately contains no provider or attachment details."""


def candidate(row: object, now: datetime) -> tuple[str, str]:
    if not isinstance(row, dict):
        raise CleanupUnavailable()
    identity = row.get("id")
    try:
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError()
        due = datetime.fromisoformat(row["cleanup_after"].replace("Z", "+00:00"))
        if due.tzinfo is None or due > now:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CleanupUnavailable() from None
    key = f"files/{identity}"
    if (row.get("object_key") != key or row.get("status") not in {"removing", "removed"}
            or row.get("mime") not in MIMES
            or type(row.get("byte_size")) is not int
            or not 1 <= row["byte_size"] <= MAX_BYTES):
        raise CleanupUnavailable()
    return identity, key


class AttachmentCleanup:
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key,
            http_client=http_client, max_response_bytes=JSON_LIMIT,
            prefix="V22_ATTACHMENT_CLEANUP", total_timeout_seconds=5)
        self.client = http_client
        self.origin = self.rpc.url
        self.headers = {"apikey": service_role_key, "authorization": f"Bearer {service_role_key}",
                        "accept-encoding": "identity"}

    async def _request(self, method: str, path: str, *, payload=None, absence=False):
        async with asyncio.timeout(5):
            async with self.client.stream(method, f"{self.origin}/{path}",
                    headers=self.headers, json=payload, timeout=5,
                    follow_redirects=False) as response:
                # An existing object must fail closed without downloading its body.
                if absence and 200 <= response.status_code < 300:
                    raise CleanupUnavailable()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise CleanupUnavailable()
                raw = bytearray()
                if response.is_stream_consumed:
                    if len(response.content) > JSON_LIMIT:
                        raise CleanupUnavailable()
                    raw.extend(response.content)
                else:
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > JSON_LIMIT:
                            raise CleanupUnavailable()
                        raw.extend(chunk)
                try:
                    body = json.loads(raw) if raw else None
                except (ValueError, RecursionError):
                    raise CleanupUnavailable() from None
                if absence:
                    if (response.status_code in {400, 404} and isinstance(body, dict)
                            and (body.get("code") == "NoSuchKey" or (
                                str(body.get("statusCode")) == "404"
                                and body.get("message") == "Object not found"))):
                        return None
                    raise CleanupUnavailable()
                if not 200 <= response.status_code < 300:
                    raise CleanupUnavailable()
                return body

    async def run(self) -> dict[str, int]:
        rows = await self.rpc.post("prepare_v22_attachment_cleanup", {})
        if not isinstance(rows, list) or len(rows) > 20:
            raise CleanupUnavailable()
        now = datetime.now(timezone.utc)
        candidates = [candidate(row, now) for row in rows]
        if len({identity for identity, _ in candidates}) != len(candidates):
            raise CleanupUnavailable()
        if not candidates:
            return {"processed": 0, "removed": 0, "failed": 0}
        bucket = await self._request("GET", f"storage/v1/bucket/{BUCKET}")
        if (not isinstance(bucket, dict) or bucket.get("id") != BUCKET
                or bucket.get("public") is not False
                or type(bucket.get("file_size_limit")) is not int
                or bucket["file_size_limit"] != MAX_BYTES
                or not isinstance(bucket.get("allowed_mime_types"), list)
                or sorted(bucket["allowed_mime_types"]) != sorted(MIMES)):
            raise CleanupUnavailable()
        semaphore = asyncio.Semaphore(4)

        async def remove(identity: str, key: str) -> bool:
            async with semaphore:
                try:
                    async with asyncio.timeout(12):
                        await self._request("DELETE", f"storage/v1/object/{BUCKET}", payload={"prefixes": [key]})
                        await self._request("GET", f"storage/v1/object/{BUCKET}/{key}", absence=True)
                        await self._request("POST", "rest/v1/rpc/finish_v22_attachment_cleanup",
                                            payload={"p_id": identity})
                    return True
                except Exception:
                    # SQL keeps the row due. Never log raw provider exceptions.
                    return False

        results = await asyncio.gather(*(remove(identity, key) for identity, key in candidates))
        removed = sum(results)
        return {"processed": len(results), "removed": removed, "failed": len(results) - removed}
