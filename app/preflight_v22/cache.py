"""Best-effort Redis cache for frozen v2.2 preflight responses."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import orjson
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.api.v2.models import PreflightRequest, PreflightResponse
from app.report_v22.contract_version import CONTRACT_VERSION


logger = logging.getLogger(__name__)


class PreflightCache:
    CACHE_VERSION = "v1"

    def __init__(self, redis: Any | None, *, prefix: str, ttl_seconds: int) -> None:
        self.redis = redis
        self.prefix = prefix.strip().strip(":")
        if not self.prefix:
            raise ValueError("Redis key prefix must not be empty")
        self.ttl_seconds = ttl_seconds

    def key(self, request: PreflightRequest, normalized_site_url: str) -> str:
        payload = request.model_dump(mode="json")
        payload["site_url"] = normalized_site_url
        canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
        digest = hashlib.sha256(canonical).hexdigest()
        return (
            f"{self.prefix}:preflight:{self.CACHE_VERSION}:"
            f"{CONTRACT_VERSION}:{digest}"
        )

    async def get(self, key: str) -> PreflightResponse | None:
        if self.redis is None:
            return None
        try:
            value = await self.redis.get(key)
            if value is None:
                return None
            return PreflightResponse.model_validate_json(value)
        except (RedisError, OSError, ValidationError, ValueError, TypeError):
            logger.warning("v2.2 preflight cache read was unavailable or invalid")
            return None

    async def set(self, key: str, response: PreflightResponse) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(key, response.model_dump_json(), ex=self.ttl_seconds)
        except (RedisError, OSError, ValueError, TypeError):
            logger.warning("v2.2 preflight cache write was unavailable")
