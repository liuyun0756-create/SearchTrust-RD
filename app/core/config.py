"""
app/core/config.py
──────────────────
Centralised configuration management via Pydantic BaseSettings.
All values can be overridden with environment variables or a .env file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, field_validator
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
    SERPAPI_BASE_URL: str = "https://serpapi.com/search"

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
    DIFY_STREAM_TIMEOUT: int = Field(default=300)
    DIFY_RETRY: Annotated[int, Field(ge=0, le=5)] = Field(default=3)

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
    @field_validator("DIFY_API_URL", mode="before")
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
