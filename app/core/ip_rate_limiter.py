"""
app/core/ip_rate_limiter.py
────────────────────────────
Per-IP rate limiting middleware backed by Redis.

Uses a sliding window counter approach:
  - Key: rate_limit:ip:{client_ip}:{minute_bucket}
  - Each request increments the counter
  - TTL = 60 seconds
  - If counter > limit, reject with 429

"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 进程内内存限流（用于轮询等低风险端点）
# ─────────────────────────────────────────────────────────────────────────────
# 结构：{ (ip, minute_bucket): count }
# 每次请求后检查旧桶并清理，避免无限增长。
_mem_counters: dict[tuple[str, int], int] = defaultdict(int)
_mem_last_cleanup: int = 0
_MEM_CLEANUP_INTERVAL = 120  # 每 120 s 清理一次过期桶


def _mem_rate_check(client_ip: str, limit: int) -> int:
    """
    进程内滑动窗口计数器（不消耗 Redis 命令）。

    Returns
    -------
    int — 当前分钟内的请求计数（含本次）
    """
    global _mem_last_cleanup

    current_minute = int(time.time() // 60)
    key = (client_ip, current_minute)
    _mem_counters[key] += 1
    count = _mem_counters[key]

    # 定期清理过期桶（超过 2 分钟的条目）
    now = int(time.time())
    if now - _mem_last_cleanup > _MEM_CLEANUP_INTERVAL:
        cutoff = current_minute - 2
        stale = [k for k in _mem_counters if k[1] < cutoff]
        for k in stale:
            del _mem_counters[k]
        _mem_last_cleanup = now

    return count


# ─────────────────────────────────────────────────────────────────────────────
# 需要 Redis 限流的端点前缀（仅高风险写操作）
# ─────────────────────────────────────────────────────────────────────────────
_REDIS_RATE_LIMITED_PATHS = {
    "/api/v1/analyze",   # POST — 提交分析任务，是真正需要跨进程保护的端点
}

# 豁免所有限流的端点
_EXEMPT_PATHS = {
    "/api/v1/health",
}


class IPRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiter.

    - POST /api/v1/analyze → Redis 滑动窗口计数（跨进程，防分布式刷接口）
    - GET  /api/v1/task/*  → 进程内内存计数（不消耗 Upstash 命令配额）
    - GET  /api/v1/health  → 完全豁免
    """

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        path = request.url.path

        # 完全豁免的端点
        if path in _EXEMPT_PATHS:
            return await call_next(request)

        client_ip = self._get_client_ip(request)

        # ── 高风险端点：Redis 限流（跨进程准确计数）─────────────────────────
        if path in _REDIS_RATE_LIMITED_PATHS:
            return await self._redis_rate_limit(request, call_next, client_ip)

        # ── 其余端点（含轮询）：内存限流（零 Redis 命令）────────────────────
        return await self._mem_rate_limit(request, call_next, client_ip)

    # ── Redis-backed limiter ──────────────────────────────────────────────────

    async def _redis_rate_limit(
        self, request: Request, call_next: Callable, client_ip: str
    ) -> Response:
        redis = get_redis()
        current_minute = int(time.time() // 60)
        rate_key = f"rate_limit:ip:{client_ip}:{current_minute}"

        try:
            count = await redis.incr_with_ttl(rate_key, ttl=60)
            if count > settings.RATE_LIMIT_PER_MINUTE:
                logger.warning(
                    "Rate limit exceeded (redis) — ip=%s count=%d limit=%d path=%s",
                    client_ip, count, settings.RATE_LIMIT_PER_MINUTE,
                    request.url.path,
                )
                return self._too_many(settings.RATE_LIMIT_PER_MINUTE, count, current_minute)
        except Exception as exc:
            # Redis 故障时放行（fail-open），避免因限流基础设施故障影响业务
            logger.error("Rate limiter Redis error: %s", exc, exc_info=True)
            return await call_next(request)

        response = await call_next(request)
        self._add_headers(response, count, current_minute)
        return response

    # ── In-memory limiter ─────────────────────────────────────────────────────

    async def _mem_rate_limit(
        self, request: Request, call_next: Callable, client_ip: str
    ) -> Response:
        # 轮询场景可适当放宽：允许比提交端点更高的频率
        # 使用 RATE_LIMIT_PER_MINUTE * 6 作为内存限流上限（默认 60 rpm → 360 rpm）
        mem_limit = settings.RATE_LIMIT_PER_MINUTE * 6
        count = _mem_rate_check(client_ip, mem_limit)
        if count > mem_limit:
            logger.warning(
                "Rate limit exceeded (memory) — ip=%s count=%d limit=%d path=%s",
                client_ip, count, mem_limit, request.url.path,
            )
            current_minute = int(time.time() // 60)
            return self._too_many(mem_limit, count, current_minute)

        response = await call_next(request)
        current_minute = int(time.time() // 60)
        self._add_headers(response, count, current_minute)
        return response

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _too_many(self, limit: int, count: int, current_minute: int) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": f"Rate limit exceeded: {limit} requests per minute",
                "code": "RATE_LIMIT_EXCEEDED",
            },
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str((current_minute + 1) * 60),
            },
        )

    def _add_headers(self, response: Response, count: int, current_minute: int) -> None:
        limit = settings.RATE_LIMIT_PER_MINUTE
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        response.headers["X-RateLimit-Reset"] = str((current_minute + 1) * 60)

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, respecting X-Forwarded-For."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        if request.client:
            return request.client.host
        return "unknown"
