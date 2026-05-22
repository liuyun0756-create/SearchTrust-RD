"""
app/models/request.py
─────────────────────
Pydantic request models for the SEO Trust Path Analysis API.
"""

from __future__ import annotations

import ipaddress
import socket
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# SSRF protection — block private / reserved IP ranges
# ─────────────────────────────────────────────────────────────────────────────

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),          # 内网 A 类
    ipaddress.ip_network("172.16.0.0/12"),        # 内网 B 类
    ipaddress.ip_network("192.168.0.0/16"),       # 内网 C 类
    ipaddress.ip_network("127.0.0.0/8"),          # 本机回环
    ipaddress.ip_network("169.254.0.0/16"),       # AWS/GCP 元数据服务
    ipaddress.ip_network("100.64.0.0/10"),        # 运营商共享地址
    ipaddress.ip_network("::1/128"),              # IPv6 回环
    ipaddress.ip_network("fc00::/7"),             # IPv6 内网
    ipaddress.ip_network("fe80::/10"),            # IPv6 链路本地
]

_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",                  # GCP 元数据
    "metadata.google",
}


def _is_ssrf_safe(url: str) -> bool:
    """
    Return True only when the URL is safe to fetch from the server side.
    Blocks private IPs, loopback, link-local, and cloud metadata endpoints.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return False

    # 直接命中黑名单主机名
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        return False

    # 如果是 IP 地址，检查是否在保留范围内
    try:
        ip = ipaddress.ip_address(hostname)
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                return False
        return True
    except ValueError:
        pass  # 不是 IP，是域名

    # 域名解析后检查（防止 DNS rebinding）
    try:
        resolved = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in resolved:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                for net in _BLOCKED_NETWORKS:
                    if ip in net:
                        return False
            except ValueError:
                continue
    except socket.gaierror:
        pass  # DNS 解析失败，交由后续抓取处理

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations — values must match Dify workflow input options exactly
# ─────────────────────────────────────────────────────────────────────────────

class PageType(str, Enum):
    """Page type classification — English values from frontend.

    Each value is automatically mapped to the Chinese string expected
    by the Dify workflow via resolve_page_type().
    """

    ENTITY_DESTINATION  = "Entity-Destination Page"
    VENUE_PAGE          = "Venue Page"
    EVENT_CALENDAR      = "Event-Calendar Page"
    EVENTS_PAGE         = "Events Page"
    MENU_PAGE           = "Menu Page"
    PRODUCT_PAGE        = "Product Page"
    INVENTORY_LIST      = "Inventory-List Page"
    SERVICE_PAGE        = "Service Page"
    SERVICE_AREA_PAGE   = "Service-Area Page"
    LANDING_PAGE        = "Landing Page"
    ABOUT_US_PAGE       = "About-Us Page"
    TEAM_PAGE           = "Team Page"
    CONTACT_US_PAGE     = "Contact-Us Page"
    CITY_PAGE           = "City Page"
    BLOG_PAGE           = "Blog Page"
    BLOG_POST           = "Blog Post"
    FAQ_PAGE            = "FAQ Page"
    QA_PAGE             = "Q&A Page"
    CATEGORY_PAGE       = "Category Page"
    TAG_PAGE            = "Tag Page"
    INDEX_PAGE          = "Index Page"


# Mapping from English page_type values → Dify Chinese values.
# Chinese values not listed here are passed through as-is (see resolve_page_type).
_PAGE_TYPE_EN_TO_ZH: dict[str, str] = {
    "entity-destination page": "实体目的地",
    "venue page":              "场馆页",
    "event-calendar page":     "活动日历",
    "events page":             "Events 页面",
    "menu page":               "菜单",
    "product page":            "商品",
    "inventory-list page":     "库存列表页",
    "service page":            "本地服务落地页",
    "service-area page":       "服务总览",
    "landing page":            "分类页",
    "about-us page":           "关于我们",
    "team page":               "团队介绍",
    "contact-us page":         "联系我们",
    "city page":               "门店信息",
    "blog page":               "博客",
    "blog post":               "文章",
    "faq page":                "指南 FAQ",
    "q&a page":                "问答页",
    "category page":           "分类",
    "tag page":                "标签",
    "index page":              "索引页",
}


def resolve_page_type(page_type: str) -> str:
    """
    Resolve a page_type value to the Chinese string expected by Dify.

    If the value is already a Chinese string (passed directly), it is
    returned as-is. English values are mapped via _PAGE_TYPE_EN_TO_ZH.
    """
    return _PAGE_TYPE_EN_TO_ZH.get(page_type.lower(), page_type)


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
        examples=[PageType.SERVICE_PAGE],
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
        if not _is_ssrf_safe(v):
            raise ValueError("URL points to a private or reserved address")
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
