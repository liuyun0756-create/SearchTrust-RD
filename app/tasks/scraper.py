"""
app/tasks/scraper.py
────────────────────
Three-layer scraper: Firecrawl (primary) → Jina Reader → direct HTTP.

Public API
----------
scrape(url)              — Main entry point
fetch_page_content(url)  — Waterfall: Firecrawl → Jina → direct HTTP
fetch_gbp_data(...)      — SerpAPI Google Maps / GBP lookup
extract_business_info()  — Regex heuristics to pull name / city / phone

Scraper levels
--------------
1. Firecrawl    — paid-per-call, stronger JS rendering, reliable primary
2. Jina Reader  — free, fast, clean Markdown output
3. Direct HTTP  — server-rendered HTML fallback with safe redirect handling

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
import html
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from app.core.config import settings
from app.models.request import _is_ssrf_safe

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

# Content that can indicate a successful HTTP 200 is actually an error page.
# These are signals, not a flat blacklist: legitimate sites commonly load
# reCAPTCHA, JavaScript fallbacks, or Cloudflare assets alongside real content.
_FAILURE_SIGNALS: tuple[str, ...] = (
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

# These phrases are sufficiently specific to reject a compact response on
# their own. Less-specific signals such as "captcha" must appear in
# combination; otherwise normal pages with protected forms are false positives.
_STRONG_FAILURE_SIGNALS: frozenset[str] = frozenset({
    "access denied",
    "403 forbidden",
    "browser check",
    "ddos protection",
    "verify you are human",
})

# Reader services normally reduce challenge pages to a short message while a
# usable business page is substantially longer. Keep this deliberately larger
# than SCRAPER_MIN_CONTENT_LENGTH so the decision uses both context and size.
_CHALLENGE_PAGE_MAX_LENGTH = 15_000
_MAX_DIRECT_REDIRECTS = 5
_REDIRECT_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

_GBP_LOOKUP_ATTEMPTS = 3
_GBP_SHORT_URL_CACHE_TTL = 24 * 60 * 60
_GBP_SHORT_URL_CACHE_MAX = 256
_GBP_SHORT_URL_CACHE: dict[str, tuple[float, str]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

class ScraperSource(str, Enum):
    JINA = "jina"
    FIRECRAWL = "firecrawl"
    DIRECT = "direct"


@dataclass
class ScrapeResult:
    """Successful scrape outcome with metadata."""

    content: str
    source: ScraperSource
    elapsed: float          # wall-clock seconds for this level
    content_length: int


@dataclass(frozen=True)
class IdentitySignal:
    """One page-derived business identity clue with retained provenance."""

    field: str
    value: str
    source: str
    quality: str
    scope: str = "page"

    def as_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "quality": self.quality,
            "scope": self.scope,
        }


_GENERIC_BRAND_WORDS: frozenset[str] = frozenset({
    "logo", "official", "company", "business", "site", "website", "home",
    "header", "footer", "mobile", "desktop", "sticky", "light", "dark",
    "white", "black", "color", "colour", "primary", "secondary", "default",
    "image", "icon", "brand", "mark", "new", "final", "small", "large",
})
_LOCALITY_SENTENCE_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "is", "are", "was",
    "were", "be", "been", "being", "not", "no", "our", "your", "their",
    "morning", "evening", "today", "tomorrow", "available", "open", "closed",
    "service", "services", "repair", "repairs", "plumbing", "call", "contact",
})
_IDENTITY_QUALITY_WEIGHT = {"strong": 5, "supporting": 3, "weak": 1}


# ─────────────────────────────────────────────────────────────────────────────
# Content quality gate
# ─────────────────────────────────────────────────────────────────────────────

def _is_valid_content(text: str) -> bool:
    """
    Return True only when the scraped text passes both length and challenge-page
    checks.

    A response that is technically HTTP 200 but contains a Cloudflare
    challenge page or an access-denied message is treated as a failure. A
    legitimate page is not rejected merely because a form loads reCAPTCHA.
    """
    min_len = settings.SCRAPER_MIN_CONTENT_LENGTH
    if not text or len(text) < min_len:
        logger.debug("Content too short: %d < %d chars", len(text) if text else 0, min_len)
        return False
    text_lower = text.lower()
    matched_signals = {
        signal for signal in _FAILURE_SIGNALS if signal in text_lower
    }

    if len(text) <= _CHALLENGE_PAGE_MAX_LENGTH:
        strong_matches = matched_signals & _STRONG_FAILURE_SIGNALS
        if strong_matches or len(matched_signals) >= 2:
            logger.warning(
                "Content looks like a challenge page len=%d signals=%s",
                len(text),
                sorted(matched_signals),
            )
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
# Level 3 — direct HTTP fallback
# ────────────────────────────────────────────────────────────────────────────

_HTML_BLOCK_TAG_RE = re.compile(
    r"</?(?:address|article|aside|blockquote|br|dd|div|dl|dt|fieldset|figcaption|"
    r"figure|footer|form|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|"
    r"tbody|td|tfoot|th|thead|tr|ul)[^>]*>",
    re.IGNORECASE,
)
_HTML_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_LINK_RE = re.compile(
    r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_JSON_LD_SCRIPT_RE = re.compile(
    r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>.*?</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _html_to_readable_text(raw_html: str) -> str:
    """Convert HTML into compact readable text while preserving links/schema."""
    if not raw_html:
        return ""

    json_ld_blocks = _JSON_LD_SCRIPT_RE.findall(raw_html)
    without_scripts = _HTML_SCRIPT_RE.sub("\n", raw_html)

    def replace_image(match: re.Match[str]) -> str:
        """Preserve only image alts that are structurally marked as a logo."""
        tag = match.group(0)
        alt_match = re.search(r'\balt=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not alt_match:
            return " "

        alt = html.unescape(alt_match.group(1)).strip()
        if "logo" not in f"{tag} {alt}".lower():
            return " "

        brand = _clean_logo_brand(alt)
        if not _is_usable_brand_candidate(brand):
            return " "

        src_match = re.search(r'\bsrc=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        src = html.unescape(src_match.group(1)).strip() if src_match else "#"
        safe_brand = re.sub(r"[\[\]\r\n]", " ", brand).strip()
        return f"\n![{safe_brand} logo]({src})\n"

    def replace_link(match: re.Match[str]) -> str:
        href = html.unescape(match.group(1).strip())
        label = html.unescape(_HTML_TAG_RE.sub(" ", match.group(2)))
        label = re.sub(r"\s+", " ", label).strip()
        if not label:
            return href
        return f"[{label}]({href})"

    with_logo_alts = _HTML_IMAGE_RE.sub(replace_image, without_scripts)
    readable = _HTML_LINK_RE.sub(replace_link, with_logo_alts)
    readable = _HTML_BLOCK_TAG_RE.sub("\n", readable)
    readable = _HTML_TAG_RE.sub(" ", readable)
    readable = html.unescape(readable).replace("\xa0", " ")

    lines: list[str] = []
    previous_blank = False
    for raw_line in readable.splitlines():
        line = re.sub(r"[\t \f\v]+", " ", raw_line).strip()
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False

    text = "\n".join(lines).strip()
    if json_ld_blocks:
        text += "\n\n" + "\n".join(json_ld_blocks)
    return text


async def _fetch_direct(url: str) -> Optional[str]:
    """Fetch server-rendered HTML without a third-party reader service.

    Redirects are followed manually so every target receives the same SSRF
    validation as the original analysis URL.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        "User-Agent": random.choice(_USER_AGENTS),
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(float(settings.SCRAPER_TIMEOUT + 10)),
        follow_redirects=False,
    ) as client:
        for attempt in range(1, settings.SCRAPER_RETRY + 1):
            if attempt > 1:
                wait = 2 ** (attempt - 1)
                logger.info(
                    "[Direct] retry in %ds (attempt %d/%d)",
                    wait,
                    attempt,
                    settings.SCRAPER_RETRY,
                )
                await asyncio.sleep(wait)

            try:
                current_url = url
                resp: Optional[httpx.Response] = None
                for redirect_count in range(_MAX_DIRECT_REDIRECTS + 1):
                    if not _is_ssrf_safe(current_url):
                        logger.warning("[Direct] blocked unsafe URL: %s", current_url)
                        return None

                    resp = await client.get(current_url, headers=headers)
                    if resp.status_code not in _REDIRECT_CODES:
                        break

                    location = resp.headers.get("location")
                    if not location:
                        logger.warning(
                            "[Direct] redirect missing Location status=%d url=%s",
                            resp.status_code,
                            current_url,
                        )
                        return None

                    next_url = urljoin(str(resp.url), location)
                    if not _is_ssrf_safe(next_url):
                        logger.warning(
                            "[Direct] blocked unsafe redirect from=%s to=%s",
                            current_url,
                            next_url,
                        )
                        return None
                    logger.info(
                        "[Direct] following redirect %d/%d from=%s to=%s",
                        redirect_count + 1,
                        _MAX_DIRECT_REDIRECTS,
                        current_url,
                        next_url,
                    )
                    current_url = next_url
                else:
                    logger.warning("[Direct] too many redirects url=%s", url)
                    return None

                if resp is None:
                    return None
                if resp.status_code in _NO_RETRY_CODES:
                    logger.warning(
                        "[Direct] unrecoverable status=%d url=%s",
                        resp.status_code,
                        url,
                    )
                    return None
                if resp.status_code == 429:
                    try:
                        retry_after = int(resp.headers.get("Retry-After", "10"))
                    except ValueError:
                        retry_after = 10
                    logger.warning("[Direct] rate-limited; waiting %ds url=%s", retry_after, url)
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code != 200:
                    logger.warning(
                        "[Direct] status=%d attempt=%d url=%s",
                        resp.status_code,
                        attempt,
                        url,
                    )
                    continue

                content_type = resp.headers.get("content-type", "").lower()
                if content_type and not any(
                    allowed in content_type
                    for allowed in ("text/html", "application/xhtml+xml", "text/plain")
                ):
                    logger.warning("[Direct] unsupported content-type=%s url=%s", content_type, url)
                    return None

                content = _html_to_readable_text(resp.text)
                if _is_valid_content(content):
                    logger.info(
                        "[Direct] success attempt=%d raw_len=%d text_len=%d url=%s",
                        attempt,
                        len(resp.text),
                        len(content),
                        url,
                    )
                    return content

                logger.warning(
                    "[Direct] content invalid raw_len=%d text_len=%d attempt=%d url=%s",
                    len(resp.text),
                    len(content),
                    attempt,
                    url,
                )
            except httpx.TimeoutException:
                logger.warning("[Direct] timeout attempt=%d url=%s", attempt, url)
            except httpx.ConnectError as exc:
                logger.warning("[Direct] connect error attempt=%d url=%s: %s", attempt, url, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Direct] error attempt=%d url=%s: %s", attempt, url, exc)

    logger.error("[Direct] all %d attempts failed url=%s", settings.SCRAPER_RETRY, url)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Firecrawl /map — discover site URLs via sitemap
# ─────────────────────────────────────────────────────────────────────────────

async def firecrawl_map(
    url: str,
    max_urls: int = 50,
) -> list[str]:
    """
    Use Firecrawl's /map endpoint to get a list of URLs for a site.

    /map costs 1 credit regardless of how many URLs are returned.
    URLs are sourced primarily from the site's sitemap, supplemented by
    search engine data — much higher signal than link-following discovery.

    Parameters
    ----------
    url:
        The site homepage URL.
    max_urls:
        Maximum number of URLs to retrieve (default 50).

    Returns
    -------
    List of URL strings.  Empty list on any error or missing API key.
    """
    if not settings.FIRECRAWL_API_KEY:
        logger.info("[Firecrawl/map] API key not configured — skipping")
        return []

    headers: dict[str, str] = {
        "Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "url": url,
        "limit": max_urls,
    }

    map_endpoint = f"{settings.FIRECRAWL_API_URL}/map"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ) as client:
            resp = await client.post(map_endpoint, headers=headers, json=payload)
            resp.raise_for_status()

        data = resp.json()
        # Response: {"success": true, "links": [{"url": "...", "title": "..."}, ...]}
        # or just {"success": true, "links": ["url1", "url2", ...]} in some versions
        raw_links: list[Any] = data.get("links", [])
        urls: list[str] = []
        for item in raw_links:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                u = item.get("url", "")
                if u:
                    urls.append(u)

        logger.info("[Firecrawl/map] got %d URLs url=%s", len(urls), url)
        return urls

    except Exception as exc:
        logger.error("[Firecrawl/map] failed url=%s: %s", url, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Firecrawl /batch/scrape — scrape a known list of URLs concurrently
# ─────────────────────────────────────────────────────────────────────────────

async def firecrawl_batch_scrape(
    urls: list[str],
    poll_interval: float = 3.0,
    max_wait: float = 120.0,
) -> list[dict[str, Any]]:
    """
    Scrape a predetermined list of URLs via Firecrawl's /batch/scrape endpoint.

    Unlike /crawl, this endpoint does NOT discover additional pages — it scrapes
    exactly the URLs you provide.  This gives full control over which pages are
    fetched and avoids wasting credits on unwanted deep product/category pages.

    Parameters
    ----------
    urls:
        Explicit list of URLs to scrape.
    poll_interval:
        Seconds between status-poll requests.
    max_wait:
        Maximum total seconds to wait for the batch job to complete.

    Returns
    -------
    List of page dicts, each with:
        ``url``      — page URL
        ``markdown`` — page content as Markdown
    Empty list on any error or if API key is not configured.
    """
    if not settings.FIRECRAWL_API_KEY:
        logger.info("[Firecrawl/batch] API key not configured — skipping")
        return []

    if not urls:
        return []

    headers: dict[str, str] = {
        "Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "urls": urls,
        "formats": ["markdown"],
        "onlyMainContent": False,
        "waitFor": 5000,
        "timeout": settings.SCRAPER_TIMEOUT * 1000,
        "actions": [
            {"type": "scroll", "direction": "down", "amount": 500},
            {"type": "wait", "milliseconds": 2000},
            {"type": "scroll", "direction": "down", "amount": 500},
            {"type": "wait", "milliseconds": 1000},
        ],
    }

    batch_endpoint = f"{settings.FIRECRAWL_API_URL}/batch/scrape"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    ) as client:
        # ── Start batch job ───────────────────────────────────────────────────
        try:
            resp = await client.post(batch_endpoint, headers=headers, json=payload)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("[Firecrawl/batch] failed to start job urls=%s: %s", urls, exc)
            return []

        data = resp.json()
        job_id: Optional[str] = data.get("id")
        if not job_id:
            logger.error("[Firecrawl/batch] no job id returned resp=%s", data)
            return []

        logger.info("[Firecrawl/batch] job started id=%s urls=%s", job_id, urls)

        # ── Poll until complete ───────────────────────────────────────────────
        status_url = f"{batch_endpoint}/{job_id}"
        elapsed = 0.0
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                poll_resp = await client.get(status_url, headers=headers)
                poll_resp.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "[Firecrawl/batch] poll error id=%s elapsed=%.0fs: %s",
                    job_id, elapsed, exc,
                )
                continue

            status_data = poll_resp.json()
            status = status_data.get("status", "")
            completed = status_data.get("completed", 0)
            total = status_data.get("total", "?")
            logger.info(
                "[Firecrawl/batch] id=%s status=%s pages=%s/%s elapsed=%.0fs",
                job_id, status, completed, total, elapsed,
            )

            if status == "failed":
                logger.error("[Firecrawl/batch] job failed id=%s", job_id)
                return []

            if status == "completed":
                pages: list[dict[str, Any]] = status_data.get("data", [])
                results = []
                for page in pages:
                    # batch/scrape response: page dict has metadata.url / metadata.sourceURL
                    page_url = (
                        page.get("metadata", {}).get("url")
                        or page.get("metadata", {}).get("sourceURL", "")
                    )
                    markdown = page.get("markdown", "")
                    if markdown and _is_valid_content(markdown):
                        results.append({"url": page_url, "markdown": markdown})
                logger.info(
                    "[Firecrawl/batch] done id=%s valid_pages=%d",
                    job_id, len(results),
                )
                return results

        logger.error(
            "[Firecrawl/batch] timed out after %.0fs id=%s",
            max_wait, job_id,
        )
        return []

async def fetch_page_content(url: str) -> Optional[ScrapeResult]:
    """
    Try each scraper level in order; return on first success.

    Level 1: Firecrawl    (paid-per-call, stronger JS rendering, reliable primary)
    Level 2: Jina Reader  (free, fast, clean Markdown output, fallback)
    Level 3: direct HTTP  (server-rendered HTML fallback, no vendor dependency)

    Returns
    -------
    ScrapeResult on success, None when all levels fail.
    """
    levels: list[tuple[ScraperSource, Any]] = [
        (ScraperSource.FIRECRAWL, _fetch_firecrawl),
        (ScraperSource.JINA,      _fetch_jina),
        (ScraperSource.DIRECT,    _fetch_direct),
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
# Content cleaner — strip noise before feeding to LLM rule engine
# ─────────────────────────────────────────────────────────────────────────────

# Image alt texts that carry no useful information for rule evaluation
_NOISE_ALT_TEXTS: frozenset[str] = frozenset({
    "spinner", "logo", "icon", "banner", "header", "footer",
    "background", "placeholder", "image", "loading", "arrow",
    "chevron", "close", "menu", "search", "cart", "star",
    "check", "checkmark", "play", "pause", "next", "prev",
    "previous", "forward", "back",
})

# Single-line UI fragments that add no SEO-relevant signal
_NOISE_LINE_PATTERNS: tuple[str, ...] = (
    r"^try on$",
    r"^slide \d+ of \d+$",
    r"^go to .+ page$",
    r"^\[go to .+ page\]",
    r"^\[open linked video\]",
    r"^skip to ",
    r"^\[skip to ",
)

_NOISE_LINE_RE = re.compile(
    "|".join(_NOISE_LINE_PATTERNS),
    re.IGNORECASE,
)


def clean_content(text: str) -> str:
    """
    Strip low-signal noise from scraped Markdown before it is passed to the
    LLM rule engine.  The goal is to reduce token consumption while preserving
    every signal that the rule prompts actually check.

    What is removed
    ---------------
    1. Image lines whose alt text is empty, purely numeric, or matches a known
       noise keyword — e.g. ``![Spinner](…)`` or ``![logo](…)``.
       Images with meaningful alt text (people, scenes, before/after, products)
       are kept so rule_6 (fake/stock images) can still fire.
    2. Exact-duplicate paragraphs (carousel responsive-layout double-render).
       First occurrence is kept; subsequent identical blocks are dropped.
    3. Single-line UI fragments: "Try on", "Slide 1 of 8",
       "Go to Spiers Sycamore Crystal page", "Open linked video", etc.
    4. Runs of more than two consecutive blank lines compressed to two.

    What is NOT removed
    -------------------
    - Image lines with substantive alt text (contains non-noise words with
      length > 10 chars), needed for rule_6.
    - All text content, headings, links, reviews, addresses, phone numbers,
      structured sections — needed by every other rule.
    - Page-separator markers (=== PATH ===) used by the aggregation logic.
    """
    if not text:
        return text

    lines = text.splitlines()
    cleaned: list[str] = []

    for line in lines:
        stripped = line.strip()

        # ── 1. Image lines ────────────────────────────────────────────────────
        # Match both bare ![alt](url) and linked [![alt](url)](href)
        img_match = re.match(r'!?\[([^\]]*)\]\([^)]+\)', stripped)
        if img_match:
            alt = img_match.group(1).strip().lower()
            # Keep if alt is substantive (rule_6 needs it)
            is_noise_alt = (
                not alt
                or alt.isdigit()
                or alt in _NOISE_ALT_TEXTS
                or any(w in alt for w in _NOISE_ALT_TEXTS)
            )
            if is_noise_alt:
                continue   # drop noise image line

        # ── 3. Single-line UI fragments ───────────────────────────────────────
        if stripped and _NOISE_LINE_RE.match(stripped):
            continue

        cleaned.append(line)

    # ── 2. Deduplicate paragraphs ─────────────────────────────────────────────
    joined = "\n".join(cleaned)
    paragraphs = joined.split("\n\n")
    seen: set[str] = set()
    deduped: list[str] = []
    for para in paragraphs:
        key = para.strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(para)

    # ── 4. Compress blank lines ───────────────────────────────────────────────
    result = re.sub(r"\n{3,}", "\n\n", "\n\n".join(deduped))

    logger.debug(
        "[Cleaner] %d → %d chars (%.0f%% reduction)",
        len(text), len(result),
        100 * (1 - len(result) / len(text)) if text else 0,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Business info extractor
# ─────────────────────────────────────────────────────────────────────────────

def _extract_business_info_legacy(content: str) -> dict[str, Optional[str]]:
    """
    Extract business name, city and phone from raw page text using regex
    heuristics.  Used only to build the SerpAPI GBP query — not part of the
    SEO rule analysis.

    Priority order for business name: structured data → explicit headings →
    logo alt → site metadata → copyright → h1 title
    Priority order for city: emoji/label pattern → preposition pattern

    Returns
    -------
    dict with keys: ``name``, ``city``, ``phone`` (all Optional[str])
    """
    result: dict[str, Optional[str]] = {"name": None, "city": None, "phone": None}
    schema_identity = _extract_json_ld_business_identity(content)

    # ── Business name ─────────────────────────────────────────────────────────
    name_candidates: list[tuple[str, str]] = []  # (source, value)

    if schema_identity.get("name"):
        schema_name = str(schema_identity["name"]).strip()
        domain_shaped = bool(
            re.fullmatch(
                r"(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}",
                schema_name.lower(),
            )
        )
        name_candidates.append(
            ("json_ld_domain" if domain_shaped else "json_ld", schema_name)
        )

    # H2/H3 with "Choose/Trust/About [Business Name]"
    m = re.search(
        r"##\s+(?:Why|How|About).*?(?:Choose|Trust|Love|Prefer)\s+"
        r"([A-Z][A-Za-z0-9\s&\-\.']{2,50}?)(?:['’][sS]\b|\n|$|\?)",
        content, re.IGNORECASE
    )
    if m:
        val = m.group(1).strip()
        if 2 < len(val) < 60:
            name_candidates.append(("h2_choose", val))

    # Logo alt text is often the only human-readable brand name on small sites.
    # Prefer it over an og:site_name that merely repeats the website domain.
    logo_candidates: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", content, re.IGNORECASE):
        alt_match = re.search(r'\balt=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not alt_match:
            continue
        alt = html.unescape(alt_match.group(1)).strip()
        if "logo" in tag.lower() or "logo" in alt.lower():
            logo_candidates.append(alt)

    for alt in re.findall(r"!\[([^\]]+)\]\([^)]+\)", content):
        if "logo" in alt.lower():
            logo_candidates.append(html.unescape(alt).strip())

    for value in logo_candidates:
        val = re.sub(r"\b(?:official|company|business)?\s*logo\b", " ", value, flags=re.IGNORECASE)
        val = " ".join(val.split()).strip(" -|:")
        if 3 < len(val) < 60 and _is_meaningful_business_title(val):
            name_candidates.append(("logo", val))
            break

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

    # Firecrawl image descriptions (kept as a lower-priority logo fallback).
    for match in re.findall(
        r"Image\s*\d*:\s*([A-Za-z0-9\s&\-\.']+?)(?:\]|\)|\n|$)", content
    ):
        val = match.strip()
        skip = {"logo", "image", "icon", "loading", "banner", "header", "footer", "background"}
        if 3 < len(val) < 50 and not any(w in val.lower() for w in skip):
            name_candidates.append(("image_description", val))
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

    for source in (
        "json_ld",
        "h2_choose",
        "logo",
        "og_site_name",
        "copyright",
        "image_description",
        "title",
        "json_ld_domain",
    ):
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

    if schema_identity.get("city"):
        city_candidates.append(("json_ld", str(schema_identity["city"])))

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

    for source in ("json_ld", "label", "prep"):
        for src, val in city_candidates:
            if src == source:
                result["city"] = val
                break
        if result["city"]:
            break

    # ── Phone ─────────────────────────────────────────────────────────────────
    if schema_identity.get("phone"):
        result["phone"] = str(schema_identity["phone"])

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
        if result["phone"]:
            break
        matches = re.findall(pat, content, re.IGNORECASE)
        if matches:
            phone = re.sub(r"[\*📞]", "", matches[0]).strip()
            phone = re.sub(r"^(phone|tel|call)[:：]\s*", "", phone, flags=re.IGNORECASE).strip()
            if len(phone) >= 7 and "." not in phone:
                result["phone"] = phone
                break

    logger.debug("Extracted business info: %s", result)
    return result


def _clean_logo_brand(value: str | None) -> str:
    cleaned = html.unescape(str(value or ""))
    cleaned = re.sub(
        r"\b(?:official|company|business)?\s*logo\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.split()).strip(" -|:_")


def _is_usable_brand_candidate(value: str | None) -> bool:
    normalized = _normalise_match_text(value)
    if len(normalized) < 3 or not any(character.isalpha() for character in normalized):
        return False
    words = normalized.split()
    meaningful = [word for word in words if word not in _GENERIC_BRAND_WORDS]
    return bool(meaningful and not all(len(word) <= 2 for word in meaningful))


def _looks_like_locality(value: str | None) -> bool:
    candidate = " ".join(str(value or "").strip(" ,.-").split())
    if not candidate or len(candidate) > 45 or any(char.isdigit() for char in candidate):
        return False
    words = _normalise_match_text(candidate).split()
    if not 1 <= len(words) <= 5:
        return False
    if any(word in _LOCALITY_SENTENCE_WORDS for word in words):
        return False
    # Regex fallbacks operate on prose. Requiring name-like capitalization
    # prevents fragments such as "the morning is not" from becoming a city.
    original_words = re.findall(r"[A-Za-z][A-Za-z'.-]*", candidate)
    return bool(
        original_words
        and all(word[0].isupper() or word.isupper() for word in original_words)
    )


def _looks_like_page_topic(value: str | None) -> bool:
    """Distinguish common SEO/service title fragments from a brand fragment."""
    normalized = _normalise_match_text(value)
    return bool(
        normalized
        and re.search(
            r"\b(?:services?|repairs?|installation|replacement|maintenance|"
            r"plumbers?|drain cleaning|water heater|emergency|near me|contact|about)\b",
            normalized,
        )
    )


def _append_identity_signal(
    signals: list[IdentitySignal],
    *,
    field: str,
    value: str | None,
    source: str,
    quality: str,
    scope: str,
) -> None:
    cleaned = " ".join(html.unescape(str(value or "")).split()).strip(" -|:")
    if not cleaned:
        return
    if field == "name" and not _is_usable_brand_candidate(cleaned):
        return
    if field == "city" and source != "json_ld" and not _looks_like_locality(cleaned):
        return
    normalized = _normalise_match_text(cleaned) if field != "phone" else re.sub(r"\D", "", cleaned)[-10:]
    if not normalized:
        return
    if any(
        signal.field == field
        and (
            _normalise_match_text(signal.value)
            if field != "phone"
            else re.sub(r"\D", "", signal.value)[-10:]
        ) == normalized
        and signal.source == source
        for signal in signals
    ):
        return
    signals.append(IdentitySignal(field, cleaned, source, quality, scope))


def extract_business_identity_signals(
    content: str,
    *,
    scope: str = "page",
) -> list[IdentitySignal]:
    """Collect identity evidence without collapsing provenance prematurely."""
    signals: list[IdentitySignal] = []
    schema_identity = _extract_json_ld_business_identity(content)
    schema_name = str(schema_identity.get("name") or "").strip()
    schema_name_is_domain = bool(
        re.fullmatch(r"(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}", schema_name.lower())
    )
    _append_identity_signal(
        signals,
        field="name",
        value=schema_name,
        source="json_ld",
        quality="weak" if schema_name_is_domain else "strong",
        scope=scope,
    )
    _append_identity_signal(
        signals,
        field="city",
        value=schema_identity.get("city"),
        source="json_ld",
        quality="strong",
        scope=scope,
    )
    _append_identity_signal(
        signals,
        field="phone",
        value=schema_identity.get("phone"),
        source="json_ld",
        quality="strong",
        scope=scope,
    )

    heading_match = re.search(
        r"##\s+(?:Why|How|About).*?(?:Choose|Trust|Love|Prefer)\s+"
        r"([A-Z][A-Za-z0-9\s&\-\.']{2,50}?)(?:['’][sS]\b|\n|$|\?)",
        content,
        re.IGNORECASE,
    )
    if heading_match:
        _append_identity_signal(
            signals,
            field="name",
            value=heading_match.group(1),
            source="visible_brand_heading",
            quality="supporting",
            scope=scope,
        )

    logo_values: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", content, re.IGNORECASE):
        alt_match = re.search(r'\balt=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if alt_match and "logo" in f"{tag} {alt_match.group(1)}".lower():
            logo_values.append(alt_match.group(1))
    for alt, src in re.findall(r"!\[([^\]]+)\]\(([^)]+)\)", content):
        if "logo" in f"{alt} {src}".lower():
            logo_values.append(alt)
    for value in logo_values:
        _append_identity_signal(
            signals,
            field="name",
            value=_clean_logo_brand(value),
            source="logo_alt",
            quality="supporting",
            scope=scope,
        )

    meta_patterns = (
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']',
        r'og:site_name["\s:=]+([^"\n<]{2,80})',
    )
    for pattern in meta_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            site_name = match.group(1).strip().strip('"\'')
            site_name_is_domain = bool(
                re.fullmatch(r"(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}", site_name.lower())
            )
            _append_identity_signal(
                signals,
                field="name",
                value=site_name,
                source="og_site_name",
                quality="weak" if site_name_is_domain else "supporting",
                scope=scope,
            )
            break

    copyright_match = re.search(
        r"Copyright\s*©?\s*\d{4}\s+([^\n]+?)(?:\s+All\s+rights|$|\n)",
        content,
        re.IGNORECASE,
    )
    if copyright_match:
        owner = re.split(r"\s*\|\s*|\s+Powered\s+by\s+", copyright_match.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
        _append_identity_signal(
            signals,
            field="name",
            value=owner,
            source="copyright_owner",
            quality="weak",
            scope=scope,
        )

    title_values = [
        match.group(1).strip()
        for match in re.finditer(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    ]
    title_values.extend(
        match.group(1).strip()
        for match in re.finditer(r"^Title:\s*([^\n]+)", content, re.IGNORECASE | re.MULTILINE)
    )
    heading = re.search(r"^#\s+([^\n]+)", content, re.MULTILINE)
    if heading:
        title_values.append(heading.group(1).strip())
    for title in title_values:
        separator = next((sep for sep in (" | ", " – ", " — ", " - ") if sep in title), None)
        if not separator:
            continue
        parts = [part.strip() for part in title.split(separator) if part.strip()]
        if len(parts) < 2:
            continue
        first_quality = "weak"
        last_quality = "supporting"
        if _looks_like_page_topic(parts[-1]) and not _looks_like_page_topic(parts[0]):
            first_quality = "supporting"
            last_quality = "weak"
        elif _looks_like_page_topic(parts[0]) and not _looks_like_page_topic(parts[-1]):
            first_quality = "weak"
            last_quality = "supporting"
        _append_identity_signal(
            signals,
            field="name",
            value=parts[-1],
            source="title_suffix",
            quality=last_quality,
            scope=scope,
        )
        _append_identity_signal(
            signals,
            field="name",
            value=parts[0],
            source="title_prefix",
            quality=first_quality,
            scope=scope,
        )

    for match in re.findall(
        r"(?:📍)\s*(?:serving\s+|located\s+in\s+|based\s+in\s+)?"
        r"([A-Za-z\s\-,]{3,45}?)(?:\n|$|&|\|)",
        content,
        re.IGNORECASE,
    ):
        _append_identity_signal(
            signals,
            field="city",
            value=re.sub(
                r"^(serving|located in|based in|throughout)\s+",
                "",
                match.strip(),
                flags=re.IGNORECASE,
            ),
            source="visible_location_label",
            quality="supporting",
            scope=scope,
        )
    for match in re.findall(
        r"(?:in|serving|located in|based in|throughout|across)\s+"
        r"([A-Za-z\s\-]{3,35}?)(?:\s+and\s+|\s*[,\.!?]|\s+area|\s+neighborhood|\s+region)",
        content,
        re.IGNORECASE,
    ):
        _append_identity_signal(
            signals,
            field="city",
            value=match,
            source="prose_location",
            quality="weak",
            scope=scope,
        )

    phone_patterns = (
        r"(?:phone|tel|call)[:：]?\s*\+?[\d\s\-\.\(\)]{7,22}",
        r"\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}",
    )
    for pattern in phone_patterns:
        for match in re.findall(pattern, content, re.IGNORECASE):
            phone = re.sub(r"^(?:phone|tel|call)[:：]?\s*", "", match, flags=re.IGNORECASE).strip()
            if len(re.sub(r"\D", "", phone)) >= 10:
                _append_identity_signal(
                    signals,
                    field="phone",
                    value=phone,
                    source="visible_phone",
                    quality="strong",
                    scope=scope,
                )
    return signals


def _select_identity_value(signals: list[IdentitySignal], field: str) -> str | None:
    candidates = [signal for signal in signals if signal.field == field]
    if not candidates:
        return None
    groups: dict[str, list[IdentitySignal]] = {}
    for signal in candidates:
        key = (
            re.sub(r"\D", "", signal.value)[-10:]
            if field == "phone"
            else _normalise_match_text(signal.value)
        )
        if key:
            groups.setdefault(key, []).append(signal)
    if not groups:
        return None

    eligible_groups = {
        key: values
        for key, values in groups.items()
        if (
            any(signal.quality in {"strong", "supporting"} for signal in values)
            or len({signal.source for signal in values}) >= 2
        )
    }
    if not eligible_groups:
        return None

    def rank(item: tuple[str, list[IdentitySignal]]) -> tuple[int, int, int]:
        _, values = item
        source_count = len({signal.source for signal in values})
        score = sum(_IDENTITY_QUALITY_WEIGHT.get(signal.quality, 0) for signal in values)
        return score, source_count, -candidates.index(values[0])

    _, winning = max(eligible_groups.items(), key=rank)
    return winning[0].value


def extract_business_info(content: str) -> dict[str, Optional[str]]:
    """Return selected lookup hints while retaining full evidence separately."""
    signals = extract_business_identity_signals(content)
    result = {
        "name": _select_identity_value(signals, "name"),
        "city": _select_identity_value(signals, "city"),
        "phone": _select_identity_value(signals, "phone"),
    }
    logger.debug(
        "Extracted business info=%s evidence=%s",
        result,
        [signal.as_dict() for signal in signals],
    )
    return result


def _extract_json_ld_business_identity(content: str) -> dict[str, Optional[str]]:
    """Extract authoritative business identity fields from preserved JSON-LD."""
    identity: dict[str, Optional[str]] = {"name": None, "city": None, "phone": None}
    business_types = {
        "localbusiness",
        "organization",
        "plumber",
        "homeandconstructionbusiness",
        "professionalservice",
    }
    for raw_script in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        content or "",
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            parsed = json.loads(html.unescape(raw_script).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        for record in _schema_records(parsed):
            raw_types = record.get("@type")
            type_values = raw_types if isinstance(raw_types, list) else [raw_types]
            normalized_types = {
                str(value).strip().lower()
                for value in type_values
                if isinstance(value, str) and value.strip()
            }
            if not normalized_types.intersection(business_types):
                continue

            name = str(record.get("name") or "").strip()
            phone = str(record.get("telephone") or "").strip()
            address = record.get("address")
            city = (
                str(address.get("addressLocality") or "").strip()
                if isinstance(address, dict)
                else ""
            )
            if name and not identity["name"]:
                identity["name"] = name
            if city and not identity["city"]:
                identity["city"] = city
            if phone and not identity["phone"]:
                identity["phone"] = phone
            if all(identity.values()):
                return identity
    return identity


# ─────────────────────────────────────────────────────────────────────────────
# SerpAPI — Google Business Profile
# ─────────────────────────────────────────────────────────────────────────────

def extract_maps_url_from_content(content: str) -> Optional[str]:
    """
    从页面内容中提取可用于精确 GBP 查询的 Google Maps URL。

    优先使用包含 data_id 的 Google Maps 长链，其次使用包含
    destination_place_id / query_place_id 的导航或地点链接。页面仅暴露
    Maps 短链或 share.google GBP 链接时也保留，后续统一展开。

    Returns
    -------
    Google Maps 长链或短链，未找到时返回 None。
    """
    normalised_content = html.unescape(content or "")
    patterns = (
        r'https://(?:www\.)?google\.com/maps/[^\s\'"<>]*0x[0-9a-fA-F]+:0x[0-9a-fA-F]+[^\s\'"<>]*',
        r'https://(?:www\.)?google\.com/maps/(?:dir|search|place)/[^\s\'"<>]*(?:destination_place_id|query_place_id|place_id)=[^\s\'"<>&]+[^\s\'"<>]*',
        r'https://search\.google\.com/local/reviews\?[^\s\'"<>]*placeid=[^\s\'"<>&]+[^\s\'"<>]*',
        r'https://maps\.app\.goo\.gl/[^\s\'"<>\)\]]+',
        r'https://goo\.gl/maps/[^\s\'"<>\)\]]+',
        r'https://share\.google/[^\s\'"<>\)\]]+',
    )
    for pattern in patterns:
        match = re.search(pattern, normalised_content, re.IGNORECASE)
        if match:
            url = match.group(0).rstrip("),.;]")
            logger.info("[Scraper] extracted Google Maps URL from content: %s", url)
            return url
    return None


def _extract_google_place_id(gbp_url: str) -> Optional[str]:
    """Extract an exact Google Place ID from Maps navigation/search URLs."""
    if not gbp_url:
        return None
    try:
        query = parse_qs(urlparse(html.unescape(gbp_url)).query)
    except ValueError:
        return None
    for key in ("destination_place_id", "query_place_id", "place_id", "placeid"):
        values = query.get(key) or []
        if values and values[0].strip():
            return values[0].strip()
    return None


def _has_exact_gbp_identifier(gbp_url: str | None) -> bool:
    """Return whether a URL identifies one Google place rather than a search."""
    value = gbp_url or ""
    return bool(_extract_data_id_from_gbp_url(value) or _extract_google_place_id(value))


def _derive_branch_root_url(page_url: str, content: str) -> Optional[str]:
    """Derive a multi-location branch root such as ``/tri-cities-wa/``.

    Location slugs end in a US state abbreviation. Requiring that shape keeps
    ordinary single-location paths such as ``/services/plumbing/`` on the
    existing lookup path.
    """
    try:
        parsed = urlparse(page_url)
    except ValueError:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or not re.fullmatch(
        r"[a-z0-9-]+-[a-z]{2}", segments[0], re.IGNORECASE
    ):
        return None
    branch_root = f"{parsed.scheme}://{parsed.netloc}/{segments[0]}/"
    decoded_content = html.unescape(unquote(content or ""))
    relative_root = f"/{segments[0]}/"
    if (
        branch_root.lower() in decoded_content.lower()
        or relative_root.lower() in decoded_content.lower()
    ):
        return branch_root
    return None


_DYNAMIC_LOCATION_MARKERS: tuple[str, ...] = (
    "enter zip, city or postal code",
    "enter zip code or city",
    "find your local",
    "select your location",
    "change location",
    "current location",
    "view location details",
)


def _has_dynamic_location_selector(content: str) -> bool:
    """Return whether the target page exposes a location-selection workflow.

    This is deliberately based on explicit selector language. A navigation
    link named merely ``Locations`` is not enough to classify a normal page as
    branch-sensitive.
    """
    readable = _normalise_match_text(html.unescape(content or ""))
    return any(
        _normalise_match_text(marker) in readable
        for marker in _DYNAMIC_LOCATION_MARKERS
    )


def _extract_dynamic_location_page_urls(page_url: str, content: str) -> list[str]:
    """Extract stable same-domain branch links from a location selector.

    The URL must be present in the target-page response. We never synthesize a
    city slug from visible text or browser state, because guessing a branch is
    more harmful than returning no GBP.
    """
    if not _has_dynamic_location_selector(content):
        return []

    try:
        page = urlparse(page_url)
    except ValueError:
        return []
    page_domain = _normalise_domain(page_url)
    page_normalized = page_url.rstrip("/").lower()
    links: list[tuple[str, str]] = []

    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", content or ""):
        links.append((match.group(1), match.group(2)))
    for match in _HTML_LINK_RE.finditer(content or ""):
        label = _HTML_TAG_RE.sub(" ", match.group(2))
        links.append((label, match.group(1)))

    explicit_candidates: list[str] = []
    locality_candidates: list[str] = []
    seen_explicit: set[str] = set()
    seen_locality: set[str] = set()
    for raw_label, raw_href in links:
        label = _normalise_match_text(html.unescape(raw_label))
        href = html.unescape(raw_href).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        explicit_detail_link = any(
            marker in label
            for marker in ("view location details", "location details", "your local")
        )
        locality_label = _looks_like_locality(html.unescape(raw_label))
        if not explicit_detail_link and not locality_label:
            continue

        candidate = urljoin(page_url, href)
        try:
            parsed = urlparse(candidate)
        except ValueError:
            continue
        if not _domains_match(page_domain, _normalise_domain(candidate)):
            continue
        path = parsed.path.rstrip("/") or "/"
        if path == "/" or candidate.rstrip("/").lower() == page_normalized:
            continue
        if path.lower() in {"/location", "/locations", "/find-a-location"}:
            continue

        normalized = f"{parsed.scheme or page.scheme}://{parsed.netloc}{path}/"
        key = normalized.lower()
        target_list = explicit_candidates if explicit_detail_link else locality_candidates
        target_seen = seen_explicit if explicit_detail_link else seen_locality
        if key in target_seen:
            continue
        target_seen.add(key)
        target_list.append(normalized)

    # Explicit "location details" links are strongest. Multiple distinct
    # branch destinations are intentionally left unresolved instead of taking
    # the first one. A locality-labelled link is accepted only when unique.
    if len(explicit_candidates) == 1:
        return explicit_candidates
    if len(explicit_candidates) > 1:
        return []
    if len(locality_candidates) == 1:
        return locality_candidates
    return []


def _location_branch_url_candidates(page_url: str, location_context: str | None) -> list[str]:
    """Build a small set of same-domain branch-page discovery candidates.

    These URLs are never trusted by construction. They are fetched only when
    the user supplied a target location, and the response must independently
    contain that location before it can contribute GBP lookup evidence.
    """
    raw_context = str(location_context or "").strip()
    if not raw_context:
        return []
    try:
        parsed = urlparse(page_url)
    except ValueError:
        return []
    if not parsed.scheme or not parsed.netloc:
        return []

    parts = [part.strip() for part in raw_context.split(",") if part.strip()]
    locality = parts[0] if parts else raw_context
    region_match = re.search(r"\b([A-Za-z]{2})\b", " ".join(parts[1:]))
    region = region_match.group(1).lower() if region_match else ""

    def _slug(value: str) -> str:
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")

    locality_slug = _slug(locality)
    if not locality_slug:
        return []
    slugs = [locality_slug]
    if region:
        slugs.append(f"{locality_slug}-{region}")

    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [
        f"{origin}/{prefix}{slug}/"
        for slug in slugs
        for prefix in ("", "locations/")
    ]
    return list(dict.fromkeys(candidates))[:4]


def _branch_content_matches_location_context(
    content: str,
    location_context: str | None,
    page_url: str | None = None,
) -> bool:
    """Verify that a fetched discovery page actually represents the target locality."""
    raw_context = str(location_context or "").strip()
    if not content or not raw_context:
        return False
    locality = raw_context.split(",", 1)[0].strip()
    normalized_locality = _normalise_match_text(locality)
    readable = _normalise_match_text(html.unescape(content))
    if len(normalized_locality) < 3 or normalized_locality not in readable:
        return False

    if page_url:
        path_slug = re.sub(
            r"-+", "-", re.sub(r"[^a-z0-9]+", "-", urlparse(page_url).path.lower())
        ).strip("-")
        locality_slug = re.sub(
            r"-+", "-", re.sub(r"[^a-z0-9]+", "-", locality.lower())
        ).strip("-")
        if locality_slug and locality_slug not in path_slug:
            return False

    # A location word alone can occur in generic navigation. Require at least
    # one branch-style public identity anchor before accepting the page.
    info = extract_business_info(content)
    has_phone = bool(str(info.get("phone") or "").strip())
    has_address = bool(re.search(
        r"\b\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .#'\-]{2,80}",
        html.unescape(content),
    ))
    region_match = re.search(r"(?:^|,)\s*([A-Za-z]{2})(?:\s|,|$)", raw_context)
    if region_match and has_address:
        region = re.escape(region_match.group(1))
        # Tie the requested state to an address-shaped fragment. This rejects
        # geo-personalized content from another branch even when the requested
        # city still appears in navigation or the page title.
        has_address = bool(re.search(
            rf"\b\d{{1,6}}\s+.{{0,180}}\b{region}\b",
            html.unescape(content),
            flags=re.IGNORECASE | re.DOTALL,
        ))
    if region_match:
        return has_address
    return has_phone or has_address


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
    m = re.search(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", unquote(gbp_url))
    if m:
        return m.group(1)
    return None


def _data_cid_from_data_id(data_id: str | None) -> str | None:
    """Convert the CID half of a Google Maps hex data_id to decimal."""
    if not data_id or ":" not in data_id:
        return None
    try:
        return str(int(data_id.rsplit(":", 1)[1], 16))
    except ValueError:
        return None


def _is_google_maps_url(value: str) -> bool:
    """Return whether ``value`` points to a supported Google Maps host."""
    try:
        host = (urlparse(value).hostname or "").lower()
    except ValueError:
        return False
    return host in {
        "google.com",
        "www.google.com",
        "maps.google.com",
        "search.google.com",
        "maps.app.goo.gl",
        "goo.gl",
    }


def _set_gbp_lookup_diagnostic(
    diagnostic: dict[str, Any] | None,
    *,
    status: str,
    code: str,
    message: str,
) -> None:
    if diagnostic is not None:
        diagnostic.update({"status": status, "code": code, "message": message})


def _get_cached_gbp_url(gbp_url: str) -> str | None:
    cached = _GBP_SHORT_URL_CACHE.get(gbp_url)
    if not cached:
        return None
    cached_at, resolved = cached
    if time.monotonic() - cached_at <= _GBP_SHORT_URL_CACHE_TTL:
        return resolved
    _GBP_SHORT_URL_CACHE.pop(gbp_url, None)
    return None


def _cache_gbp_url(gbp_url: str, resolved: str) -> None:
    if len(_GBP_SHORT_URL_CACHE) >= _GBP_SHORT_URL_CACHE_MAX:
        oldest = min(_GBP_SHORT_URL_CACHE, key=lambda key: _GBP_SHORT_URL_CACHE[key][0])
        _GBP_SHORT_URL_CACHE.pop(oldest, None)
    _GBP_SHORT_URL_CACHE[gbp_url] = (time.monotonic(), resolved)


async def _resolve_gbp_url(
    gbp_url: str,
    diagnostic: dict[str, Any] | None = None,
) -> str:
    """Resolve short links and Place-ID URLs to a URL containing a data_id."""
    if not gbp_url or _extract_data_id_from_gbp_url(gbp_url):
        return gbp_url

    try:
        host = (urlparse(gbp_url).hostname or "").lower()
    except ValueError:
        return gbp_url
    is_short_link = host in {"maps.app.goo.gl", "goo.gl"}
    is_share_link = host == "share.google"
    is_place_id_link = bool(_extract_google_place_id(gbp_url))
    if not (is_short_link or is_share_link or is_place_id_link):
        return gbp_url

    cached = _get_cached_gbp_url(gbp_url)
    if cached:
        logger.info("[SerpAPI] using cached GBP short URL resolution url=%s", gbp_url)
        return cached

    last_error = "Google Maps short link did not resolve to a URL containing a data_id."
    for attempt in range(1, _GBP_LOOKUP_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
                max_redirects=10,
                headers={"User-Agent": random.choice(_USER_AGENTS)},
            ) as client:
                response = await client.get(gbp_url)
                response.raise_for_status()
            resolved = str(response.url)
            data_id = _extract_data_id_from_gbp_url(resolved)
            if not data_id:
                body_data_ids = list(dict.fromkeys(re.findall(
                    r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+",
                    getattr(response, "text", "") or "",
                )))
                if len(body_data_ids) == 1:
                    data_id = body_data_ids[0]
                elif len(body_data_ids) > 1:
                    last_error = (
                        "Google Maps response contained multiple data IDs, so the target "
                        "business could not be identified safely."
                    )
                    logger.warning(
                        "[SerpAPI] ambiguous GBP response body attempt=%d/%d url=%s data_id_count=%d",
                        attempt,
                        _GBP_LOOKUP_ATTEMPTS,
                        gbp_url,
                        len(body_data_ids),
                    )
            if data_id:
                resolved_with_data_id = (
                    resolved
                    if _extract_data_id_from_gbp_url(resolved)
                    else f"{resolved}#data_id={data_id}"
                )
                _cache_gbp_url(gbp_url, resolved_with_data_id)
                logger.info(
                    "[SerpAPI] resolved GBP short URL attempt=%d/%d to %s",
                    attempt,
                    _GBP_LOOKUP_ATTEMPTS,
                    resolved_with_data_id,
                )
                return resolved_with_data_id
            if is_share_link:
                # Some GBP share links resolve to a Google Search knowledge
                # panel instead of Maps. Preserve that resolved entity URL and
                # let branch-aware search verification handle the fallback.
                _cache_gbp_url(gbp_url, resolved)
                return resolved
            last_error = f"Google Maps returned a URL without a data_id: {resolved}"
            logger.warning(
                "[SerpAPI] GBP short URL resolution incomplete attempt=%d/%d resolved=%s",
                attempt,
                _GBP_LOOKUP_ATTEMPTS,
                resolved,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning(
                "[SerpAPI] failed to resolve GBP short URL attempt=%d/%d url=%s: %s",
                attempt,
                _GBP_LOOKUP_ATTEMPTS,
                gbp_url,
                exc,
            )
        if attempt < _GBP_LOOKUP_ATTEMPTS:
            await asyncio.sleep(2 ** (attempt - 1))

    _set_gbp_lookup_diagnostic(
        diagnostic,
        status="error",
        code="short_url_resolution_failed",
        message=f"Google Maps short URL resolution failed after {_GBP_LOOKUP_ATTEMPTS} attempts: {last_error}",
    )
    return gbp_url


def _normalise_domain(value: str | None) -> str:
    if not value:
        return ""
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = (urlparse(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _domains_match(left: str, right: str) -> bool:
    return bool(
        left
        and right
        and (
            left == right
            or left.endswith("." + right)
            or right.endswith("." + left)
        )
    )


def _normalise_match_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _business_names_match(left: str | None, right: str | None) -> bool:
    target = _normalise_match_text(left)
    candidate = _normalise_match_text(right)
    return bool(
        target
        and candidate
        and (
            target == candidate
            or (len(target) >= 6 and target in candidate)
            or (len(candidate) >= 6 and candidate in target)
        )
    )


def _phones_match(left: str | None, right: str | None) -> bool:
    target = re.sub(r"\D", "", left or "")[-10:]
    candidate = re.sub(r"\D", "", right or "")[-10:]
    return bool(len(target) == 10 and target == candidate)


_ADDRESS_TOKEN_ALIASES = {
    "street": "st", "road": "rd", "avenue": "ave", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "highway": "hwy",
    "suite": "ste", "north": "n", "south": "s", "east": "e", "west": "w",
}


def _normalise_address(value: str | None) -> list[str]:
    tokens = _normalise_match_text(value).split()
    return [_ADDRESS_TOKEN_ALIASES.get(token, token) for token in tokens]


def _addresses_match(left: str | None, right: str | None) -> bool:
    target = _normalise_address(left)
    candidate = _normalise_address(right)
    if not target or not candidate:
        return False
    target_numbers = [token for token in target if token.isdigit()]
    candidate_numbers = [token for token in candidate if token.isdigit()]
    if target_numbers and candidate_numbers and target_numbers[0] != candidate_numbers[0]:
        return False
    target_words = {token for token in target if not token.isdigit() and len(token) > 1}
    candidate_words = {token for token in candidate if not token.isdigit() and len(token) > 1}
    return bool(
        target_numbers
        and candidate_numbers
        and len(target_words.intersection(candidate_words)) >= 2
    )


def _addresses_conflict(left: str | None, right: str | None) -> bool:
    target_numbers = [token for token in _normalise_address(left) if token.isdigit()]
    candidate_numbers = [token for token in _normalise_address(right) if token.isdigit()]
    return bool(
        target_numbers
        and candidate_numbers
        and target_numbers[0] != candidate_numbers[0]
    )


def _identity_signal_values(
    signals: list[IdentitySignal] | list[dict[str, Any]] | None,
    field: str,
    *,
    scopes: set[str] | None = None,
) -> list[str]:
    values: list[str] = []
    for signal in signals or []:
        if isinstance(signal, IdentitySignal):
            signal_field = signal.field
            value = signal.value
            quality = signal.quality
            scope = signal.scope
        elif isinstance(signal, dict):
            signal_field = str(signal.get("field") or "")
            value = str(signal.get("value") or "")
            quality = str(signal.get("quality") or "")
            scope = str(signal.get("scope") or "")
        else:
            continue
        if signal_field != field or quality not in {"strong", "supporting"}:
            continue
        if scopes is not None and scope not in scopes:
            continue
        if value.strip():
            values.append(value.strip())
    return list(dict.fromkeys(values))


def _evaluate_gbp_candidate(
    result: dict[str, Any],
    *,
    website_url: str | None,
    business_name: str | None,
    phone: str | None,
    address: str | None,
    city: str | None,
    location_hints: list[str] | None = None,
    identity_signals: list[IdentitySignal] | list[dict[str, Any]] | None = None,
    require_domain_match: bool = False,
    require_location_match: bool = False,
) -> dict[str, Any]:
    """Return an explainable entity decision for one GBP candidate.

    Candidate identification and L3 comparison are intentionally separate.
    Two matching identity fields are enough to identify an ordinary single
    listing; fields that differ remain audit evidence instead of rejecting the
    listing.  When branch-selection risk is present, at least one branch anchor
    (address, phone or target location) must also match.
    """
    title = str(result.get("title") or result.get("name") or "")
    result_phone = str(result.get("phone") or "")
    result_address_raw = str(result.get("address") or "")
    target_domain = _normalise_domain(website_url)
    result_domain = _normalise_domain(str(result.get("website") or ""))

    matches: list[str] = []
    differences: list[str] = []
    blockers: list[str] = []
    score = 0

    domain_matches = _domains_match(target_domain, result_domain)
    primary_business_names = _identity_signal_values(
        identity_signals,
        "name",
        scopes={"target_page"},
    )
    business_names = list(dict.fromkeys([
        *([business_name] if business_name else []),
        *primary_business_names,
    ]))
    if not business_names:
        business_names = _identity_signal_values(identity_signals, "name")
    phones = list(dict.fromkeys([
        *([phone] if phone else []),
        *_identity_signal_values(identity_signals, "phone", scopes={"target_page"}),
    ]))
    name_matches = any(_business_names_match(value, title) for value in business_names)
    phone_matches = any(_phones_match(value, result_phone) for value in phones)
    address_matches = _addresses_match(address, result_address_raw)
    target_locations: set[str] = set()
    for value in [
        city,
        *(location_hints or []),
        *_identity_signal_values(identity_signals, "city", scopes={"target_page"}),
    ]:
        normalized = _normalise_match_text(value)
        if normalized:
            target_locations.add(normalized)
        locality = _normalise_match_text(str(value or "").split(",", 1)[0])
        if len(locality) >= 3:
            target_locations.add(locality)
    normalized_result_address = _normalise_match_text(result_address_raw)
    normalized_result_location_surface = " ".join(
        value
        for value in (
            normalized_result_address,
            _normalise_match_text(str(result.get("website") or "")),
            _normalise_match_text(title),
        )
        if value
    )
    location_matches = bool(
        normalized_result_location_surface
        and any(
            location in normalized_result_location_surface
            for location in target_locations
        )
    )

    for matched, label, weight in (
        (phone_matches, "phone", 6),
        (address_matches, "address", 6),
        (domain_matches, "domain", 3),
        (name_matches, "name", 3),
        (location_matches, "location", 2),
    ):
        if matched:
            matches.append(label)
            score += weight

    # A mismatch is useful L3 evidence. It is not, by itself, proof that this
    # is a different business once two other identity fields align.
    if business_names and title and not name_matches:
        differences.append("name_mismatch")
    if phones and result_phone and not phone_matches:
        differences.append("phone_mismatch")
    if address and result_address_raw and _addresses_conflict(address, result_address_raw):
        differences.append("address_mismatch")
    if target_domain and result_domain and not domain_matches:
        differences.append("domain_mismatch")

    identity_matches = {"name", "domain", "address", "phone"}.intersection(matches)
    branch_anchor_matches = {
        label
        for label, matched in (
            ("address", address_matches),
            ("phone", phone_matches),
            ("location", location_matches),
        )
        if matched
    }
    if require_location_match and not branch_anchor_matches:
        blockers.append("branch_anchor_unverified")

    accepted = bool(
        _is_meaningful_business_title(title)
        and len(identity_matches) >= 2
        and not blockers
    )
    return {
        "accepted": accepted,
        "score": score,
        "matches": matches,
        "identity_match_count": len(identity_matches),
        "branch_anchor_matches": sorted(branch_anchor_matches),
        "branch_selection_risk": require_location_match,
        "differences": differences,
        # Keep this key for existing diagnostics while making clear that only
        # branch-selection failures block a candidate.
        "conflicts": blockers,
        "title": title,
    }


def _candidate_identity_key(candidate: dict[str, Any]) -> str:
    for key in ("data_id", "place_id"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return "|".join((
        _normalise_match_text(str(candidate.get("title") or candidate.get("name") or "")),
        re.sub(r"\D", "", str(candidate.get("phone") or ""))[-10:],
        _normalise_match_text(str(candidate.get("address") or "")),
    ))


def _candidate_pool_has_branch_collision(
    candidates: list[dict[str, Any]],
    *,
    website_url: str | None,
    business_name: str | None,
    identity_signals: list[IdentitySignal] | list[dict[str, Any]] | None = None,
) -> bool:
    """Detect evidence that one brand/domain exposes multiple GBP locations.

    This is deliberately a risk detector, not a claim that the company is
    definitively multi-location.  It only tightens selection when the returned
    candidate pool itself contains distinct branch anchors.
    """
    target_domain = _normalise_domain(website_url)
    business_names = list(dict.fromkeys([
        *([business_name] if business_name else []),
        *_identity_signal_values(identity_signals, "name", scopes={"target_page"}),
    ]))
    if not business_names:
        business_names = _identity_signal_values(identity_signals, "name")

    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        title = str(candidate.get("title") or candidate.get("name") or "")
        candidate_domain = _normalise_domain(str(candidate.get("website") or ""))
        if not _is_meaningful_business_title(title):
            continue
        if target_domain and not _domains_match(target_domain, candidate_domain):
            continue
        if business_names and not any(_business_names_match(name, title) for name in business_names):
            continue
        eligible.append(candidate)

    if len(eligible) < 2:
        return False

    addresses = {
        " ".join(_normalise_address(str(candidate.get("address") or "")))
        for candidate in eligible
        if _normalise_address(str(candidate.get("address") or ""))
    }
    phones = {
        re.sub(r"\D", "", str(candidate.get("phone") or ""))[-10:]
        for candidate in eligible
        if len(re.sub(r"\D", "", str(candidate.get("phone") or ""))) >= 10
    }
    return len(addresses) >= 2 or len(phones) >= 2


def _select_verified_gbp_candidate(
    candidates: list[dict[str, Any]],
    *,
    website_url: str | None,
    business_name: str | None,
    phone: str | None,
    address: str | None,
    city: str | None,
    location_hints: list[str] | None = None,
    identity_signals: list[IdentitySignal] | list[dict[str, Any]] | None = None,
    require_domain_match: bool = False,
    require_location_match: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Select one safe candidate or explain why no automatic choice is valid."""
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = _candidate_identity_key(candidate)
        if key.strip("|"):
            unique.setdefault(key, candidate)

    branch_selection_risk = bool(
        require_location_match
        or _candidate_pool_has_branch_collision(
            list(unique.values()),
            website_url=website_url,
            business_name=business_name,
            identity_signals=identity_signals,
        )
    )

    evaluations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in unique.values():
        evaluation = _evaluate_gbp_candidate(
            candidate,
            website_url=website_url,
            business_name=business_name,
            phone=phone,
            address=address,
            city=city,
            location_hints=location_hints,
            identity_signals=identity_signals,
            require_domain_match=require_domain_match,
            require_location_match=branch_selection_risk,
        )
        evaluations.append((candidate, evaluation))

    accepted = sorted(
        (item for item in evaluations if item[1]["accepted"]),
        key=lambda item: int(item[1]["score"]),
        reverse=True,
    )
    summary = [
        {
            "title": evaluation["title"],
            "score": evaluation["score"],
            "matches": evaluation["matches"],
            "identity_match_count": evaluation["identity_match_count"],
            "branch_anchor_matches": evaluation["branch_anchor_matches"],
            "branch_selection_risk": evaluation["branch_selection_risk"],
            "differences": evaluation["differences"],
            "conflicts": evaluation["conflicts"],
            "accepted": evaluation["accepted"],
        }
        for _, evaluation in evaluations
    ]
    if not accepted:
        unresolved_branches = bool(
            branch_selection_risk
            and any(
                evaluation["identity_match_count"] >= 2
                and not evaluation["branch_anchor_matches"]
                for _, evaluation in evaluations
            )
        )
        if unresolved_branches:
            return None, {
                "status": "ambiguous",
                "branch_selection_risk": True,
                "candidates": summary,
            }
        return None, {
            "status": "not_found",
            "branch_selection_risk": branch_selection_risk,
            "candidates": summary,
        }
    if len(accepted) > 1 and int(accepted[0][1]["score"]) - int(accepted[1][1]["score"]) < 2:
        return None, {
            "status": "ambiguous",
            "branch_selection_risk": branch_selection_risk,
            "candidates": summary,
        }
    return accepted[0][0], {
        "status": "checked",
        "branch_selection_risk": branch_selection_risk,
        "candidates": summary,
    }


def _is_confident_gbp_match(
    result: dict[str, Any],
    *,
    website_url: str | None,
    business_name: str | None,
    phone: str | None,
    city: str | None,
    address: str | None = None,
    location_hints: list[str] | None = None,
    identity_signals: list[IdentitySignal] | list[dict[str, Any]] | None = None,
    require_domain_match: bool = False,
    require_location_match: bool = False,
) -> bool:
    """Compatibility wrapper around the explainable candidate evaluator."""
    return bool(_evaluate_gbp_candidate(
        result,
        website_url=website_url,
        business_name=business_name,
        phone=phone,
        address=address,
        city=city,
        location_hints=location_hints,
        identity_signals=identity_signals,
        require_domain_match=require_domain_match,
        require_location_match=require_location_match,
    )["accepted"])


def _is_meaningful_business_title(value: Any) -> bool:
    """Reject UI labels and numeric map features masquerading as businesses."""
    normalized = _normalise_match_text(str(value or ""))
    return bool(
        len(normalized) >= 3
        and any(character.isalpha() for character in normalized)
        and normalized not in {"level", "floor", "map", "location", "place"}
    )


def _is_confident_exact_gbp_match(
    result: dict[str, Any],
    *,
    website_url: str | None,
    business_name: str | None,
    phone: str | None,
    city: str | None,
    address: str | None = None,
    location_hints: list[str] | None = None,
    identity_signals: list[IdentitySignal] | list[dict[str, Any]] | None = None,
    require_location_match: bool = False,
    user_provided_gbp: bool = False,
) -> bool:
    """Verify an exact-ID response before allowing it to drive backend L3 rules."""
    title = str(result.get("title") or result.get("name") or "")
    if not _is_meaningful_business_title(title):
        return False

    # A real GBP response must expose at least one public identity anchor.
    if not any(str(result.get(key) or "").strip() for key in ("address", "phone", "website")):
        return False

    # The user explicitly selected this comparison target. Differences between
    # it and the page are audit evidence, not a reason to silently replace it.
    if user_provided_gbp:
        return True

    return bool(_evaluate_gbp_candidate(
        result,
        website_url=website_url,
        business_name=business_name,
        phone=phone,
        address=address,
        city=city,
        location_hints=location_hints,
        identity_signals=identity_signals,
        require_location_match=require_location_match,
    )["accepted"])


def _serpapi_payload_error(data: dict[str, Any]) -> str | None:
    error = data.get("error")
    if error:
        return str(error)
    metadata = data.get("search_metadata")
    if isinstance(metadata, dict):
        status = str(metadata.get("status") or "").strip().lower()
        if status and status not in {"success", "cached"}:
            return f"SerpAPI search status was {metadata.get('status')}."
    return None


def _is_serpapi_no_results_error(message: str | None) -> bool:
    """Treat SerpAPI's no-result payload as a completed empty search."""
    normalized = _normalise_match_text(message)
    return bool(
        normalized
        and any(
            marker in normalized
            for marker in (
                "hasn t returned any results",
                "has not returned any results",
                "no results found",
                "no results",
                "did not return any results",
            )
        )
    )


def _gbp_search_queries(
    *,
    website_url: str | None,
    business_name: str | None,
    city: str | None,
    location_hints: list[str] | None,
    phone: str | None,
    address: str | None,
    strict_domain_fallback: bool,
    require_location_match: bool = False,
) -> list[str]:
    """Build ordered, distinct GBP queries without widening strict CID fallback."""
    search_location = next(
        (
            str(value).strip()
            for value in [city, *(location_hints or [])]
            if str(value or "").strip()
        ),
        "",
    )
    queries: list[str] = []

    if website_url:
        domain = urlparse(website_url).netloc or website_url
        queries.append(
            f"{domain} {search_location}"
            if search_location and (require_location_match or not strict_domain_fallback)
            else domain
        )
        if strict_domain_fallback:
            return queries

    normalized_name = _normalise_match_text(business_name)
    normalized_domain = _normalise_match_text(_normalise_domain(website_url))
    if normalized_name and normalized_name != normalized_domain:
        queries.append(f"{business_name} {search_location}".strip())

    phone_digits = re.sub(r"\D", "", phone or "")
    if len(phone_digits) >= 10:
        # The page extractor may omit or duplicate a US country-code prefix.
        # Google Maps can search the canonical last ten digits reliably.
        queries.append(f"{phone_digits[-10:]} {search_location}".strip())

    if address and str(address).strip():
        queries.append(str(address).strip())

    if not queries and business_name:
        queries.append(f"{business_name} {search_location}".strip())

    return list(dict.fromkeys(query for query in queries if query))


async def fetch_gbp_data(
    business_name: Optional[str],
    city: Optional[str],
    website_url: Optional[str] = None,
    gbp_url: Optional[str] = None,
    location_hints: Optional[list[str]] = None,
    phone: Optional[str] = None,
    address: Optional[str] = None,
    require_location_match: bool = False,
    user_provided_gbp: bool = False,
    identity_signals: list[IdentitySignal] | list[dict[str, Any]] | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Query SerpAPI for Google Maps / GBP data.

    Priority:
    1. gbp_url 短链或长链 → 展开并提取 data_id，直接查 place details
    2. website_url 域名   → Google Maps 搜索，再按域名匹配结果
    3. business_name+city → Google Maps 搜索，按城市匹配结果

    Returns empty dict on missing key or any error.
    """
    if not settings.SERPAPI_KEY:
        logger.warning("[SerpAPI] SERPAPI_KEY not configured — skipping GBP lookup")
        _set_gbp_lookup_diagnostic(
            diagnostic,
            status="error",
            code="serpapi_key_missing",
            message="SerpAPI key is not configured.",
        )
        return {}

    # ── 优先级 1：gbp_url 含 data_id，直接拉 place details ──────────────────
    place_id_from_url = _extract_google_place_id(gbp_url or "")
    resolved_gbp_url = (
        gbp_url or ""
        if place_id_from_url
        else await _resolve_gbp_url(gbp_url or "", diagnostic=diagnostic)
    )
    place_id_from_url = place_id_from_url or _extract_google_place_id(resolved_gbp_url)
    data_id_from_url = _extract_data_id_from_gbp_url(resolved_gbp_url)
    data_cid_from_url = _data_cid_from_data_id(data_id_from_url)
    exact_failure = ""
    if place_id_from_url or (data_id_from_url and data_cid_from_url):
        logger.info(
            "[SerpAPI] exact GBP identifier place_id=%s data_id=%s data_cid=%s — fetching place details directly",
            place_id_from_url,
            data_id_from_url,
            data_cid_from_url,
        )
        params: dict[str, str] = {
            "engine":  "google_maps",
            "hl":      "en",
            "api_key": settings.SERPAPI_KEY,
        }
        if place_id_from_url:
            params["place_id"] = place_id_from_url
        elif data_cid_from_url:
            params["data_cid"] = data_cid_from_url
        for attempt in range(1, _GBP_LOOKUP_ATTEMPTS + 1):
            request_params = dict(params)
            if attempt > 1:
                request_params["no_cache"] = "true"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
                    resp = await client.get(settings.SERPAPI_BASE_URL, params=request_params)
                    resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                payload_error = _serpapi_payload_error(data)
                if payload_error:
                    raise ValueError(payload_error)
                place = data.get("place_results") or (data.get("local_results") or [None])[0]
                if isinstance(place, dict) and place:
                    if not _is_confident_exact_gbp_match(
                        place,
                        website_url=website_url,
                        business_name=business_name,
                        phone=phone,
                        address=address,
                        city=city,
                        location_hints=location_hints,
                        identity_signals=identity_signals,
                        require_location_match=require_location_match,
                        user_provided_gbp=user_provided_gbp,
                    ):
                        exact_failure = (
                            "SerpAPI returned an exact-ID object that did not match enough "
                            "target business identity signals."
                        )
                        logger.warning(
                            "[SerpAPI] rejected exact lookup result attempt=%d/%d "
                            "place_id=%s data_id=%s title=%r",
                            attempt,
                            _GBP_LOOKUP_ATTEMPTS,
                            place_id_from_url,
                            data_id_from_url,
                            place.get("title") or place.get("name"),
                        )
                        if attempt < _GBP_LOOKUP_ATTEMPTS:
                            await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    gbp_info = _build_gbp_info(place)
                    gbp_info["data_id"] = gbp_info.get("data_id") or data_id_from_url
                    if place_id_from_url:
                        gbp_info["place_id"] = place_id_from_url
                    _set_gbp_lookup_diagnostic(
                        diagnostic,
                        status="checked",
                        code="exact_place_id_match" if place_id_from_url else "exact_cid_match",
                        message=(
                            f"GBP profile resolved by exact Google Place ID on attempt {attempt}."
                            if place_id_from_url
                            else f"GBP profile resolved by exact Google Maps CID on attempt {attempt}."
                        ),
                    )
                    return await _enrich_gbp_info(gbp_info)
                exact_failure = "SerpAPI returned no place_results for the exact Google identifier."
                metadata = data.get("search_metadata")
                search_id = metadata.get("id") if isinstance(metadata, dict) else None
                logger.warning(
                    "[SerpAPI] exact lookup returned no place attempt=%d/%d place_id=%s data_id=%s search_id=%s",
                    attempt,
                    _GBP_LOOKUP_ATTEMPTS,
                    place_id_from_url,
                    data_id_from_url,
                    search_id,
                )
            except Exception as exc:  # noqa: BLE001
                exact_failure = str(exc)
                logger.warning(
                    "[SerpAPI] exact lookup failed attempt=%d/%d place_id=%s data_id=%s: %s",
                    attempt,
                    _GBP_LOOKUP_ATTEMPTS,
                    place_id_from_url,
                    data_id_from_url,
                    exc,
                )
            if attempt < _GBP_LOOKUP_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))

    # ── 优先级 2 & 3：构建搜索查询 ──────────────────────────────────────────
    strict_domain_fallback = bool(gbp_url and _is_google_maps_url(gbp_url))
    queries = _gbp_search_queries(
        website_url=website_url,
        business_name=business_name,
        city=city,
        location_hints=location_hints,
        phone=phone,
        address=address,
        strict_domain_fallback=strict_domain_fallback,
        require_location_match=require_location_match,
    )
    if not strict_domain_fallback:
        search_location = next(
            (
                str(value).strip()
                for value in [
                    city,
                    *(location_hints or []),
                    *_identity_signal_values(identity_signals, "city"),
                ]
                if str(value or "").strip()
            ),
            "",
        )
        for name_value in _identity_signal_values(identity_signals, "name")[:3]:
            queries.append(f"{name_value} {search_location}".strip())
        for phone_value in _identity_signal_values(identity_signals, "phone")[:2]:
            digits = re.sub(r"\D", "", phone_value)[-10:]
            if len(digits) == 10:
                queries.append(f"{digits} {search_location}".strip())
        queries = list(dict.fromkeys(query for query in queries if query))[:8]
    if strict_domain_fallback and not website_url:
        message = (
            "The supplied Google Maps URL could not be resolved and no website domain "
            "was available for a safe fallback."
        )
        _set_gbp_lookup_diagnostic(
            diagnostic,
            status="error",
            code="safe_fallback_unavailable",
            message=message,
        )
        logger.warning("[SerpAPI] %s", message)
        return {}
    if not queries:
        logger.info("[SerpAPI] no query params — skipping GBP lookup")
        _set_gbp_lookup_diagnostic(
            diagnostic,
            status="error" if gbp_url else "skipped",
            code="safe_fallback_unavailable" if gbp_url else "lookup_input_missing",
            message=(
                "No website domain was available for a safe GBP fallback."
                if gbp_url
                else "No GBP URL, website, business name, or city was available for lookup."
            ),
        )
        return {}

    # An exact-ID lookup already receives transport/no-result retries above.
    # If it falls back to a strict domain search, repeating the same completed
    # Google Maps query cannot add identity evidence and only wastes provider
    # calls. Normal single-clue auto-discovery keeps its retry behaviour for
    # resilience when no independent query is available.
    request_queries = (
        queries
        if strict_domain_fallback
        else queries * _GBP_LOOKUP_ATTEMPTS if len(queries) == 1 else queries
    )
    logger.info(
        "[SerpAPI] GBP search plan queries=%s strict_domain_match=%s",
        request_queries,
        strict_domain_fallback,
    )

    last_request_error = ""
    last_no_match_code = ""
    last_no_match_message = ""
    completed_search = False
    candidate_pool: list[dict[str, Any]] = []
    for attempt, query in enumerate(request_queries, start=1):
        request_params: dict[str, str] = {
            "engine": "google_maps",
            "q": query,
            "type": "search",
            "hl": "en",
            "api_key": settings.SERPAPI_KEY,
        }
        if attempt > 1 and query == request_queries[attempt - 2]:
            request_params["no_cache"] = "true"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            ) as client:
                resp = await client.get(settings.SERPAPI_BASE_URL, params=request_params)
                resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            payload_error = _serpapi_payload_error(data)
            if payload_error and not _is_serpapi_no_results_error(payload_error):
                raise ValueError(payload_error)

            completed_search = True

            candidates: list[dict[str, Any]] = []
            place_result = data.get("place_results")
            if isinstance(place_result, dict):
                candidates.append(place_result)
            local_results = data.get("local_results") or []
            if isinstance(local_results, dict):
                candidates.append(local_results)
            elif isinstance(local_results, list):
                candidates.extend(item for item in local_results if isinstance(item, dict))
            candidate_pool.extend(candidates)

            code = "strict_fallback_no_match" if strict_domain_fallback else "search_no_match"
            message = (
                "Exact CID lookup did not return a profile and the strict website-domain fallback "
                "did not find a matching GBP profile."
                if strict_domain_fallback
                else "GBP search completed without a confident website-domain or city match."
            )
            last_no_match_code = code
            last_no_match_message = message
            logger.warning(
                "[SerpAPI] no confident match attempt=%d/%d query=%r strict_domain_match=%s "
                "candidate_count=%d exact_failure=%s",
                attempt,
                len(request_queries),
                query,
                strict_domain_fallback,
                len(candidates),
                exact_failure,
            )
        except Exception as exc:  # noqa: BLE001
            last_request_error = str(exc)
            logger.warning(
                "[SerpAPI] fallback request failed attempt=%d/%d query=%r: %s",
                attempt,
                len(request_queries),
                query,
                exc,
            )
        if attempt < len(request_queries) and query == request_queries[attempt]:
            await asyncio.sleep(2 ** min(attempt - 1, 2))

    if completed_search:
        matched_raw, selection = _select_verified_gbp_candidate(
            candidate_pool,
            website_url=website_url,
            business_name=business_name,
            phone=phone,
            address=address,
            city=city,
            location_hints=location_hints,
            identity_signals=identity_signals,
            require_domain_match=strict_domain_fallback,
            require_location_match=require_location_match,
        )
        if diagnostic is not None:
            diagnostic["candidate_decisions"] = selection.get("candidates", [])
        if matched_raw is not None:
            gbp_info = _build_gbp_info(matched_raw)
            _set_gbp_lookup_diagnostic(
                diagnostic,
                status="checked",
                code="strict_domain_fallback_match" if strict_domain_fallback else "search_match",
                message="GBP profile was selected from independently verified identity signals.",
            )
            return await _enrich_gbp_info(gbp_info)
        if selection.get("status") == "ambiguous":
            _set_gbp_lookup_diagnostic(
                diagnostic,
                status="ambiguous",
                code="multiple_confident_candidates",
                message=(
                    "Multiple GBP candidates matched with similar confidence, so no profile "
                    "was connected automatically."
                ),
            )
            return {}
        _set_gbp_lookup_diagnostic(
            diagnostic,
            status="not_found",
            code=last_no_match_code,
            message=last_no_match_message or (
                "GBP search completed, but no candidate passed strict entity verification."
            ),
        )
        return {}

    _set_gbp_lookup_diagnostic(
        diagnostic,
        status="error",
        code="serpapi_request_failed",
        message=(
            f"GBP lookup failed after {len(request_queries)} attempts: {last_request_error}. "
            f"Exact lookup detail: {exact_failure or 'not available'}"
        ),
    )
    return {}


def extract_schema_summary(html: str) -> list[str]:
    """Return unique JSON-LD @type values from an HTML response."""
    types: list[str] = []
    for script in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            parsed = json.loads(script.strip())
        except json.JSONDecodeError:
            continue
        for item in _schema_records(parsed):
            value = item.get("@type")
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.strip() and candidate.strip() not in types:
                    types.append(candidate.strip())
    return types


def _schema_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [record for item in value for record in _schema_records(item)]
    if not isinstance(value, dict):
        return []
    records = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
        records.extend(record for item in graph for record in _schema_records(item))
    return records


async def fetch_schema_summary(url: str) -> dict[str, Any] | None:
    """Inspect JSON-LD without converting an unavailable response into a claim."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            headers={"User-Agent": random.choice(_USER_AGENTS), "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        return {"checked": True, "source_url": str(response.url), "types": extract_schema_summary(response.text)}
    except Exception as exc:  # noqa: BLE001
        logger.info("[Schema] unavailable url=%s: %s", url, exc)
        return None


def _build_gbp_info(r: dict[str, Any]) -> dict[str, Any]:
    """Normalise a SerpAPI result dict into a consistent GBP info structure."""
    type_val = r.get("type", "")
    categories_raw = r.get("types") or r.get("categories") or type_val
    if isinstance(categories_raw, list):
        categories = [str(item).strip() for item in categories_raw if str(item).strip()]
    elif categories_raw:
        categories = [str(categories_raw).strip()]
    else:
        categories = []
    if isinstance(type_val, list):
        type_val = ", ".join(str(item) for item in type_val)

    service_areas_observed = "service_areas" in r
    service_area_business = r.get("service_area_business")
    if service_area_business is None:
        service_area_business = r.get("pure_service_area_business")
    return {
        "name":          r.get("title", ""),
        "address":       r.get("address", ""),
        "phone":         r.get("phone", ""),
        "rating":        r.get("rating", ""),
        "reviews":       r.get("reviews", ""),   # 评论总数
        "type":          type_val,
        "categories":    categories,
        "hours":         r.get("hours") or r.get("open_state", ""),
        "website":       r.get("website", ""),
        "service_areas": r.get("service_areas", []),
        "service_areas_observed": service_areas_observed,
        "service_area_business": service_area_business,
        # data_id 用于后续拉取评论详情
        "data_id":       r.get("data_id", ""),
    }


async def _enrich_gbp_info(gbp_info: dict[str, Any]) -> dict[str, Any]:
    """Attach bounded public review, photo and post observations."""
    data_id = str(gbp_info.get("data_id") or "").strip()
    if not data_id:
        gbp_info.update({
            "review_list": [],
            "review_fetch": {"attempted": False, "error": "GBP data_id was unavailable."},
            "photo_fetch": {"attempted": False, "error": "GBP data_id was unavailable."},
            "post_fetch": {"attempted": False, "error": "GBP data_id was unavailable."},
        })
        return gbp_info

    review_fetch, photo_fetch, post_fetch = await asyncio.gather(
        fetch_gbp_review_audit(data_id, max_reviews=30),
        fetch_gbp_photo_audit(data_id),
        fetch_gbp_post_audit(data_id),
    )
    gbp_info["review_list"] = review_fetch.get("items", [])
    gbp_info["review_fetch"] = {key: value for key, value in review_fetch.items() if key != "items"}
    gbp_info["photo_fetch"] = photo_fetch
    gbp_info["post_fetch"] = post_fetch
    return gbp_info


async def fetch_gbp_reviews(
    data_id: str,
    max_reviews: int = 30,
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
    result = await fetch_gbp_review_audit(data_id, max_reviews=max_reviews)
    return result.get("items", [])


async def fetch_gbp_review_audit(
    data_id: str,
    max_reviews: int = 30,
) -> dict[str, Any]:
    """Fetch at most the most recent 30 reviews with pagination metadata."""
    if not settings.SERPAPI_KEY or not data_id:
        return {"attempted": False, "items": [], "error": "SerpAPI key or GBP data_id was unavailable."}

    params: dict[str, str] = {
        "engine":   "google_maps_reviews",
        "data_id":  data_id,
        "hl":       "en",
        "api_key":  settings.SERPAPI_KEY,
        "sort_by":  "newestFirst",
    }

    reviews: list[dict[str, Any]] = []
    token = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            for _ in range(4):
                page_params = dict(params)
                if token:
                    page_params["next_page_token"] = token
                resp = await client.get(settings.SERPAPI_BASE_URL, params=page_params)
                resp.raise_for_status()
                data = resp.json()
                reviews_raw: list[dict[str, Any]] = data.get("reviews", [])
                for rv in reviews_raw:
                    response = rv.get("response") or rv.get("owner_response") or {}
                    owner_reply = response.get("snippet") if isinstance(response, dict) else response
                    reviews.append({
                        "author": (rv.get("user") or {}).get("name", ""),
                        "rating": rv.get("rating", ""),
                        "date": rv.get("date", ""),
                        "text": rv.get("snippet", rv.get("description", "")),
                        "owner_reply": owner_reply or "",
                    })
                    if len(reviews) >= max_reviews:
                        break
                if len(reviews) >= max_reviews:
                    break
                pagination = data.get("serpapi_pagination") or {}
                token = str(pagination.get("next_page_token") or data.get("next_page_token") or "")
                if not token:
                    break

        logger.info("[SerpAPI] fetched %d reviews for data_id=%s", len(reviews), data_id)
        return {"attempted": True, "items": reviews[:max_reviews], "error": None}

    except Exception as exc:  # noqa: BLE001
        logger.warning("[SerpAPI] reviews fetch failed data_id=%s: %s", data_id, exc)
        return {"attempted": True, "items": reviews[:max_reviews], "error": str(exc)}


async def fetch_gbp_photo_audit(data_id: str, max_pages: int = 1) -> dict[str, Any]:
    """Count publicly returned photo records without downloading image files."""
    return await _fetch_gbp_activity_collection(
        engine="google_maps_photos",
        data_id=data_id,
        collection_key="photos",
        max_pages=max_pages,
    )


async def fetch_gbp_post_audit(data_id: str, max_pages: int = 1) -> dict[str, Any]:
    """Count publicly returned GBP posts and preserve the latest observed date."""
    return await _fetch_gbp_activity_collection(
        engine="google_maps_posts",
        data_id=data_id,
        collection_key="posts",
        max_pages=max_pages,
    )


async def _fetch_gbp_activity_collection(
    *,
    engine: str,
    data_id: str,
    collection_key: str,
    max_pages: int,
) -> dict[str, Any]:
    if not settings.SERPAPI_KEY or not data_id:
        return {"attempted": False, "count": None, "latest_date": None, "error": "SerpAPI key or GBP data_id was unavailable."}

    base_params: dict[str, str] = {
        "engine": engine,
        "data_id": data_id,
        "hl": "en",
        "api_key": settings.SERPAPI_KEY,
    }
    count = 0
    latest_date: str | None = None
    token = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            for _ in range(max_pages):
                params = dict(base_params)
                if token:
                    params["next_page_token"] = token
                resp = await client.get(settings.SERPAPI_BASE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                items = data.get(collection_key) or []
                if not isinstance(items, list):
                    items = []
                count += len(items)
                if latest_date is None:
                    for item in items:
                        if isinstance(item, dict):
                            latest_date = item.get("date") or item.get("published_at") or item.get("created_at")
                            if latest_date:
                                break
                pagination = data.get("serpapi_pagination") or {}
                token = str(pagination.get("next_page_token") or data.get("next_page_token") or "")
                if not token:
                    break
        return {"attempted": True, "count": count, "latest_date": latest_date, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SerpAPI] %s fetch failed data_id=%s: %s", collection_key, data_id, exc)
        return {"attempted": True, "count": count, "latest_date": latest_date, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Sub-page URL extraction + concurrent fetch
# ─────────────────────────────────────────────────────────────────────────────

# Asset file extensions — URLs ending in these are never HTML pages
_ASSET_EXTENSIONS: frozenset[str] = frozenset({
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
    ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".css", ".js", ".map",
    ".pdf", ".zip", ".gz", ".tar",
    ".mp4", ".mp3", ".webm", ".ogg",
})

# Path segments that indicate non-content pages (cart, auth, tracking, etc.)
# Any URL whose path contains one of these segments is excluded.
_BLOCKLIST_SEGMENTS: frozenset[str] = frozenset({
    "cart", "checkout", "payment", "order", "orders",
    "account", "my-account", "login", "logout", "signin", "signup",
    "register", "password", "auth",
    "search", "autocomplete", "suggest",
    "api", "graphql", "webhook", "callback",
    "cdn", "static", "assets", "media", "images", "img", "fonts",
    "atc", "add-to-cart",
    "tracking", "analytics", "pixel", "beacon",
    "sitemap", "robots",
    "feed", "rss",
})

# Maximum number of sub-pages to scrape per main page
_MAX_SUB_PAGES: int = 10


def discover_sub_page_urls(content: str, base_url: str) -> list[str]:
    """
    Extract all candidate sub-page URLs from main-page content.

    Works on both Markdown (Firecrawl/Jina output) and raw HTML.
    Returns a deduplicated list of absolute URLs that pass all filters,
    ordered by first appearance.  The caller decides how many to fetch.

    Filters applied
    ---------------
    1. Same host as base_url (rejects CDN / external domains)
    2. Not a static asset (rejects .svg, .png, .js, …)
    3. Not a blocklisted path segment (rejects /cart, /login, /api, …)
    4. Not the homepage itself (rejects bare "/" or the base_url)
    5. Path depth ≤ 2 (rejects /a/b/c deep product/category pages)
    6. No fragment-only links (rejects #section anchors)
    7. Link text must not itself contain a URL (rejects [![](img)](page)
       outer-bracket false-positives by requiring plain text in [...])

    No path-depth limit is applied — any sub-page URL found in the main
    page content is a valid candidate regardless of how deep its path is.
    Depth limiting is done at the crawl level: only main-page links are
    followed (no recursive discovery into sub-pages).
    """
    from urllib.parse import urlparse as _urlparse, urldefrag as _urldefrag

    base_host = _urlparse(base_url).netloc.lower()
    seen: set[str] = set()
    results: list[str] = []

    # Collect raw URL strings from both Markdown and HTML
    raw_candidates: list[str] = []

    # Markdown links: [plain text](url) — text must not contain [ ] ( )
    # This pattern rejects [![](img)](page) because the outer [...] contains "]"
    for m in re.findall(r'\[[^\]\[()]+\]\(([^)]+)\)', content):
        raw_candidates.append(m.strip())

    # HTML href attributes
    for m in re.findall(r'href=["\']([^"\']+)["\']', content, re.IGNORECASE):
        raw_candidates.append(m.strip())

    for raw in raw_candidates:
        # Strip fragment
        raw, fragment = _urldefrag(raw)
        if not raw:
            continue

        # Resolve to absolute URL
        if raw.startswith("http"):
            candidate = raw
        elif raw.startswith("/"):
            candidate = base_url.rstrip("/") + raw
        else:
            continue   # relative paths without leading slash are ambiguous

        # 1. Same host
        try:
            parsed = _urlparse(candidate)
        except Exception:
            continue
        if parsed.netloc.lower() != base_host:
            continue

        path = parsed.path.rstrip("/") or "/"

        # 2. Not a static asset
        path_lower = path.lower()
        if any(path_lower.endswith(ext) for ext in _ASSET_EXTENSIONS):
            continue

        # 3. Not a blocklisted segment
        segments = [s for s in path.lower().split("/") if s]
        if any(seg in _BLOCKLIST_SEGMENTS for seg in segments):
            continue

        # 4. Not the homepage
        if path in ("", "/"):
            continue

        # 5. Path depth ≤ 2  (e.g. /about or /services/plumbing, not /a/b/c)
        if len(segments) > 2:
            continue

        # 6. Deduplicate (normalise by stripping query string for comparison)
        norm = f"{parsed.scheme}://{parsed.netloc}{path}"
        if norm in seen:
            continue
        seen.add(norm)

        results.append(candidate)

    if results:
        logger.info("[Scraper] discovered %d sub-page candidate(s): %s", len(results), results)
    else:
        logger.info("[Scraper] no sub-page candidates found in main page content")
    return results


async def _fetch_sub_page(
    page_url: str,
) -> tuple[str, Optional[str]]:
    """
    Fetch a single sub-page.  Returns ``(page_url, content)`` or
    ``(page_url, None)`` on failure.
    """
    logger.info("[Scraper] fetching sub-page url=%s", page_url)
    result = await fetch_page_content(page_url)
    if result:
        logger.info(
            "[Scraper] sub-page OK len=%d url=%s",
            result.content_length, page_url,
        )
        return page_url, result.content
    logger.warning("[Scraper] sub-page failed url=%s", page_url)
    return page_url, None


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def scrape(
    url: str,
    gbp_url: Optional[str] = None,
    location_context: Optional[str] = None,
) -> dict[str, Any]:
    """
    Full scraping pipeline for a single URL.

    Flow (Firecrawl configured)
    ---------------------------
    1. /scrape  main page (Firecrawl → Jina fallback)
    2. /map     discover full site URL list (sitemap-based, 1 credit)
    3. Filter   depth ≤ 2, same host, no blocklisted segments
    4. /batch/scrape  fetch the selected sub-pages concurrently
    5. Keep the target page separate from supporting-page discovery content
    6. Extract business info (name, city, phone) via regex
    7. Fetch GBP data from SerpAPI

    Flow (Firecrawl not configured — fallback)
    ------------------------------------------
    1. Fetch main page via Jina Reader
    2. Discover sub-page URLs from main content (link extraction)
    3. Fetch sub-pages concurrently via Jina
    4-5. Same as above

    Returns
    -------
    dict with keys:
        ``content``        — cleaned target-page text only
        ``business``       — extracted business metadata (dict)
        ``gbp``            — GBP data from SerpAPI (dict, may be empty)
        ``scraper_source`` — "firecrawl_batch" | "firecrawl" | "jina"
        ``sub_pages``      — list of sub-page URLs successfully appended
        ``url``            — original URL

    Raises
    ------
    RuntimeError when all scrapers fail on the main page.
    """
    logger.info("[Scraper] fetching url=%s", url)
    input_gbp_url = gbp_url
    user_provided_gbp = bool(str(input_gbp_url or "").strip())

    from urllib.parse import urlparse
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    target_content = ""
    combined_content = ""
    appended: list[str] = []
    scraper_source = "firecrawl_batch"

    # ── Option A: Firecrawl /map → filter → /batch/scrape ────────────────────
    # /map retrieves the site's URL list (primarily from sitemap, 1 credit).
    # We then filter to depth ≤ 2 sub-pages and scrape exactly those URLs,
    # giving us full control over which pages are fetched.
    if settings.FIRECRAWL_API_KEY:
        from urllib.parse import urlparse as _up

        logger.info("[Scraper] using Firecrawl /map + /batch/scrape url=%s", url)
        main_path = _up(url).path.rstrip("/") or "/"

        # ── Step 1: fetch main page via /scrape ───────────────────────────────
        main_result = await fetch_page_content(url)
        if main_result:
            target_content = main_result.content
            combined_content = target_content
            scraper_source = main_result.source.value
            logger.info(
                "[Scraper] main page OK source=%s len=%d url=%s",
                scraper_source, main_result.content_length, url,
            )

        # ── Step 2: discover sub-page URLs via /map ───────────────────────────
        all_map_urls = await firecrawl_map(url)

        # Filter: same host, path depth ≤ 2, not the main page itself,
        # not a blocklisted segment, not a static asset
        sub_urls: list[str] = []
        seen_paths: set[str] = set()
        for candidate in all_map_urls:
            try:
                cp = _up(candidate)
            except Exception:
                continue
            if cp.netloc.lower() != _up(url).netloc.lower():
                continue
            path = cp.path.rstrip("/") or "/"
            if path == main_path or path in ("", "/"):
                continue
            path_lower = path.lower()
            if any(path_lower.endswith(ext) for ext in _ASSET_EXTENSIONS):
                continue
            segments = [s for s in path.lower().split("/") if s]
            if any(seg in _BLOCKLIST_SEGMENTS for seg in segments):
                continue
            if len(segments) > 2:                  # depth limit
                continue
            norm = f"{cp.scheme}://{cp.netloc}{path}"
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            sub_urls.append(candidate)
            if len(sub_urls) >= _MAX_SUB_PAGES:
                break

        logger.info(
            "[Scraper] /map filtered %d → %d sub-page(s) url=%s",
            len(all_map_urls), len(sub_urls), url,
        )

        # ── Step 3: batch-scrape the selected sub-pages ───────────────────────
        if sub_urls:
            batch_pages = await firecrawl_batch_scrape(sub_urls)
            for page in batch_pages:
                page_url = page.get("url", "")
                markdown = page.get("markdown", "")
                if not markdown:
                    continue
                page_path = _up(page_url).path.strip("/").replace("/", "-").upper() or "PAGE"
                combined_content += f"\n\n=== {page_path} ===\n{markdown}\n=== END {page_path} ==="
                appended.append(page_url)
                logger.info("[Scraper] sub-page appended url=%s", page_url)

        # If main page scrape failed, fall through to Option B
        if not target_content:
            logger.warning("[Scraper] Firecrawl main page failed — falling back url=%s", url)

    # ── Option B: fallback — /scrape main page + manual sub-page discovery ───
    if not target_content:
        main_result = await fetch_page_content(url)
        if main_result is None:
            raise RuntimeError(
                f"Page scraping failed (all scrapers failed) for url={url}"
            )
        target_content = main_result.content
        # Discard any supporting-only batch result from a failed primary target
        # fetch.  The audit must always have a real target page as its base.
        combined_content = target_content
        appended.clear()
        scraper_source = main_result.source.value
        logger.info(
            "[Scraper] main page OK source=%s len=%d url=%s",
            scraper_source, main_result.content_length, url,
        )

        # Manual sub-page discovery
        all_sub_urls = discover_sub_page_urls(main_result.content, base_url)
        sub_urls = all_sub_urls[:_MAX_SUB_PAGES]
        if sub_urls:
            logger.info("[Scraper] launching %d sub-page fetch(es): %s", len(sub_urls), sub_urls)
            sub_results_raw = await asyncio.gather(
                *[_fetch_sub_page(pu) for pu in sub_urls],
                return_exceptions=True,
            )
            for r in sub_results_raw:
                if isinstance(r, Exception):
                    logger.warning("[Scraper] sub-page task exception: %s", r)
                    continue
                sub_url, sub_content = r
                if sub_content:
                    from urllib.parse import urlparse as _up
                    tag = _up(sub_url).path.strip("/").replace("/", "-").upper() or "PAGE"
                    combined_content += f"\n\n=== {tag} ===\n{sub_content}\n=== END {tag} ==="
                    appended.append(sub_url)

    raw_content_length = len(target_content)
    discovery_content_length = len(combined_content)
    logger.info(
        "[Scraper] content assembled — target_len=%d discovery_len=%d sub_pages=%s",
        raw_content_length, discovery_content_length, appended,
    )

    # ── GBP URL auto-discovery ──────────────────────────────────────────────
    # Multi-location service pages often expose only a generic GBP share link,
    # while their branch root or location selector exposes a stable same-domain
    # branch URL. Fetch that one branch page only for GBP discovery; do not
    # append it to the audit content or change the target-page assessment.
    branch_root_url = _derive_branch_root_url(url, combined_content)
    dynamic_location_page_urls = _extract_dynamic_location_page_urls(url, target_content)
    dynamic_location_page_url = next(iter(dynamic_location_page_urls), None)
    branch_discovery_url = branch_root_url or dynamic_location_page_url
    has_location_selector = _has_dynamic_location_selector(target_content)
    normalized_location_context = re.sub(
        r"\s+", " ", str(location_context or "")
    ).strip(" ,")
    require_location_match = bool(
        branch_root_url or has_location_selector or normalized_location_context
    )
    branch_discovery_content: str | None = None

    if (
        not user_provided_gbp
        and branch_discovery_url
        and branch_discovery_url.rstrip("/") != url.rstrip("/")
    ):
        logger.info(
            "[Scraper] checking stable branch page for GBP discovery url=%s",
            branch_discovery_url,
        )
        try:
            _, branch_discovery_content = await _fetch_sub_page(branch_discovery_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Scraper] branch-page GBP discovery failed url=%s: %s",
                branch_discovery_url,
                exc,
            )

    # A browser-side selector often does not expose its selected branch as a
    # link in the server response. When the user provides a target locality,
    # probe a bounded set of same-domain branch routes and accept one only
    # after its content independently confirms that locality and exposes a
    # branch phone/address. No context means no guessed branch.
    if (
        not user_provided_gbp
        and not branch_discovery_url
        and normalized_location_context
    ):
        for candidate_url in _location_branch_url_candidates(
            url, normalized_location_context
        ):
            logger.info(
                "[Scraper] verifying location-derived branch candidate url=%s context=%s",
                candidate_url,
                normalized_location_context,
            )
            try:
                _, candidate_content = await _fetch_sub_page(candidate_url)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "[Scraper] location-derived branch candidate failed url=%s: %s",
                    candidate_url,
                    exc,
                )
                continue
            if _branch_content_matches_location_context(
                candidate_content or "",
                normalized_location_context,
                candidate_url,
            ):
                branch_discovery_url = candidate_url
                branch_discovery_content = candidate_content
                logger.info(
                    "[Scraper] verified location-derived branch page url=%s",
                    candidate_url,
                )
                break

    if not gbp_url:
        discovered_gbp_url = extract_maps_url_from_content(combined_content)
        if not _has_exact_gbp_identifier(discovered_gbp_url):
            if branch_discovery_content:
                branch_gbp_url = extract_maps_url_from_content(branch_discovery_content)
                if _has_exact_gbp_identifier(branch_gbp_url):
                    discovered_gbp_url = branch_gbp_url
                    logger.info(
                        "[Scraper] exact GBP identifier found on branch page url=%s",
                        branch_discovery_url,
                    )
                elif not discovered_gbp_url and branch_gbp_url:
                    discovered_gbp_url = branch_gbp_url
        gbp_url = discovered_gbp_url
        if gbp_url:
            logger.info("[Scraper] auto-filled gbp_url from page content url=%s", url)

    # ── GBP: exact identifiers can be fetched before heuristic extraction ────
    gbp_prefetch: Optional[dict[str, Any]] = None
    gbp_lookup_attempted = False
    gbp_error: str | None = None
    gbp_lookup_diagnostic: dict[str, Any] = {}
    has_exact_gbp_identifier = _has_exact_gbp_identifier(gbp_url)
    if has_exact_gbp_identifier and user_provided_gbp:
        logger.info("[Scraper] user-provided exact GBP identifier detected — fetching directly")
        gbp_lookup_attempted = True
        gbp_prefetch_result = await fetch_gbp_data(
            business_name=None,
            city=None,
            website_url=url,
            gbp_url=gbp_url,
            user_provided_gbp=True,
            diagnostic=gbp_lookup_diagnostic,
        )
        gbp_prefetch = gbp_prefetch_result or None

    # ── Business info ─────────────────────────────────────────────────────────
    # Keep target-page facts isolated for the audit, while the wider discovery
    # snapshot can still improve GBP lookup without entering Dify page rules.
    target_business_info = extract_business_info(target_content)
    discovery_business_info = extract_business_info(combined_content)
    target_identity_signals = extract_business_identity_signals(target_content, scope="target_page")
    discovery_identity_signals = extract_business_identity_signals(combined_content, scope="site_discovery")
    branch_business_info = extract_business_info(branch_discovery_content or "")
    branch_identity_signals = extract_business_identity_signals(
        branch_discovery_content or "",
        scope="branch_discovery",
    )

    # ── GBP URL auto-fill ─────────────────────────────────────────────────────
    if not gbp_url:
        gbp_url = extract_maps_url_from_content(combined_content)
        if gbp_url:
            logger.info("[Scraper] auto-filled gbp_url from page content url=%s", url)

    # ── Clean content for LLM consumption ────────────────────────────────────
    cleaned = clean_content(target_content)
    logger.info(
        "[Scraper] target content cleaned — raw=%d cleaned=%d chars (%.0f%% reduction) url=%s",
        raw_content_length, len(cleaned),
        100 * (1 - len(cleaned) / raw_content_length) if raw_content_length else 0,
        url,
    )

    if gbp_prefetch is not None:
        gbp_data = gbp_prefetch
        logger.info("[Scraper] using prefetched GBP data url=%s", url)
    else:
        try:
            from app.report_v21.page_facts import build_page_facts

            page_facts = build_page_facts(
                cleaned,
                target_business_info,
                [signal.as_dict() for signal in target_identity_signals],
            )
            branch_page_facts = build_page_facts(
                clean_content(branch_discovery_content or ""),
                branch_business_info,
                [signal.as_dict() for signal in branch_identity_signals],
            )
            branch_address = next(
                (
                    value
                    for value in branch_page_facts.get("addresses", [])
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
            target_address = next(
                (
                    value
                    for value in page_facts.get("addresses", [])
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
            location_hints = [
                value
                for value in [
                    normalized_location_context,
                    branch_business_info.get("city"),
                    *branch_page_facts.get("service_areas", []),
                    target_business_info.get("city"),
                    *page_facts.get("service_areas", []),
                ]
                if isinstance(value, str) and value.strip()
            ]
            unresolved_dynamic_branch = bool(
                has_location_selector
                and not branch_discovery_url
                and not normalized_location_context
                and not gbp_url
            )
            gbp_lookup_attempted = bool(
                gbp_url
                or url
                or discovery_business_info.get("name")
                or discovery_business_info.get("city")
            )
            if unresolved_dynamic_branch:
                _set_gbp_lookup_diagnostic(
                    gbp_lookup_diagnostic,
                    status="not_found",
                    code="branch_location_context_missing",
                    message=(
                        "The page uses a browser-side location selector, but the submitted "
                        "URL did not identify one branch. No GBP profile was guessed."
                    ),
                )
                gbp_data = {}
            else:
                gbp_data = await fetch_gbp_data(
                    business_name=(
                        branch_business_info.get("name")
                        or target_business_info.get("name")
                        or discovery_business_info.get("name")
                    ),
                    city=(
                        branch_business_info.get("city")
                        or target_business_info.get("city")
                    ),
                    website_url=url,
                    gbp_url=gbp_url,
                    location_hints=location_hints,
                    # A stable branch page is discovery-only evidence. Its phone
                    # and address may identify the branch, while the strict L3
                    # comparison still uses the untouched target-page facts.
                    phone=(
                        branch_business_info.get("phone")
                        or target_business_info.get("phone")
                    ),
                    address=branch_address or target_address,
                    require_location_match=require_location_match,
                    user_provided_gbp=user_provided_gbp,
                    identity_signals=[
                        *target_identity_signals,
                        *discovery_identity_signals,
                        *branch_identity_signals,
                    ],
                    diagnostic=gbp_lookup_diagnostic,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Scraper] GBP fetch failed url=%s: %s; continuing without GBP", url, exc)
            gbp_error = str(exc)
            gbp_data = {}

    if not gbp_data and gbp_lookup_diagnostic.get("status") == "error":
        code = str(gbp_lookup_diagnostic.get("code") or "lookup_failed")
        message = str(gbp_lookup_diagnostic.get("message") or "GBP lookup failed.")
        gbp_error = f"{code}: {message}"

    # ── Return ────────────────────────────────────────────────────────────────
    result: dict[str, Any] = {
        "url":                url,
        "content":            cleaned,           # cleaned for LLM rule engine
        "raw_content_length": raw_content_length, # original length for debugging
        "business":           target_business_info,
        "gbp":                gbp_data,
        "gbp_url":            gbp_url,
        "gbp_lookup_attempted": gbp_lookup_attempted,
        "gbp_error":          gbp_error,
        "gbp_lookup_diagnostic": gbp_lookup_diagnostic or None,
        "location_context": normalized_location_context or None,
        "identity_signals": [signal.as_dict() for signal in discovery_identity_signals],
        "target_identity_signals": [signal.as_dict() for signal in target_identity_signals],
        "scraper_source":     scraper_source,
        "sub_pages":          appended,
    }
    logger.info(
        "[Scraper] done url=%s source=%s sub_pages=%s",
        url, scraper_source, appended,
    )
    return result
