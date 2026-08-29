"""Redis-backed, expiring shared v2.2 market snapshots."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from redis.asyncio import Redis

from app.collectors.serp_market_models import SerpMarketSnapshot
from app.competitors_v22.models import SharedMarketSnapshot
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError


class CompetitorMarketSnapshotError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_COMPETITOR_MARKET_SNAPSHOT_INVALID",
            "A saved competitor market snapshot could not be validated.",
        )


class SharedMarketSnapshotStore:
    def __init__(self, redis: Redis, *, prefix: str, ttl_seconds: int) -> None:
        normalized = prefix.strip().strip(":")
        if not normalized:
            raise ValueError("Redis key prefix must not be empty")
        if not 60 <= ttl_seconds <= 86_400:
            raise ValueError("competitor market TTL is outside the safe range")
        self.redis = redis
        self.prefix = normalized
        self.ttl_seconds = ttl_seconds

    def key(self, input_digest: str) -> str:
        if re.fullmatch(r"sha256:[a-f0-9]{64}", input_digest) is None:
            raise ValueError("competitor market input digest is invalid")
        return f"{self.prefix}:competitor-market:{input_digest[7:]}"

    def _validate(self, raw: bytes | str, *, input_digest: str) -> SharedMarketSnapshot:
        try:
            envelope = SharedMarketSnapshot.model_validate_json(raw)
        except (TypeError, ValidationError, ValueError) as exc:
            raise CompetitorMarketSnapshotError() from exc
        if envelope.input_digest != input_digest:
            raise CompetitorMarketSnapshotError()
        if request_digest(envelope.snapshot.model_dump(mode="json")) != envelope.snapshot_checksum:
            raise CompetitorMarketSnapshotError()
        return envelope

    async def get(
        self,
        *,
        input_digest: str,
        now: datetime,
    ) -> SharedMarketSnapshot | None:
        key = self.key(input_digest)
        raw = await self.redis.get(key)
        if raw is None:
            return None
        envelope = self._validate(raw, input_digest=input_digest)
        if envelope.expires_at <= now:
            await self.redis.delete(key)
            return None
        return envelope

    async def save(
        self,
        *,
        input_digest: str,
        snapshot: SerpMarketSnapshot,
        now: datetime,
    ) -> SharedMarketSnapshot:
        existing = await self.get(input_digest=input_digest, now=now)
        if existing is not None:
            return existing
        envelope = SharedMarketSnapshot(
            schema_version="competitor_shared_market_v1",
            snapshot_id=uuid5(NAMESPACE_URL, f"searchtrust:v22:{input_digest}"),
            source_job_id=snapshot.job_id,
            input_digest=input_digest,
            snapshot_checksum=request_digest(snapshot.model_dump(mode="json")),
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            snapshot=snapshot,
        )
        created = await self.redis.set(
            self.key(input_digest),
            canonical_json_bytes(envelope.model_dump(mode="json")),
            ex=self.ttl_seconds,
            nx=True,
        )
        if created:
            return envelope
        canonical = await self.get(input_digest=input_digest, now=now)
        if canonical is None:
            raise CompetitorMarketSnapshotError()
        return canonical
