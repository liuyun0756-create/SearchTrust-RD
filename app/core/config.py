"""
app/core/config.py
──────────────────
Centralised configuration management via Pydantic BaseSettings.
All values can be overridden with environment variables or a .env file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
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

    # ── Dify ──────────────────────────────────────────────────────────────────
    DIFY_API_KEY: str = Field(default="", description="Dify application API key")
    DIFY_API_URL: AnyHttpUrl = Field(default="https://api.dify.ai/v1", description="Base URL for Dify API")
    DIFY_WORKFLOW_ID: str = Field(default="", description="Dify workflow ID")

    # ── SerpAPI ───────────────────────────────────────────────────────────────
    SERPAPI_KEY: str = Field(default="", description="SerpAPI key")
    SERPAPI_KEY_SECONDARY: str = Field(
        default="",
        description="Secondary SerpAPI key used after the primary key is unavailable",
    )
    SERPAPI_KEY_TERTIARY: str = Field(
        default="",
        description="Tertiary SerpAPI key used after the primary and secondary keys are unavailable",
    )
    SERPAPI_BASE_URL: str = "https://serpapi.com/search"

    # ── Address AI fallback ─────────────────────────────────────────────────
    ADDRESS_AI_ENABLED: bool = Field(
        default=False,
        description="Use an OpenAI-compatible model to confirm incomplete address candidates",
    )
    ADDRESS_AI_API_KEY: str = Field(default="", description="Address AI provider API key")
    ADDRESS_AI_BASE_URL: str = Field(default="", description="OpenAI-compatible API base URL")
    ADDRESS_AI_MODEL: str = Field(default="", description="Address AI model name")
    ADDRESS_AI_TIMEOUT: Annotated[int, Field(ge=3, le=60)] = Field(default=20)

    # ── Concurrency ───────────────────────────────────────────────────────────
    MAX_CONCURRENT_REQUESTS: Annotated[int, Field(ge=1, le=100)] = Field(
        default=10,
        description="Maximum number of concurrent Dify requests",
    )

    # ── Scraper ───────────────────────────────────────────────────────────────
    SCRAPER_TIMEOUT: Annotated[int, Field(ge=5, le=120)] = Field(default=30)
    SCRAPER_RETRY: Annotated[int, Field(ge=0, le=10)] = Field(default=3)
    JINA_BASE_URL: str = "https://r.jina.ai"
    JINA_API_KEY: str = Field(default="")
    FIRECRAWL_API_KEY: str = Field(default="")
    FIRECRAWL_API_URL: str = Field(default="https://api.firecrawl.dev/v1")
    SCRAPER_MIN_CONTENT_LENGTH: int = Field(default=300)

    # ── Dify streaming ────────────────────────────────────────────────────────
    DIFY_STREAM_TIMEOUT: Annotated[int, Field(ge=60, le=1800)] = Field(default=1200)
    DIFY_RETRY: Annotated[int, Field(ge=0, le=5)] = Field(default=3)

    # ── Task progress streaming ───────────────────────────────────────────────
    TASK_STREAM_TIMEOUT: Annotated[int, Field(ge=60, le=3600)] = Field(default=1260)
    TASK_STREAM_HEARTBEAT_INTERVAL: Annotated[int, Field(ge=5, le=60)] = Field(default=20)

    # ── v2.2 durable jobs ────────────────────────────────────────────────────
    # The v2 runtime is deliberately disabled until the v2.2 generation
    # pipeline is complete. Empty connection/auth values keep the legacy v1
    # process bootable when Redis has not been provisioned.
    V22_ANALYZE_ENABLED: bool = False
    V22_REDIS_URL: SecretStr = Field(default="", repr=False)
    V22_REDIS_PREFIX: str = Field(default="searchtrust:v22", min_length=1, max_length=100)
    V22_QUEUE_NAME: str = Field(default="searchtrust:v22:queue", min_length=1, max_length=100)
    V22_INTERNAL_API_TOKEN: SecretStr = Field(default="", repr=False)
    V22_CALLBACK_URL: str = Field(default="")
    V22_CALLBACK_SECRET: SecretStr = Field(default="", repr=False)
    V22_CALLBACK_CLOCK_SKEW_SECONDS: Annotated[int, Field(ge=30, le=900)] = 300
    V22_CALLBACK_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=30)] = 5
    V22_JOB_MAX_ATTEMPTS: Annotated[int, Field(ge=1, le=10)] = 3
    V22_WORKER_CONCURRENCY: Annotated[int, Field(ge=1, le=100)] = 5
    V22_JOB_TIMEOUT_SECONDS: Annotated[int, Field(ge=60, le=7200)] = 3600
    V22_JOB_STATE_TTL_SECONDS: Annotated[int, Field(ge=3600, le=2592000)] = 604800
    V22_JOB_HEARTBEAT_SECONDS: Annotated[int, Field(ge=5, le=300)] = 30
    V22_JOB_STALE_SECONDS: Annotated[int, Field(ge=30, le=3600)] = 180

    # ── v2.2 preflight ──────────────────────────────────────────────────────
    V22_PREFLIGHT_ENABLED: bool = False
    V22_PREFLIGHT_CACHE_TTL_SECONDS: Annotated[int, Field(ge=60, le=3600)] = 900
    V22_PREFLIGHT_CONNECT_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=30)] = 5
    V22_PREFLIGHT_READ_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=60)] = 10
    V22_PREFLIGHT_TOTAL_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=120)] = 15
    V22_PREFLIGHT_MAX_REDIRECTS: Annotated[int, Field(ge=0, le=10)] = 3
    V22_PREFLIGHT_MAX_RESPONSE_BYTES: Annotated[int, Field(ge=65536, le=10_000_000)] = 2_000_000
    PAGESPEED_API_KEY: SecretStr = Field(default="", repr=False)

    # ── v2.2 site inventory ─────────────────────────────────────────────────
    V22_SITE_INVENTORY_CONCURRENCY: Annotated[int, Field(ge=1, le=20)] = 10
    V22_SITE_INVENTORY_REQUESTS_PER_SECOND: Annotated[int, Field(ge=1, le=20)] = 5
    V22_SITE_INVENTORY_CONNECT_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=30)] = 5
    V22_SITE_INVENTORY_READ_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=60)] = 15
    V22_SITE_INVENTORY_TOTAL_TIMEOUT_SECONDS: Annotated[int, Field(ge=1, le=120)] = 30
    V22_SITE_INVENTORY_MAX_REDIRECTS: Annotated[int, Field(ge=0, le=10)] = 5
    V22_SITE_INVENTORY_STRUCTURAL_BYTES: Annotated[int, Field(ge=65_536, le=1_000_000)] = 256_000
    V22_SITE_INVENTORY_DEEP_BYTES: Annotated[int, Field(ge=262_144, le=5_000_000)] = 2_000_000
    V22_SITE_INVENTORY_SITEMAP_BYTES: Annotated[int, Field(ge=65_536, le=5_000_000)] = 2_000_000
    V22_SITE_INVENTORY_SITEMAP_DECOMPRESSED_BYTES: Annotated[
        int, Field(ge=262_144, le=50_000_000)
    ] = 10_000_000
    V22_SITE_INVENTORY_SITEMAP_INDEX_DEPTH: Annotated[int, Field(ge=0, le=5)] = 2
    V22_SITE_INVENTORY_SITEMAP_FILES: Annotated[int, Field(ge=1, le=100)] = 20
    V22_SITE_INVENTORY_BATCH_SIZE: Annotated[int, Field(ge=1, le=100)] = 25
    V22_SITE_INVENTORY_FIRECRAWL_ENABLED: bool = True

    # ── Dify RPM token bucket (in-process) ───────────────────────────────────
    DIFY_RPM_CAPACITY: Annotated[int, Field(ge=1)] = Field(default=60)
    DIFY_RPM_REFILL: Annotated[int, Field(ge=1)] = Field(default=60)
    DIFY_RPM_INTERVAL: Annotated[int, Field(ge=1)] = Field(default=60)

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = Field(
        default=[
            "https://trysearchtrust.com",
            "https://www.trysearchtrust.com",
        ],
    )

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("DIFY_API_URL", "ADDRESS_AI_BASE_URL", "V22_CALLBACK_URL", mode="before")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return str(v).rstrip("/")

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def dify_api_url_str(self) -> str:
        return str(self.DIFY_API_URL)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
