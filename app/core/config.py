"""
app/core/config.py
──────────────────
Centralised configuration management via Pydantic BaseSettings.
All values can be overridden with environment variables or a .env file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Project meta ─────────────────────────────────────────────────────────
    APP_NAME: str = "SEO Trust Path Analysis Service"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL used for cache and Celery broker/backend",
    )

    # ── Dify ──────────────────────────────────────────────────────────────────
    DIFY_API_KEY: str = Field(
        default="",
        description="Dify application API key (Bearer token)",
    )
    DIFY_API_URL: AnyHttpUrl = Field(
        default="https://api.dify.ai/v1",
        description="Base URL for Dify API endpoints",
    )
    DIFY_WORKFLOW_ID: str = Field(
        default="",
        description="Dify workflow ID to invoke for SEO analysis",
    )

    # ── SerpAPI ───────────────────────────────────────────────────────────────
    SERPAPI_KEY: str = Field(
        default="",
        description="SerpAPI key for Google Maps / Business Profile lookups",
    )
    SERPAPI_BASE_URL: str = "https://serpapi.com/search"

    # ── Concurrency / rate limiting ───────────────────────────────────────────
    MAX_CONCURRENT_REQUESTS: Annotated[int, Field(ge=1, le=100)] = Field(
        default=10,
        description="Maximum number of concurrent LLM (Dify) requests",
    )
    RATE_LIMIT_PER_MINUTE: Annotated[int, Field(ge=1)] = Field(
        default=10,
        description="Per-IP request rate limit (requests per minute)",
    )

    # ── Scraper ───────────────────────────────────────────────────────────────
    SCRAPER_TIMEOUT: Annotated[int, Field(ge=5, le=120)] = Field(
        default=30,
        description="HTTP timeout in seconds for Jina Reader requests",
    )
    SCRAPER_RETRY: Annotated[int, Field(ge=0, le=10)] = Field(
        default=3,
        description="Number of retry attempts for failed scraper requests",
    )
    JINA_BASE_URL: str = "https://r.jina.ai"
    JINA_API_KEY: str = Field(
        default="",
        description="Jina Reader API key — leave empty to use the free tier",
    )

    # ── Firecrawl (fallback scraper) ──────────────────────────────────────────
    FIRECRAWL_API_KEY: str = Field(
        default="",
        description="Firecrawl API key — used as fallback when Jina fails",
    )
    FIRECRAWL_API_URL: str = Field(
        default="https://api.firecrawl.dev/v1",
        description="Firecrawl base URL",
    )
    SCRAPER_MIN_CONTENT_LENGTH: int = Field(
        default=300,
        description="Minimum character length for scraped content to be considered valid",
    )

    # ── Cache TTLs ────────────────────────────────────────────────────────────
    TASK_RESULT_TTL: Annotated[int, Field(ge=60)] = Field(
        default=1800,
        description="Redis TTL (seconds) for in-progress task state (30 min)",
    )
    TASK_DONE_TTL: Annotated[int, Field(ge=60)] = Field(
        default=3600,
        description="Redis TTL (seconds) for completed/failed task results (1 h)",
    )
    SCRAPER_CACHE_TTL: Annotated[int, Field(ge=60)] = Field(
        default=3600,
        description="Redis TTL (seconds) for scraped page content (1 h)",
    )
    REPORT_CACHE_TTL: Annotated[int, Field(ge=60)] = Field(
        default=86400,
        description="Redis TTL (seconds) for full analysis reports (24 h)",
    )

    # ── Dify streaming ────────────────────────────────────────────────────────
    DIFY_STREAM_TIMEOUT: int = Field(
        default=120,
        description="Maximum seconds to wait for a Dify streaming response",
    )
    DIFY_RETRY: Annotated[int, Field(ge=0, le=5)] = Field(
        default=3,
        description="Number of retry attempts for Dify workflow calls",
    )

    # ── Dify global rate limiting (token bucket) ─────────────────────────────
    # Tune to match your actual OpenAI / Dify RPM quota.
    #
    # Example — OpenAI GPT-4o Tier 1 (500 RPM):
    #   DIFY_RPM_CAPACITY=100  DIFY_RPM_REFILL=500  DIFY_RPM_INTERVAL=60
    DIFY_RPM_CAPACITY: Annotated[int, Field(ge=1)] = Field(
        default=60,
        description="Token bucket capacity — maximum burst size for Dify calls",
    )
    DIFY_RPM_REFILL: Annotated[int, Field(ge=1)] = Field(
        default=60,
        description="Tokens refilled per DIFY_RPM_INTERVAL seconds",
    )
    DIFY_RPM_INTERVAL: Annotated[int, Field(ge=1)] = Field(
        default=60,
        description="Refill interval in seconds (usually 60 for RPM buckets)",
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = Field(
        default=["*"],
        description="Allowed CORS origins",
    )

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("DIFY_API_URL", mode="before")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return str(v).rstrip("/")

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def validate_redis_url(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must start with redis://, rediss://, or unix://")
        return v

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def celery_broker_url(self) -> str:
        """Celery broker URL (Redis DB 0)."""
        return self.REDIS_URL

    @property
    def celery_result_backend(self) -> str:
        """Celery result backend URL — same DB as broker (DB 0).
        Upstash free tier only supports DB 0, so we use key prefixes
        to separate broker and result data instead of separate DBs.
        """
        parts = self.REDIS_URL.rsplit("/", 1)
        return f"{parts[0]}/0"

    @property
    def dify_api_url_str(self) -> str:
        return str(self.DIFY_API_URL)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


# Module-level convenience alias
settings: Settings = get_settings()
