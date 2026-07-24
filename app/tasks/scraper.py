"""
app/tasks/scraper.py
────────────────────
Dual-layer scraper: Firecrawl (primary) → Jina Reader (fallback).

Public API
----------
scrape(url)              — Main entry point
fetch_page_content(url)  — Waterfall: Firecrawl → Jina
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
from urllib.parse import unquote, urlparse

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
        r"##\s+(?:Why|How|About).*?(?:Choose|Trust|Love|Prefer)\s+"
        r"([A-Z][A-Za-z0-9\s&\-\.']{2,50}?)(?:['’][sS]\b|\n|$|\?)",
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

def extract_maps_url_from_content(content: str) -> Optional[str]:
    """
    从页面内容中提取可用于精确 GBP 查询的 Google Maps URL。

    优先使用包含 data_id 的 Google Maps 长链；页面仅暴露 goo.gl/maps 或
    maps.app.goo.gl 短链时也保留该链接，后续统一展开并按 CID 查询。

    Returns
    -------
    Google Maps 长链或短链，未找到时返回 None。
    """
    patterns = (
        r'https://(?:www\.)?google\.com/maps/[^\s\'"<>]*0x[0-9a-fA-F]+:0x[0-9a-fA-F]+[^\s\'"<>]*',
        r'https://maps\.app\.goo\.gl/[^\s\'"<>]+',
        r'https://goo\.gl/maps/[^\s\'"<>]+',
    )
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            url = match.group(0).rstrip("),.;]")
            logger.info("[Scraper] extracted Google Maps URL from content: %s", url)
            return url
    return None


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
    """Expand Google Maps short links with bounded retries and caching."""
    if not gbp_url or _extract_data_id_from_gbp_url(gbp_url):
        return gbp_url

    try:
        host = (urlparse(gbp_url).hostname or "").lower()
    except ValueError:
        return gbp_url
    if host not in {"maps.app.goo.gl", "goo.gl"}:
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
            if _is_google_maps_url(resolved) and _extract_data_id_from_gbp_url(resolved):
                _cache_gbp_url(gbp_url, resolved)
                logger.info(
                    "[SerpAPI] resolved GBP short URL attempt=%d/%d to %s",
                    attempt,
                    _GBP_LOOKUP_ATTEMPTS,
                    resolved,
                )
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


def _is_confident_gbp_match(
    result: dict[str, Any],
    *,
    website_url: str | None,
    city: str | None,
    require_domain_match: bool = False,
) -> bool:
    """Require an objective domain or city match before accepting search results."""
    target_domain = _normalise_domain(website_url)
    result_domain = _normalise_domain(str(result.get("website") or ""))
    if _domains_match(target_domain, result_domain):
        return True
    if require_domain_match:
        return False

    target_city = _normalise_match_text(city)
    result_address = _normalise_match_text(str(result.get("address") or ""))
    return bool(target_city and result_address and target_city in result_address)


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


async def fetch_gbp_data(
    business_name: Optional[str],
    city: Optional[str],
    website_url: Optional[str] = None,
    gbp_url: Optional[str] = None,
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
    resolved_gbp_url = await _resolve_gbp_url(gbp_url or "", diagnostic=diagnostic)
    data_id_from_url = _extract_data_id_from_gbp_url(resolved_gbp_url)
    data_cid_from_url = _data_cid_from_data_id(data_id_from_url)
    exact_failure = ""
    if data_id_from_url and data_cid_from_url:
        logger.info(
            "[SerpAPI] gbp_url contains data_id=%s data_cid=%s — fetching place details directly",
            data_id_from_url,
            data_cid_from_url,
        )
        params: dict[str, str] = {
            "engine":  "google_maps",
            "data_cid": data_cid_from_url,
            "hl":      "en",
            "api_key": settings.SERPAPI_KEY,
        }
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
                    gbp_info = _build_gbp_info(place)
                    gbp_info["data_id"] = gbp_info.get("data_id") or data_id_from_url
                    _set_gbp_lookup_diagnostic(
                        diagnostic,
                        status="checked",
                        code="exact_cid_match",
                        message=f"GBP profile resolved by exact Google Maps CID on attempt {attempt}.",
                    )
                    return await _enrich_gbp_info(gbp_info)
                exact_failure = "SerpAPI returned no place_results for the exact Google Maps CID."
                metadata = data.get("search_metadata")
                search_id = metadata.get("id") if isinstance(metadata, dict) else None
                logger.warning(
                    "[SerpAPI] exact CID lookup returned no place attempt=%d/%d data_id=%s search_id=%s",
                    attempt,
                    _GBP_LOOKUP_ATTEMPTS,
                    data_id_from_url,
                    search_id,
                )
            except Exception as exc:  # noqa: BLE001
                exact_failure = str(exc)
                logger.warning(
                    "[SerpAPI] exact CID lookup failed attempt=%d/%d data_id=%s: %s",
                    attempt,
                    _GBP_LOOKUP_ATTEMPTS,
                    data_id_from_url,
                    exc,
                )
            if attempt < _GBP_LOOKUP_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))

    # ── 优先级 2 & 3：构建搜索查询 ──────────────────────────────────────────
    strict_domain_fallback = bool(gbp_url and _is_google_maps_url(gbp_url))
    if website_url:
        domain = urlparse(website_url).netloc or website_url
        query = domain
        logger.info(
            "[SerpAPI] querying by domain=%s strict_domain_match=%s",
            domain,
            strict_domain_fallback,
        )
    elif business_name:
        if strict_domain_fallback:
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
        query = f"{business_name} {city or ''}".strip()
        logger.info("[SerpAPI] querying by name+city=%s", query)
    else:
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

    params: dict[str, str] = {
        "engine": "google_maps",
        "q": query,
        "type": "search",
        "hl": "en",
        "api_key": settings.SERPAPI_KEY,
    }

    last_request_error = ""
    last_no_match_code = ""
    last_no_match_message = ""
    last_outcome = ""
    for attempt in range(1, _GBP_LOOKUP_ATTEMPTS + 1):
        request_params = dict(params)
        if attempt > 1:
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
            if payload_error:
                raise ValueError(payload_error)

            candidates: list[dict[str, Any]] = []
            place_result = data.get("place_results")
            if isinstance(place_result, dict):
                candidates.append(place_result)
            local_results = data.get("local_results") or []
            if isinstance(local_results, dict):
                candidates.append(local_results)
            elif isinstance(local_results, list):
                candidates.extend(item for item in local_results if isinstance(item, dict))

            matched_raw = next(
                (
                    candidate
                    for candidate in candidates
                    if _is_confident_gbp_match(
                        candidate,
                        website_url=website_url,
                        city=city,
                        require_domain_match=strict_domain_fallback,
                    )
                ),
                None,
            )
            if matched_raw is not None:
                gbp_info = _build_gbp_info(matched_raw)
                _set_gbp_lookup_diagnostic(
                    diagnostic,
                    status="checked",
                    code="strict_domain_fallback_match" if strict_domain_fallback else "search_match",
                    message=(
                        "Exact CID lookup was unavailable; GBP profile was verified by exact website domain."
                        if strict_domain_fallback
                        else "GBP profile was verified by website domain or city."
                    ),
                )
                return await _enrich_gbp_info(gbp_info)

            code = "strict_fallback_no_match" if strict_domain_fallback else "search_no_match"
            message = (
                "Exact CID lookup did not return a profile and the strict website-domain fallback "
                "did not find a matching GBP profile."
                if strict_domain_fallback
                else "GBP search completed without a confident website-domain or city match."
            )
            last_no_match_code = code
            last_no_match_message = message
            last_outcome = "no_match"
            logger.warning(
                "[SerpAPI] no confident match attempt=%d/%d query=%r strict_domain_match=%s "
                "candidate_count=%d exact_failure=%s",
                attempt,
                _GBP_LOOKUP_ATTEMPTS,
                query,
                strict_domain_fallback,
                len(candidates),
                exact_failure,
            )
            if attempt < _GBP_LOOKUP_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))

        except Exception as exc:  # noqa: BLE001
            last_request_error = str(exc)
            last_outcome = "error"
            logger.warning(
                "[SerpAPI] fallback request failed attempt=%d/%d query=%r: %s",
                attempt,
                _GBP_LOOKUP_ATTEMPTS,
                query,
                exc,
            )
            if attempt < _GBP_LOOKUP_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))

    if last_outcome == "no_match":
        _set_gbp_lookup_diagnostic(
            diagnostic,
            status="not_found",
            code=last_no_match_code,
            message=last_no_match_message,
        )
        return {}

    _set_gbp_lookup_diagnostic(
        diagnostic,
        status="error",
        code="serpapi_request_failed",
        message=(
            f"GBP lookup failed after {_GBP_LOOKUP_ATTEMPTS} attempts: {last_request_error}. "
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

async def scrape(url: str, gbp_url: Optional[str] = None) -> dict[str, Any]:
    """
    Full scraping pipeline for a single URL.

    Flow (Firecrawl configured)
    ---------------------------
    1. /scrape  main page (Firecrawl → Jina fallback)
    2. /map     discover full site URL list (sitemap-based, 1 credit)
    3. Filter   depth ≤ 2, same host, no blocklisted segments
    4. /batch/scrape  fetch the selected sub-pages concurrently
    5. Concatenate all page content with === PATH === separators
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
        ``content``        — full combined page text (main + sub-pages)
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

    from urllib.parse import urlparse
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

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
            combined_content = main_result.content
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
        if not combined_content:
            logger.warning("[Scraper] Firecrawl main page failed — falling back url=%s", url)

    # ── Option B: fallback — /scrape main page + manual sub-page discovery ───
    if not combined_content:
        main_result = await fetch_page_content(url)
        if main_result is None:
            raise RuntimeError(
                f"Page scraping failed (all scrapers failed) for url={url}"
            )
        combined_content = main_result.content
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

    raw_content_length = len(combined_content)
    logger.info(
        "[Scraper] content assembled — len=%d sub_pages=%s",
        raw_content_length, appended,
    )

    # ── GBP: if data_id in gbp_url, run in parallel with business info ────────
    gbp_prefetch: Optional[dict[str, Any]] = None
    gbp_lookup_attempted = False
    gbp_error: str | None = None
    gbp_lookup_diagnostic: dict[str, Any] = {}
    has_data_id = bool(_extract_data_id_from_gbp_url(gbp_url or ""))
    if has_data_id:
        logger.info("[Scraper] data_id detected — fetching GBP in parallel with business info")
        gbp_lookup_attempted = True
        gbp_prefetch_result = await fetch_gbp_data(
            business_name=None,
            city=None,
            website_url=url,
            gbp_url=gbp_url,
            diagnostic=gbp_lookup_diagnostic,
        )
        gbp_prefetch = gbp_prefetch_result

    # ── Business info ─────────────────────────────────────────────────────────
    # Use raw content for regex-based extraction (phone/address patterns need
    # the full unmodified text; clean_content may strip some context lines).
    business_info = extract_business_info(combined_content)

    # ── GBP URL auto-fill ─────────────────────────────────────────────────────
    if not gbp_url:
        gbp_url = extract_maps_url_from_content(combined_content)
        if gbp_url:
            logger.info("[Scraper] auto-filled gbp_url from page content url=%s", url)

    # ── Clean content for LLM consumption ────────────────────────────────────
    cleaned = clean_content(combined_content)
    logger.info(
        "[Scraper] content cleaned — raw=%d cleaned=%d chars (%.0f%% reduction) url=%s",
        raw_content_length, len(cleaned),
        100 * (1 - len(cleaned) / raw_content_length) if raw_content_length else 0,
        url,
    )

    if gbp_prefetch is not None:
        gbp_data = gbp_prefetch
        logger.info("[Scraper] using prefetched GBP data url=%s", url)
    else:
        try:
            gbp_lookup_attempted = bool(
                gbp_url or business_info.get("name") or business_info.get("city")
            )
            gbp_data = await fetch_gbp_data(
                business_name=business_info.get("name"),
                city=business_info.get("city"),
                website_url=url,
                gbp_url=gbp_url,
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
        "business":           business_info,
        "gbp":                gbp_data,
        "gbp_url":            gbp_url,
        "gbp_lookup_attempted": gbp_lookup_attempted,
        "gbp_error":          gbp_error,
        "gbp_lookup_diagnostic": gbp_lookup_diagnostic or None,
        "scraper_source":     scraper_source,
        "sub_pages":          appended,
    }
    logger.info(
        "[Scraper] done url=%s source=%s sub_pages=%s",
        url, scraper_source, appended,
    )
    return result
