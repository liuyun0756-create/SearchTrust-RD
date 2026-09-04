"""Redis keys isolated from formal v2.2 analysis jobs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class CompetitorDiscoveryRedisKeys:
    prefix: str

    def __post_init__(self) -> None:
        normalized = self.prefix.strip().strip(":")
        if not normalized:
            raise ValueError("Redis key prefix must not be empty")
        object.__setattr__(self, "prefix", f"{normalized}:competitor-discovery")

    def request(self, job_id: UUID | str) -> str:
        return f"{self.prefix}:job:{job_id}:request"

    def state(self, job_id: UUID | str) -> str:
        return f"{self.prefix}:job:{job_id}:state"

    def idempotency(self, case_id: UUID | str, idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"{self.prefix}:idem:{case_id}:{digest}"

    def events(self, job_id: UUID | str) -> str:
        return f"{self.prefix}:events:{job_id}"

    def checkpoint(self, job_id: UUID | str, checkpoint_key: str) -> str:
        digest = hashlib.sha256(checkpoint_key.encode("utf-8")).hexdigest()
        return f"{self.prefix}:job:{job_id}:checkpoint:{digest}"

    def checkpoint_pattern(self, job_id: UUID | str) -> str:
        return f"{self.prefix}:job:{job_id}:checkpoint:*"

    @property
    def active(self) -> str:
        return f"{self.prefix}:active"
