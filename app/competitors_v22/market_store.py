"""Redis-backed, expiring shared v2.2 market snapshots."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from redis.asyncio import Redis

from app.collectors.serp_market_models import SerpMarketSnapshot
from app.collectors.serp_market import SERP_MARKET_PARSER_VERSION
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
        self.legacy_key(input_digest)  # Validate without changing request identity.
        return f"{self.prefix}:competitor-market:{SERP_MARKET_PARSER_VERSION}:{input_digest[7:]}"

    def legacy_key(self, input_digest: str) -> str:
        if re.fullmatch(r"sha256:[a-f0-9]{64}", input_digest) is None:
            raise ValueError("competitor market input digest is invalid")
        return f"{self.prefix}:competitor-market:{input_digest[7:]}"

    def snapshot_key(self, snapshot_id: UUID) -> str:
        return f"{self.prefix}:competitor-market:snapshot:{UUID(str(snapshot_id))}"

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

    async def _read(self, key: str, *, input_digest: str, now: datetime,
                    remove_expired: bool = False) -> SharedMarketSnapshot | None:
        raw = await self.redis.get(key)
        if raw is None:
            return None
        envelope = self._validate(raw, input_digest=input_digest)
        if envelope.expires_at <= now:
            if remove_expired:
                await self.redis.delete(key)
            return None
        return envelope

    async def get_current(self, *, input_digest: str, now: datetime) -> SharedMarketSnapshot | None:
        """New discovery may reuse only observations made by this parser."""
        return await self._read(self.key(input_digest), input_digest=input_digest, now=now,
            remove_expired=True)

    async def get(self, *, input_digest: str, now: datetime,
                  snapshot_id: UUID | None = None) -> SharedMarketSnapshot | None:
        current = await self.get_current(input_digest=input_digest, now=now)
        if snapshot_id is None:
            # A normal digest lookup never prefers a legacy capture over current.
            return current if current is not None else await self._read(
                self.legacy_key(input_digest), input_digest=input_digest, now=now)
        if current is not None and current.snapshot_id == snapshot_id:
            return current
        # An explicitly confirmed ID is an immutable source lookup, not a cache
        # preference: a new parser must not replace an already-confirmed capture.
        pinned = await self._read(self.snapshot_key(snapshot_id), input_digest=input_digest, now=now)
        if pinned is not None:
            if pinned.snapshot_id != snapshot_id:
                raise CompetitorMarketSnapshotError()
            return pinned
        legacy = await self._read(self.legacy_key(input_digest), input_digest=input_digest, now=now)
        return legacy if legacy is not None and legacy.snapshot_id == snapshot_id else None

    async def save(
        self,
        *,
        input_digest: str,
        snapshot: SerpMarketSnapshot,
        now: datetime,
    ) -> SharedMarketSnapshot:
        existing = await self.get_current(input_digest=input_digest, now=now)
        if existing is not None:
            return existing
        checksum = request_digest(snapshot.model_dump(mode="json"))
        envelope = SharedMarketSnapshot(
            schema_version="competitor_shared_market_v1",
            # The lookup key represents a market, not one observation forever.
            # A new capture (including its original job/times) gets a new ID;
            # existing cache entries above retain their exact historical identity.
            snapshot_id=uuid5(NAMESPACE_URL, f"searchtrust:v22:market:{input_digest}:{checksum}"),
            source_job_id=snapshot.job_id,
            input_digest=input_digest,
            snapshot_checksum=checksum,
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
            # Index only new captures. Legacy bytes and TTLs are never rewritten.
            await self.redis.set(self.snapshot_key(envelope.snapshot_id),
                canonical_json_bytes(envelope.model_dump(mode="json")), ex=self.ttl_seconds, nx=True)
            return envelope
        canonical = await self.get_current(input_digest=input_digest, now=now)
        if canonical is None:
            raise CompetitorMarketSnapshotError()
        return canonical
