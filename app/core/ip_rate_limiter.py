"""
app/core/ip_rate_limiter.py
────────────────────────────
Per-IP rate limiting middleware backed by Redis.

Uses a sliding window counter approach:
  - Key: rate_limit:ip:{client_ip}:{minute_bucket}
  - Each request increments the counter
  - TTL = 60 seconds
  - If counter > limit, reject with 429

This replaces slowapi which has known bugs on Windows.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)


class IPRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiter using Redis sliding window counters.

    Applies to all requests. Configure via settings.RATE_LIMIT_PER_MINUTE.
    """

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        # Skip rate limiting for health check endpoint
        if request.url.path == "/api/v1/health":
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        redis = get_redis()

        # Sliding window: use current minute as bucket
        current_minute = int(time.time() // 60)
        rate_key = f"rate_limit:ip:{client_ip}:{current_minute}"

        try:
            # Atomically increment counter and set TTL in one pipeline round-trip.
            # Using separate incr() + expire() calls has a race condition: if the
            # process crashes between the two commands the key never expires,
            # permanently banning the IP.
            count = await redis.incr_with_ttl(rate_key, ttl=60)

            # Check limit
            if count > settings.RATE_LIMIT_PER_MINUTE:
                logger.warning(
                    "Rate limit exceeded — ip=%s count=%d limit=%d",
                    client_ip,
                    count,
                    settings.RATE_LIMIT_PER_MINUTE,
                )
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={
                        "detail": f"Rate limit exceeded: {settings.RATE_LIMIT_PER_MINUTE} requests per minute",
                        "code": "RATE_LIMIT_EXCEEDED",
                    },
                )

            # Allow request
            response = await call_next(request)

            # Add rate limit headers
            response.headers["X-RateLimit-Limit"] = str(settings.RATE_LIMIT_PER_MINUTE)
            response.headers["X-RateLimit-Remaining"] = str(
                max(0, settings.RATE_LIMIT_PER_MINUTE - count)
            )
            response.headers["X-RateLimit-Reset"] = str((current_minute + 1) * 60)

            return response

        except Exception as exc:
            # On Redis failure, allow request through (fail-open)
            logger.error("Rate limiter Redis error: %s", exc, exc_info=True)
            return await call_next(request)

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, respecting X-Forwarded-For."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # X-Forwarded-For can be: "client, proxy1, proxy2"
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()

        # Fallback to direct connection IP
        if request.client:
            return request.client.host

        return "unknown"
