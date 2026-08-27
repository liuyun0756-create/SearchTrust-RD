"""Signed, idempotent delivery of durable task snapshots to Next.js."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Callable, Protocol
from uuid import UUID

import httpx

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.models import JobCallbackEvent, JobState
from app.jobs_v22.store import DurableJobStore


logger = logging.getLogger(__name__)
SIGNATURE_VERSION = "v1"


def build_callback_event(state: JobState) -> JobCallbackEvent:
    return JobCallbackEvent(
        event_id=f"{state.job_id}:{state.revision}",
        job_id=state.job_id,
        case_id=state.case_id,
        revision=state.revision,
        status=state.status,
        stage=state.stage,
        progress=state.progress,
        message=state.message,
        attempt_count=state.attempt_count,
        heartbeat_at=state.heartbeat_at,
        completed_at=state.completed_at,
        error=state.error,
        cost_counters=state.cost_counters,
        occurred_at=state.updated_at,
    )


def sign_callback_body(
    secret: str,
    timestamp: int,
    body: bytes,
    *,
    version: str = SIGNATURE_VERSION,
) -> str:
    signing_input = f"{version}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class CallbackSender(Protocol):
    async def send(self, state: JobState) -> bool: ...


class SignedCallbackClient:
    def __init__(
        self,
        *,
        url: str,
        secret: str,
        http_client: httpx.AsyncClient,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.url = url
        self.secret = secret
        self.http_client = http_client
        self.clock = clock or (lambda: int(time.time()))

    async def send(self, state: JobState) -> bool:
        event = build_callback_event(state)
        body = canonical_json_bytes(event)
        timestamp = self.clock()
        signature = sign_callback_body(self.secret, timestamp, body)
        headers = {
            "Content-Type": "application/json",
            "X-SearchTrust-Signature-Version": SIGNATURE_VERSION,
            "X-SearchTrust-Timestamp": str(timestamp),
            "X-SearchTrust-Event-ID": event.event_id,
            "X-SearchTrust-Signature": signature,
        }
        try:
            response = await self.http_client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "v2.2 callback transport failed job_id_suffix=%s revision=%d error=%s",
                str(state.job_id)[-8:],
                state.revision,
                type(exc).__name__,
            )
            return False
        if 200 <= response.status_code < 300:
            return True
        logger.warning(
            "v2.2 callback rejected job_id_suffix=%s revision=%d status=%d",
            str(state.job_id)[-8:],
            state.revision,
            response.status_code,
        )
        return False


class CallbackSynchronizer:
    def __init__(self, store: DurableJobStore, sender: CallbackSender) -> None:
        self.store = store
        self.sender = sender

    async def sync(self, job_id: UUID) -> bool:
        state = await self.store.require_state(job_id)
        if state.callback_synced_revision >= state.revision:
            return True
        if not await self.sender.send(state):
            return False
        await self.store.mark_callback_synced(job_id, state.revision)
        return True
