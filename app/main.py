"""
app/main.py
───────────
FastAPI application factory.

Responsibilities
----------------
- Create and configure the FastAPI instance
- Register routers
- Configure CORS middleware
- Global exception handlers
- Startup / shutdown lifecycle hooks
"""

from __future__ import annotations

import logging
import logging.config
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

os.environ.setdefault("PYTHONUTF8", "1")

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.v2.competitors import router as v2_competitors_router
from app.api.v2.preflight import router as v2_preflight_router
from app.api.v2.runtime import close_v22_runtime, create_v22_runtime, router as v2_runtime_router
from app.competitors_v22.runtime import create_competitor_discovery_runtime
from app.core.config import settings

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
        "httpx": {"level": "WARNING", "propagate": True},
    },
}

logging.config.dictConfig(_LOG_CONFIG)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)
    runtime = None
    competitor_runtime = None
    try:
        runtime = await create_v22_runtime()
        if runtime is not None:
            competitor_runtime = create_competitor_discovery_runtime(runtime.redis)
    except Exception as exc:  # Liveness must remain available when the durable queue is down.
        logger.warning("v2.2 durable queue unavailable during startup: %s", type(exc).__name__)
    app.state.v22_runtime = runtime
    app.state.v22_competitor_runtime = competitor_runtime
    try:
        yield
    finally:
        await close_v22_runtime(runtime)
        logger.info("Shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="SearchTrust V2.2 SEO evidence collection and reporting service.",
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
        openapi_url="/openapi.json" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(health_router)
    app.include_router(v2_preflight_router)
    app.include_router(v2_runtime_router)
    app.include_router(v2_competitors_router)

    # ── Global exception handlers ─────────────────────────────────────────────
    _register_exception_handlers(app)

    return app


def _register_exception_handlers(app: FastAPI) -> None:

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
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": detail, "code": "VALIDATION_ERROR"},
        )

    @app.exception_handler(500)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled 500 error — %s %s: %s",
            request.method, request.url.path, exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error — please try again later.", "code": "INTERNAL_ERROR"},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled exception — %s %s: %s",
            request.method, request.url.path, exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An unexpected error occurred.", "code": "UNEXPECTED_ERROR"},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level app instance
# ─────────────────────────────────────────────────────────────────────────────
app: FastAPI = create_app()
