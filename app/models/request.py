"""
app/models/request.py
─────────────────────
Pydantic request models for the SEO Trust Path Analysis API.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations — values must match Dify workflow input options exactly
# ─────────────────────────────────────────────────────────────────────────────

class PageType(str, Enum):
    """Page type classification — matches Dify workflow page_type options."""

    ENTITY_DESTINATION  = "实体目的地"
    VENUE               = "场馆页"
    EVENT_CALENDAR      = "活动日历"
    EVENTS_PAGE         = "Events 页面"
    MENU                = "菜单"
    PRODUCT             = "商品"
    INVENTORY_LIST      = "库存列表页"
    LOCAL_SERVICE       = "本地服务落地页"
    SERVICE_OVERVIEW    = "服务总览"
    CATEGORY_PAGE       = "分类页"
    ABOUT_US            = "关于我们"
    TEAM_INTRO          = "团队介绍"
    CONTACT_US          = "联系我们"
    STORE_INFO          = "门店信息"
    BLOG                = "博客"
    ARTICLE             = "文章"
    GUIDE_FAQ           = "指南 FAQ"
    QA_PAGE             = "问答页"
    CATEGORY            = "分类"
    TAG                 = "标签"
    INDEX_PAGE          = "索引页"


class Language(str, Enum):
    """Supported analysis output languages."""

    CHINESE = "中文"
    ENGLISH = "English"
    BOTH    = "Both"


# ─────────────────────────────────────────────────────────────────────────────
# Request model
# ─────────────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    """
    Payload for submitting an SEO trust path analysis job.

    Attributes
    ----------
    url:
        The fully-qualified URL of the page to analyse.
    page_type:
        Semantic classification of the page (must match Dify workflow options).
    language:
        Desired language for the generated report.
    gbp_url:
        Optional. The website URL registered in Google Business Profile.
        Used to query GBP data via domain matching (more accurate than
        name+city search). Defaults to the same value as `url` if not provided.
    """

    url: HttpUrl = Field(
        ...,
        description="Fully-qualified URL of the page to analyse",
        examples=["https://example.com/service"],
    )
    page_type: PageType = Field(
        ...,
        description="Page type — must match one of the 21 Dify workflow options",
        examples=[PageType.LOCAL_SERVICE],
    )
    language: Language = Field(
        default=Language.ENGLISH,
        description="Desired language for the generated SEO report",
    )
    gbp_url: Optional[str] = Field(
        default=None,
        description=(
            "Website URL registered in Google Business Profile. "
            "Used for domain-based GBP lookup. "
            "If omitted, falls back to the homepage of `url`."
        ),
        examples=["https://nxtlvlautospa.com"],
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("url", mode="before")
    @classmethod
    def normalise_url(cls, v: str) -> str:
        """Strip trailing whitespace and ensure the URL has a scheme."""
        v = str(v).strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("gbp_url", mode="before")
    @classmethod
    def normalise_gbp_url(cls, v: Optional[str]) -> Optional[str]:
        """Normalise gbp_url if provided."""
        if v is None:
            return None
        v = str(v).strip()
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "url": "https://www.example.com/service",
                "page_type": "本地服务落地页",
                "language": "English",
                "gbp_url": "https://www.example.com",
            }
        }
    }
