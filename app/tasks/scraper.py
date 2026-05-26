"""
app/tasks/scraper.py
────────────────────
Dual-layer scraper: Jina Reader (primary) → Firecrawl (fallback).

Public API
----------
scrape(url)              — Main entry point
fetch_page_content(url)  — Waterfall: Jina → Firecrawl
fetch_gbp_data(...)      — SerpAPI Google Maps / GBP lookup
extract_business_info()  — Regex heuristics to pull name / city / phone

Scraper levels
--------------
1. Jina Reader  — free, fast, clean Markdown output
2. Firecrawl    — paid-per-call, stronger JS rendering, reliable last resort

Sub-page scraping
-----------------
Main page and predicted sub-pages (contact/about) are fetched concurrently
in a single asyncio.gather() call. Sub-page URLs are derived from the
base domain using common path conventions — no need to parse the main page
first. Results are appended with the same === PAGE === / === END PAGE ===
separator format used by the original Dify web_scraper node.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_USER_AGENTS: list[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
]

# HTTP status codes that are unrecoverable — no point retrying
_NO_RETRY_CODES: frozenset[int] = frozenset({400, 401, 403, 404, 410})

# Content that looks like a successful HTTP 200 but is actually an error page
_FAILURE_KEYWORDS: tuple[str, ...] = (
    "access denied",
    "403 forbidden",
    "captcha",
    "just a moment",        # Cloudflare challenge
    "enable javascript",
    "browser check",
    "ddos protection",
    "verify you are human",
    "ray id",               # Cloudflare ray-id footer
)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

class ScraperSource(str, Enum):
    JINA = "jina"
    FIRECRAWL = "firecrawl"


@dataclass
class ScrapeResult:
    """Successful scrape outcome with metadata."""

    content: str
    source: ScraperSource
    elapsed: float          # wall-clock seconds for this level
    content_length: int


# ─────────────────────────────────────────────────────────────────────────────
# Content quality gate
# ─────────────────────────────────────────────────────────────────────────────

def _is_valid_content(text: str) -> bool:
    """
    Return True only when the scraped text passes both length and
    anti-pattern checks.

    A response that is technically HTTP 200 but contains a Cloudflare
    challenge page or an access-denied message is treated as a failure.
    """
    min_len = settings.SCRAPER_MIN_CONTENT_LENGTH
    if not text or len(text) < min_len:
        logger.debug("Content too short: %d < %d chars", len(text) if text else 0, min_len)
        return False
    text_lower = text.lower()
    for kw in _FAILURE_KEYWORDS:
        if kw in text_lower:
            logger.warning("Content contains failure signal: %r", kw)
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Level 1 — Jina Reader
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_jina(url: str) -> Optional[str]:
    """
    Fetch page content via Jina Reader (https://r.jina.ai/<url>).

    Retry behaviour
    ---------------
    - 403 / 404 / other _NO_RETRY_CODES → return None immediately
    - 429 rate-limited                  → honour Retry-After header, then retry
    - timeout / connection error        → exponential back-off retry
    - invalid content quality           → retry (site might be loading)
    """
    jina_url = f"{settings.JINA_BASE_URL}/{url}"
    headers: dict[str, str] = {
        "Accept": "text/plain",
        "User-Agent": random.choice(_USER_AGENTS),
        "X-Return-Format": "markdown",
        "X-Timeout": str(settings.SCRAPER_TIMEOUT),
    }
    if settings.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {settings.JINA_API_KEY}"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(float(settings.SCRAPER_TIMEOUT + 10)),
        follow_redirects=True,
    ) as client:
        for attempt in range(1, settings.SCRAPER_RETRY + 1):
            if attempt > 1:
                wait = 2 ** (attempt - 1)   # 2 s, 4 s
                logger.info("[Jina] retry in %ds (attempt %d/%d)", wait, attempt, settings.SCRAPER_RETRY)
                await asyncio.sleep(wait)

            try:
                resp = await client.get(jina_url, headers=headers)

                if resp.status_code in _NO_RETRY_CODES:
                    logger.warning("[Jina] unrecoverable status=%d url=%s", resp.status_code, url)
                    return None

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "30"))
                    logger.warning("[Jina] rate-limited; waiting %ds url=%s", retry_after, url)
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code != 200:
                    logger.warning("[Jina] status=%d attempt=%d url=%s", resp.status_code, attempt, url)
                    continue

                if _is_valid_content(resp.text):
                    logger.info(
                        "[Jina] success attempt=%d len=%d url=%s",
                        attempt, len(resp.text), url,
                    )
                    return resp.text

                logger.warning("[Jina] content invalid len=%d attempt=%d url=%s", len(resp.text), attempt, url)

            except httpx.TimeoutException:
                logger.warning("[Jina] timeout attempt=%d url=%s", attempt, url)
            except httpx.ConnectError as exc:
                logger.warning("[Jina] connect error attempt=%d url=%s: %s", attempt, url, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Jina] unexpected error attempt=%d url=%s: %s", attempt, url, exc)

    logger.error("[Jina] all %d attempts failed url=%s", settings.SCRAPER_RETRY, url)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Level 2 — Firecrawl
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_firecrawl(url: str) -> Optional[str]:
    """
    Fetch page content via Firecrawl (https://docs.firecrawl.dev).

    Firecrawl is a paid-per-call service; it is only invoked after Jina fails.
    Returns None if FIRECRAWL_API_KEY is not configured.
    """
    if not settings.FIRECRAWL_API_KEY:
        logger.info("[Firecrawl] API key not configured — skipping")
        return None

    endpoint = f"{settings.FIRECRAWL_API_URL}/scrape"
    headers: dict[str, str] = {
        "Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": False,       # 保留评论区等动态内容
        "waitFor": 5000,                # 等待 5 秒让评论/JS 内容加载完
        "timeout": settings.SCRAPER_TIMEOUT * 1000,
        "actions": [
            {"type": "scroll", "direction": "down", "amount": 500},  # 滚动触发懒加载
            {"type": "wait", "milliseconds": 2000},                   # 等待内容渲染
            {"type": "scroll", "direction": "down", "amount": 500},  # 继续滚动
            {"type": "wait", "milliseconds": 1000},                   # 再等一秒
        ],
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(float(settings.SCRAPER_TIMEOUT + 15)),
        follow_redirects=True,
    ) as client:
        for attempt in range(1, settings.SCRAPER_RETRY + 1):
            if attempt > 1:
                wait = 2 ** (attempt - 1)
                logger.info("[Firecrawl] retry in %ds (attempt %d/%d)", wait, attempt, settings.SCRAPER_RETRY)
                await asyncio.sleep(wait)

            try:
                resp = await client.post(endpoint, headers=headers, json=payload)

                if resp.status_code in _NO_RETRY_CODES:
                    logger.warning("[Firecrawl] unrecoverable status=%d url=%s", resp.status_code, url)
                    return None

                if resp.status_code == 402:
                    logger.error("[Firecrawl] quota exhausted (402) — top up your account")
                    return None

                if resp.status_code == 429:
                    logger.warning("[Firecrawl] rate-limited attempt=%d url=%s", attempt, url)
                    await asyncio.sleep(30)
                    continue

                if resp.status_code != 200:
                    logger.warning("[Firecrawl] status=%d attempt=%d url=%s", resp.status_code, attempt, url)
                    continue

                data: dict[str, Any] = resp.json()
                # Firecrawl v1: data.data.markdown  — v0: data.markdown
                content: str = (
                    data.get("data", {}).get("markdown", "")
                    or data.get("markdown", "")
                )

                if _is_valid_content(content):
                    logger.info(
                        "[Firecrawl] success attempt=%d len=%d url=%s",
                        attempt, len(content), url,
                    )
                    return content

                logger.warning("[Firecrawl] content invalid len=%d attempt=%d url=%s", len(content), attempt, url)

            except httpx.TimeoutException:
                logger.warning("[Firecrawl] timeout attempt=%d url=%s", attempt, url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Firecrawl] error attempt=%d url=%s: %s", attempt, url, exc)

    logger.error("[Firecrawl] all %d attempts failed url=%s", settings.SCRAPER_RETRY, url)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Waterfall dispatcher
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_page_content(url: str) -> Optional[ScrapeResult]:
    """
    Try each scraper level in order; return on first success.

    Level 1: Jina Reader  (free, fast, clean Markdown output)
    Level 2: Firecrawl    (paid-per-call, stronger JS rendering, reliable fallback)

    Returns
    -------
    ScrapeResult on success, None when all levels fail.
    """
    levels: list[tuple[ScraperSource, Any]] = [
        (ScraperSource.FIRECRAWL, _fetch_firecrawl),
        (ScraperSource.JINA,      _fetch_jina),
    ]

    for source, fetcher in levels:
        logger.info("[Scraper] trying source=%s url=%s", source.value, url)
        t0 = time.monotonic()
        content = await fetcher(url)
        elapsed = time.monotonic() - t0

        if content:
            logger.info(
                "[Scraper] success source=%s elapsed=%.1fs len=%d url=%s",
                source.value, elapsed, len(content), url,
            )
            return ScrapeResult(
                content=content,
                source=source,
                elapsed=elapsed,
                content_length=len(content),
            )
        logger.warning(
            "[Scraper] source=%s failed elapsed=%.1fs url=%s",
            source.value, elapsed, url,
        )

    logger.error("[Scraper] all sources failed url=%s", url)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Business info extractor
# ─────────────────────────────────────────────────────────────────────────────

def extract_business_info(content: str) -> dict[str, Optional[str]]:
    """
    Extract business name, city and phone from raw page text using regex
    heuristics.  Used only to build the SerpAPI GBP query — not part of the
    SEO rule analysis.

    Priority order for business name: copyright → logo alt → h1 title
    Priority order for city: emoji/label pattern → preposition pattern

    Returns
    -------
    dict with keys: ``name``, ``city``, ``phone`` (all Optional[str])
    """
    result: dict[str, Optional[str]] = {"name": None, "city": None, "phone": None}

    # ── Business name ─────────────────────────────────────────────────────────
    name_candidates: list[tuple[str, str]] = []  # (source, value)

    # H2/H3 with "Choose/Trust/About [Business Name]"
    m = re.search(
        r"##\s+(?:Why|How|About).*?(?:Choose|Trust|Love|Prefer)\s+([A-Z][A-Za-z0-9\s&\-\.']{2,50}?)(?:\n|$|\?)",
        content, re.IGNORECASE
    )
    if m:
        val = m.group(1).strip()
        if 2 < len(val) < 60:
            name_candidates.append(("h2_choose", val))

    # og:site_name from Firecrawl metadata
    m = re.search(r'og:site_name["\s:=]+([^"\n<]{2,60})', content, re.IGNORECASE)
    if m:
        val = m.group(1).strip().strip('"\'')
        if 2 < len(val) < 60:
            name_candidates.append(("og_site_name", val))

    # Copyright line: © 2024 Company Name
    m = re.search(
        r"Copyright\s*©?\s*\d{4}\s+([A-Za-z0-9\s&\-\.']+?)(?:\s*\.|$|\n|All)",
        content, re.IGNORECASE,
    )
    if m:
        val = m.group(1).strip()
        if 2 < len(val) < 50:
            name_candidates.append(("copyright", val))

    # Image alt / logo description
    for match in re.findall(
        r"Image\s*\d*:\s*([A-Za-z0-9\s&\-\.']+?)(?:\]|\)|\n|$)", content
    ):
        val = match.strip()
        skip = {"logo", "image", "icon", "loading", "banner", "header", "footer", "background"}
        if 3 < len(val) < 50 and not any(w in val.lower() for w in skip):
            name_candidates.append(("logo", val))
            break

    # H1 / title with separator
    m = re.search(r"#\s+([^\n]+)", content)
    if m:
        title = m.group(1).strip()
        for sep in (" - ", " | ", " – ", " — "):
            if sep in title:
                parts = title.split(sep)
                brand = parts[-1].strip() if len(parts[-1].strip()) >= 3 else parts[0].strip()
                if 2 < len(brand) < 50:
                    name_candidates.append(("title", brand))
                break

    for source in ("h2_choose", "og_site_name", "copyright", "logo", "title"):
        for src, val in name_candidates:
            if src == source:
                result["name"] = val
                break
        if result["name"]:
            break

    # ── City ──────────────────────────────────────────────────────────────────
    skip_words = {
        "the", "our", "your", "this", "that", "local", "area", "all", "best",
        "top", "premium", "professional", "quality", "expert", "trusted",
        "mobile", "full", "complete", "interior", "exterior", "service",
        "detailing", "cleaning", "repair", "auto", "car", "vehicle",
        "get", "in", "touch", "directly", "contact", "us", "now", "today",
        "click", "here", "more", "info", "information", "learn",
    }
    city_candidates: list[tuple[str, str]] = []

    # 📍 or label pattern — must be followed by a real city name (short, no verbs)
    for match in re.findall(
        r"(?:📍)\s*(?:serving\s+|located\s+in\s+|based\s+in\s+)?([A-Za-z\s\-,]{3,40}?)(?:\n|$|&|\|)",
        content, re.IGNORECASE,
    ):
        val = match.strip().rstrip(",").strip()
        # 去掉开头可能残留的 serving/located in 等词
        val = re.sub(r"^(serving|located in|based in|throughout)\s+", "", val, flags=re.IGNORECASE).strip()
        words = val.lower().split()
        if len(val) > 3 and len(words) <= 4 and not any(w in skip_words for w in words):
            city_candidates.append(("label", val))

    for match in re.findall(
        r"(?:in|serving|located in|based in|throughout|across)\s+"
        r"([A-Za-z\s\-]{3,30}?)(?:\s+and\s+|\s*[,\.!?]|\s+area|\s+neighborhood|\s+region)",
        content, re.IGNORECASE,
    ):
        val = match.strip()
        if len(val) > 3 and val.lower() not in skip_words:
            city_candidates.append(("prep", val))

    for source in ("label", "prep"):
        for src, val in city_candidates:
            if src == source:
                result["city"] = val
                break
        if result["city"]:
            break

    # ── Phone ─────────────────────────────────────────────────────────────────
    phone_patterns = [
        # 📞 **(xxx) xxx-xxxx** 格式（contact页面常见）
        r"📞\s*\*+\s*\(?\d{3}\)?[-\s\.]?\d{3}[-\s\.]\d{4}\s*\*+",
        # **xxx-xxx-xxxx** 加粗格式
        r"\*\*\(?\d{3}\)?[-\s\.]?\d{3}[-\s\.]\d{4}\*\*",
        # phone:/tel:/call: 标签
        r"(?:phone|tel|call)[:：]\s*\+?[\d\s\-\.\(\)]{7,20}",
        # 通用格式（最后兜底）
        r"\+?[(]?[0-9]{1,4}[)]?[-\s\.]?[(]?[0-9]{1,4}[)]?[-\s\.]?[0-9]{2,4}[-\s\.]?[0-9]{2,4}",
    ]
    for pat in phone_patterns:
        matches = re.findall(pat, content, re.IGNORECASE)
        if matches:
            phone = re.sub(r"[\*📞]", "", matches[0]).strip()
            phone = re.sub(r"^(phone|tel|call)[:：]\s*", "", phone, flags=re.IGNORECASE).strip()
            if len(phone) >= 7 and "." not in phone:
                result["phone"] = phone
                break

    logger.debug("Extracted business info: %s", result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# SerpAPI — Google Business Profile
# ─────────────────────────────────────────────────────────────────────────────

def _extract_data_id_from_gbp_url(gbp_url: str) -> Optional[str]:
    """
    从 Google Maps URL 中提取 data_id（0x... 格式的十六进制坐标 ID）。

    支持格式：
    - 长链：https://www.google.com/maps/place/.../@lat,lng,z/data=!4m...!1s0xXXX:0xYYY...
    - 短链 / 其他格式：无法直接提取，返回 None
    """
    if not gbp_url:
        return None
    # data_id 格式：0x<hex>:0x<hex>，出现在 Maps URL 的 data= 片段里
    m = re.search(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", gbp_url)
    if m:
        return m.group(1)
    return None


async def fetch_gbp_data(
    business_name: Optional[str],
    city: Optional[str],
    website_url: Optional[str] = None,
    gbp_url: Optional[str] = None,
) -> dict[str, Any]:
    """
    Query SerpAPI for Google Maps / GBP data.

    Priority:
    1. gbp_url 含 data_id → 直接查 place details，最精准，跳过搜索
    2. website_url 域名   → Google Maps 搜索，再按域名匹配结果
    3. business_name+city → Google Maps 搜索，按城市匹配结果

    Returns empty dict on missing key or any error.
    """
    if not settings.SERPAPI_KEY:
        logger.warning("[SerpAPI] SERPAPI_KEY not configured — skipping GBP lookup")
        return {}

    # ── 优先级 1：gbp_url 含 data_id，直接拉 place details ──────────────────
    data_id_from_url = _extract_data_id_from_gbp_url(gbp_url or "")
    if data_id_from_url:
        logger.info("[SerpAPI] gbp_url contains data_id=%s — fetching place details directly", data_id_from_url)
        params: dict[str, str] = {
            "engine":  "google_maps",
            "type":    "place",
            "data_id": data_id_from_url,
            "hl":      "en",
            "api_key": settings.SERPAPI_KEY,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
                resp = await client.get(settings.SERPAPI_BASE_URL, params=params)
                resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            place = data.get("place_results") or (data.get("local_results") or [None])[0]
            if place:
                gbp_info = _build_gbp_info(place)
                gbp_info["data_id"] = gbp_info.get("data_id") or data_id_from_url
                gbp_info["review_list"] = await fetch_gbp_reviews(gbp_info["data_id"])
                return gbp_info
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SerpAPI] place details fetch failed data_id=%s: %s; falling back to search", data_id_from_url, exc)

    # ── 优先级 2 & 3：构建搜索查询 ──────────────────────────────────────────
    if website_url:
        from urllib.parse import urlparse as _urlparse
        domain = _urlparse(website_url).netloc or website_url
        query = domain
        logger.info("[SerpAPI] querying by domain=%s", domain)
    elif business_name:
        query = f"{business_name} {city or ''}".strip()
        logger.info("[SerpAPI] querying by name+city=%s", query)
    else:
        logger.info("[SerpAPI] no query params — skipping GBP lookup")
        return {}

    params: dict[str, str] = {
        "engine": "google_maps",
        "q": query,
        "type": "search",
        "hl": "en",
        "api_key": settings.SERPAPI_KEY,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ) as client:
            resp = await client.get(settings.SERPAPI_BASE_URL, params=params)
            resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        # Prefer place_results (exact match) over local_results (list)
        matched_raw: Optional[dict] = None
        if "place_results" in data:
            logger.info("[SerpAPI] place_results found query=%r", query)
            matched_raw = data["place_results"]

        if matched_raw is None:
            local: list[dict[str, Any]] = data.get("local_results", [])
            if local:
                results = local if isinstance(local, list) else [local]
                city_lower = (city or "").lower()

                # 1. 优先：网站域名精确匹配（最可靠）
                if website_url:
                    from urllib.parse import urlparse as _up
                    target_domain = _up(website_url).netloc.lower().lstrip("www.")
                    for r in results:
                        r_site = r.get("website", "").lower().lstrip("www.")
                        if target_domain and target_domain in r_site:
                            logger.info("[SerpAPI] domain-matched result query=%r domain=%s", query, target_domain)
                            matched_raw = r
                            break

                # 2. 次选：城市匹配
                if matched_raw is None:
                    for r in results:
                        if city_lower and city_lower in r.get("address", "").lower():
                            logger.info("[SerpAPI] city-matched local result query=%r", query)
                            matched_raw = r
                            break

                # 3. 兜底：第一条
                if matched_raw is None:
                    logger.info("[SerpAPI] using first local result query=%r", query)
                    matched_raw = results[0]

        if matched_raw is not None:
            gbp_info = _build_gbp_info(matched_raw)
            # 用 data_id 拉取评论详情（最多 10 条）
            data_id = gbp_info.get("data_id", "")
            if data_id:
                gbp_info["review_list"] = await fetch_gbp_reviews(data_id)
            else:
                gbp_info["review_list"] = []
            return gbp_info

        logger.info("[SerpAPI] no GBP results found query=%r", query)
        return {}

    except httpx.HTTPError as exc:
        logger.error("[SerpAPI] request failed query=%r: %s", query, exc)
        return {}
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.error("[SerpAPI] response parse failed: %s", exc)
        return {}


def _build_gbp_info(r: dict[str, Any]) -> dict[str, Any]:
    """Normalise a SerpAPI result dict into a consistent GBP info structure."""
    type_val = r.get("type", "")
    if isinstance(type_val, list):
        type_val = ", ".join(type_val)
    return {
        "name":          r.get("title", ""),
        "address":       r.get("address", ""),
        "phone":         r.get("phone", ""),
        "rating":        r.get("rating", ""),
        "reviews":       r.get("reviews", ""),   # 评论总数
        "type":          type_val,
        "hours":         r.get("open_state", r.get("hours", "")),
        "website":       r.get("website", ""),
        "service_areas": r.get("service_areas", []),
        # data_id 用于后续拉取评论详情
        "data_id":       r.get("data_id", ""),
    }


async def fetch_gbp_reviews(
    data_id: str,
    max_reviews: int = 10,
) -> list[dict[str, Any]]:
    """
    使用 SerpAPI google_maps_reviews engine 拉取真实评论内容。

    Parameters
    ----------
    data_id:
        从 google_maps 搜索结果中得到的 place data_id（如 0x...）。
    max_reviews:
        最多返回几条评论，默认 10 条。

    Returns
    -------
    list of dicts，每条包含：author、rating、date、text。
    失败时返回空列表。
    """
    if not settings.SERPAPI_KEY or not data_id:
        return []

    params: dict[str, str] = {
        "engine":   "google_maps_reviews",
        "data_id":  data_id,
        "hl":       "en",
        "api_key":  settings.SERPAPI_KEY,
        "sort_by":  "newestFirst",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(settings.SERPAPI_BASE_URL, params=params)
            resp.raise_for_status()
        data = resp.json()

        reviews_raw: list[dict] = data.get("reviews", [])
        reviews: list[dict[str, Any]] = []
        for rv in reviews_raw[:max_reviews]:
            reviews.append({
                "author": rv.get("user", {}).get("name", ""),
                "rating": rv.get("rating", ""),
                "date":   rv.get("date", ""),
                "text":   rv.get("snippet", rv.get("description", "")),
            })

        logger.info("[SerpAPI] fetched %d reviews for data_id=%s", len(reviews), data_id)
        return reviews

    except Exception as exc:  # noqa: BLE001
        logger.warning("[SerpAPI] reviews fetch failed data_id=%s: %s", data_id, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Sub-page URL extraction + concurrent fetch
# ─────────────────────────────────────────────────────────────────────────────

# Which sub-page types to look for, and the regex patterns to find their URLs
_SUB_PAGE_PATTERNS: dict[str, list[str]] = {
    "contact": [
        r'\[.*?\]\((https?://[^)]*contact[^)]*)\)',      # Markdown absolute
        r'\[.*?\]\((/[^)]*contact[^)]*)\)',              # Markdown relative
        r'href=["\']([^"\']*contact[^"\']*)["\']',       # HTML href
    ],
    "about": [
        r'\[.*?\]\((https?://[^)]*about[^)]*)\)',
        r'\[.*?\]\((/[^)]*about[^)]*)\)',
        r'href=["\']([^"\']*about[^"\']*)["\']',
    ],
}


def extract_sub_page_urls(content: str, base_url: str) -> dict[str, str]:
    """
    Scan main-page content for contact / about sub-page URLs.

    Parameters
    ----------
    content:
        Raw Markdown / HTML content of the main page.
    base_url:
        Scheme + host (e.g. ``"https://example.com"``).
        Used to resolve relative paths to absolute URLs.

    Returns
    -------
    dict mapping page_type → absolute URL.
    At most one URL per type (first match wins).
    """
    found: dict[str, str] = {}

    for page_type, patterns in _SUB_PAGE_PATTERNS.items():
        for pattern in patterns:
            for match in re.findall(pattern, content, re.IGNORECASE):
                if not match:
                    continue
                if match.startswith("http"):
                    found[page_type] = match
                elif match.startswith("/"):
                    found[page_type] = base_url.rstrip("/") + match
                break          # first valid match for this pattern
            if page_type in found:
                break          # stop trying other patterns for this type

    if found:
        logger.info("[Scraper] extracted sub-page URLs: %s", found)
    return found


async def _fetch_sub_page(
    page_type: str,
    page_url: str,
) -> tuple[str, Optional[str]]:
    """
    Fetch a single sub-page.  Returns ``(page_type, content)`` or
    ``(page_type, None)`` on failure.
    """
    logger.info("[Scraper] fetching sub-page type=%s url=%s", page_type, page_url)
    result = await fetch_page_content(page_url)
    if result:
        logger.info(
            "[Scraper] sub-page OK type=%s len=%d url=%s",
            page_type, result.content_length, page_url,
        )
        return page_type, result.content
    logger.warning("[Scraper] sub-page failed type=%s url=%s", page_type, page_url)
    return page_type, None


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def scrape(url: str, gbp_url: Optional[str] = None) -> dict[str, Any]:
    """
    Full scraping pipeline for a single URL.

    Flow
    ----
    1. Fetch main page    → Jina Reader (primary) → Firecrawl (fallback)
    2. Extract sub-page URLs from main-page content (real links, not guesses)
    3. Fetch all sub-pages concurrently (each: Jina → Firecrawl)
    4. Append sub-page content with === PAGE === separators
    5. Extract business info (name, city, phone) via regex
    6. Fetch GBP data from SerpAPI (non-blocking on failure)

    Parameters
    ----------
    url:
        Fully-qualified target URL (must start with http/https).

    Returns
    -------
    dict with keys:
        ``content``        — full combined page text (main + sub-pages)
        ``business``       — extracted business metadata (dict)
        ``gbp``            — GBP data from SerpAPI (dict, may be empty)
        ``scraper_source`` — scraper that succeeded for the main page
        ``sub_pages``      — list of sub-page types successfully appended
        ``url``            — original URL

    Raises
    ------
    RuntimeError when both Jina and Firecrawl fail on the main page.
    """
    logger.info("[Scraper] fetching url=%s", url)

    # ── 1. Main page fetch + optional parallel GBP (data_id path) ────────────
    # When gbp_url contains a data_id we can query GBP independently of the
    # page content, so we fire both requests concurrently and save 2-5 s.
    # When there is no data_id, GBP needs business_name/city extracted from
    # page content, so we keep the serial flow for that branch.
    gbp_prefetch: Optional[dict[str, Any]] = None
    has_data_id = bool(_extract_data_id_from_gbp_url(gbp_url or ""))

    if has_data_id:
        logger.info("[Scraper] data_id detected — running main page + GBP in parallel")
        results = await asyncio.gather(
            fetch_page_content(url),
            fetch_gbp_data(
                business_name=None,
                city=None,
                website_url=url,
                gbp_url=gbp_url,
            ),
            return_exceptions=True,
        )
        main_result = results[0] if not isinstance(results[0], Exception) else None
        gbp_prefetch = results[1] if not isinstance(results[1], Exception) else {}
        if isinstance(results[1], Exception):
            logger.warning("[Scraper] parallel GBP fetch failed: %s — continuing without GBP", results[1])
    else:
        main_result = await fetch_page_content(url)

    if main_result is None:
        raise RuntimeError(
            f"Page scraping failed (all scrapers failed) for url={url}"
        )

    logger.info(
        "[Scraper] main page OK source=%s len=%d url=%s",
        main_result.source.value, main_result.content_length, url,
    )

    # ── 2. Extract real sub-page URLs from main content ───────────────────────
    from urllib.parse import urlparse
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    sub_urls = extract_sub_page_urls(main_result.content, base_url)

    # ── 3. Concurrently fetch all detected sub-pages ──────────────────────────
    combined_content = main_result.content
    appended: list[str] = []

    if sub_urls:
        logger.info(
            "[Scraper] launching %d concurrent sub-page fetch(es): %s",
            len(sub_urls), list(sub_urls.keys()),
        )
        sub_tasks = [
            _fetch_sub_page(pt, pu) for pt, pu in sub_urls.items()
        ]
        sub_results_raw = await asyncio.gather(*sub_tasks, return_exceptions=True)
        sub_results: list[tuple[str, Optional[str]]] = []
        for r in sub_results_raw:
            if isinstance(r, Exception):
                logger.warning("[Scraper] sub-page task raised unexpected exception: %s", r)
            else:
                sub_results.append(r)

        # ── 4. Append sub-page content ────────────────────────────────────────
        for page_type, sub_content in sub_results:
            if sub_content:
                tag = page_type.upper()
                combined_content += (
                    f"\n\n=== {tag} PAGE ===\n{sub_content}\n=== END {tag} ==="
                )
                appended.append(page_type)

    logger.info(
        "[Scraper] content assembled — main_len=%d sub_pages=%s",
        len(main_result.content), appended,
    )

    # ── 5. Business info ──────────────────────────────────────────────────────
    business_info = extract_business_info(combined_content)

    # ── 6. GBP lookup ─────────────────────────────────────────────────────────
    # If gbp_prefetch is already populated (parallel fetch above), reuse it.
    # Otherwise query SerpAPI now using business_name/city from page content.
    if gbp_prefetch is not None:
        gbp_data = gbp_prefetch
        logger.info("[Scraper] using prefetched GBP data url=%s", url)
    else:
        try:
            gbp_data = await fetch_gbp_data(
                business_name=business_info.get("name"),
                city=business_info.get("city"),
                website_url=url,
                gbp_url=gbp_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Scraper] GBP fetch failed url=%s: %s; continuing without GBP", url, exc)
            gbp_data = {}

    # ── 7. Return assembled result ────────────────────────────────────────────
    result: dict[str, Any] = {
        "url":            url,
        "content":        combined_content,
        "business":       business_info,
        "gbp":            gbp_data,
        "scraper_source": main_result.source.value,
        "sub_pages":      appended,
    }
    logger.info(
        "[Scraper] done url=%s source=%s sub_pages=%s",
        url, main_result.source.value, appended,
    )
    return result

