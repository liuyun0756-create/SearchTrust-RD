"""
app/main.py
───────────
FastAPI application factory.

Responsibilities
----------------
- Create and configure the FastAPI instance
- Register routers
- Configure CORS middleware
- Register slowapi rate-limit middleware
- Global exception handlers
- Startup / shutdown lifecycle hooks (Redis pool, logging)
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Fix Windows GBK encoding issue with slowapi/starlette reading .env
os.environ.setdefault("PYTHONUTF8", "1")

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.analyze import router as analyze_router
from app.core.config import settings
from app.core.ip_rate_limiter import IPRateLimitMiddleware
from app.core.redis_client import close_pool, init_redis

# ─────────────────────────────────────────────────────────────────────────────
# Logging configuration
# ─────────────────────────────────────────────────────────────────────────────
_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
        "json": {
            # Structured JSON output — swap in production if feeding to a log aggregator
            "format": '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
            "datefmt": "%Y-%m-%dT%H:%M:%SZ",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "standard",
            "level": "DEBUG",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "DEBUG" if settings.DEBUG else "INFO",
    },
    "loggers": {
        "uvicorn": {"level": "INFO", "propagate": True},
        "uvicorn.error": {"level": "INFO", "propagate": True},
        "uvicorn.access": {"level": "WARNING", "propagate": True},
        "celery": {"level": "INFO", "propagate": True},
        "httpx": {"level": "WARNING", "propagate": True},
        "hiredis": {"level": "WARNING", "propagate": True},
    },
}

logging.config.dictConfig(_LOG_CONFIG)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan context manager (startup / shutdown)
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan handler.

    Startup:
        - Initialise Redis connection pool and verify connectivity.
        - Attach the rate-limiter to app state (required by slowapi).

    Shutdown:
        - Gracefully drain and close the Redis connection pool.
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)

    try:
        await init_redis()
        logger.info("Redis connected — %s", settings.REDIS_URL)
    except RuntimeError as exc:
        logger.critical("Redis connection failed at startup: %s", exc)
        sys.exit(1)

    logger.info("Rate limiter attached — %d req/min per IP", settings.RATE_LIMIT_PER_MINUTE)

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down — closing Redis pool…")
    await close_pool()
    logger.info("Shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Create and fully configure the FastAPI application."""

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "Enterprise-grade SEO trust-path analysis service. "
            "Submit a URL + page type, get a comprehensive AI-powered SEO report."
        ),
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
        openapi_url="/openapi.json" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── Rate limiting (Redis-backed per-IP middleware) ─────────────────────────
    # Replaces slowapi which has a bug on Windows where SlowAPIMiddleware
    # silently returns 500 for all requests. This middleware uses Redis
    # INCR + EXPIRE for a sliding window counter — works on all platforms.
    app.add_middleware(IPRateLimitMiddleware)

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(analyze_router)

    # ── Global exception handlers ─────────────────────────────────────────────
    _register_exception_handlers(app)

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Attach global exception handlers for common error scenarios."""

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "Endpoint not found", "code": "NOT_FOUND"},
        )

    @app.exception_handler(405)
    async def method_not_allowed_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            content={"detail": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
        )

    @app.exception_handler(422)
    async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc), "code": "VALIDATION_ERROR"},
        )

    @app.exception_handler(500)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled 500 error — %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "Internal server error — please try again later.",
                "code": "INTERNAL_ERROR",
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled exception — %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "An unexpected error occurred.",
                "code": "UNEXPECTED_ERROR",
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level app instance (used by uvicorn / Celery)
# ─────────────────────────────────────────────────────────────────────────────
app: FastAPI = create_app()
