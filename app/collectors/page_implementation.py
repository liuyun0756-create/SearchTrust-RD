"""Opt-in internal single-page check. Never registered as an automatic task.

Only static observations are returned, not raw HTML or implementation/effect claims.
The caller must load authorized source records before preparing the plan.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import unicodedata
from datetime import datetime, timezone
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from uuid import UUID

from app.collectors.site_inventory_discovery import RobotsPolicy, parse_robots
from app.collectors.site_inventory_fetcher import BoundedSiteFetcher, SiteFetchError
from app.collectors.site_inventory_urls import SiteScope
from app.report_v22.capability import instant
from app.report_v22.page_implementation_checks import checked_plan, checksum_model
from app.report_v22.page_implementation_models import PageCheckPlan, PageObservation, page_url
from app.security_v22.urls import UrlSafetyError, UrlUnreachableError


_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_EXCLUDED = frozenset("head title script style template noscript textarea select iframe".split())
_BLOCKS = frozenset("address article aside blockquote br div dl dt dd fieldset footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table td th tr ul".split())
_CHALLENGES = ("cf-chl-", "challenge-platform", "g-recaptcha", "h-captcha", "hcaptcha")


def normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class PageCheckFailure(Exception):
    def __init__(self, code):
        self.code = code


class StaticPage(HTMLParser):
    def __init__(self, target_url: str):
        super().__init__(convert_charrefs=True)
        self.target_url = target_url
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.links: set[str] = set()
        self.anchors: set[str] = set()
        self.nodes = 0
        self.restricted = False

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 20000 or len(self.stack) >= 128:
            raise PageCheckFailure("parse_failed")
        a = dict(attrs)
        if tag in {"plaintext", "xmp"}:
            raise PageCheckFailure("parse_failed")
        if (tag == "input" and (a.get("type") or "").casefold() == "password"
                or any(marker in (a.get(key) or "").casefold()
                       for marker in _CHALLENGES for key in ("id", "class", "src"))):
            self.restricted = True
        # A <base> can change every relative destination: do not guess its intent.
        if tag == "base" and a.get("href"):
            raise PageCheckFailure("parse_failed")
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in _EXCLUDED
                  or "hidden" in a or (a.get("aria-hidden") or "").casefold() == "true"
                  or tag == "input" and (a.get("type") or "").casefold() == "hidden")
        if not hidden:
            if tag in _BLOCKS:
                self.parts.append("\n")
            for key in (("id", "name") if tag == "a" else ("id",)):
                if a.get(key) and len(a[key]) <= 128:
                    self.anchors.add(a[key])
            if tag == "a" and a.get("href"):
                try:
                    self.links.add(page_url(urljoin(self.target_url, a["href"]), site=self.target_url))
                except ValueError:
                    pass  # Do not retain external, credential or query-bearing destinations.
        if tag not in _VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                hidden = self.stack[index][1]
                del self.stack[index:]
                if not hidden and tag in _BLOCKS:
                    self.parts.append("\n")
                return

    def handle_data(self, data):
        if not (self.stack and self.stack[-1][1]):
            self.parts.append(data)

    def extract(self, html: str):
        if "\ufffd" in html or "\x00" in html or "<![" in html:
            raise PageCheckFailure("parse_failed")
        try:
            self.feed(html)
            self.close()
        except (AssertionError, ValueError):
            raise PageCheckFailure("parse_failed") from None
        text = normalized_text("".join(self.parts))
        if self.restricted or any(marker in text for marker in (
            "verify you are human", "checking your browser", "access denied", "just a moment...",
        )):
            raise PageCheckFailure("access_restricted")
        if not text and not self.links and not self.anchors:
            raise PageCheckFailure("empty_page")
        return text, self.links, self.anchors


@dataclass(frozen=True)
class ConservativeRobotsPolicy:
    parsed: RobotsPolicy
    denied_paths: tuple[str, ...]

    def allowed(self, url: str) -> bool:
        # Honor all explicit disallows, even in another bot's group. This can
        # decline a permitted page but cannot use first-match precedence to
        # bypass a more specific disallow. Unsupported glob rules fail closed.
        path = unquote(urlsplit(url).path)
        return self.parsed.allowed(url) and not any(path.startswith(p) for p in self.denied_paths)


def robots_policy(body: bytes, root: str):
    try:
        text = body.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        raise PageCheckFailure("robots_invalid") from None
    if "<" in text or any(unicodedata.category(c).startswith("C") and c not in "\r\n\t" for c in text):
        raise PageCheckFailure("robots_invalid")
    has_agent = False
    meaningful = False
    denied_paths = []
    delays = [0.0]
    for raw in text.splitlines():
        line = raw.partition("#")[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise PageCheckFailure("robots_invalid")
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.casefold()
        if key in {"user-agent", "sitemap"}:
            meaningful = True
        if key == "user-agent":
            if not value:
                raise PageCheckFailure("robots_invalid")
            has_agent = True
        if key in {"allow", "disallow", "crawl-delay", "request-rate"} and not has_agent:
            raise PageCheckFailure("robots_invalid")
        if key in {"allow", "disallow"}:
            if value and (not value.startswith("/") or "*" in value or "$" in value
                          or re.search(r"%(?![0-9a-fA-F]{2})", value)):
                raise PageCheckFailure("robots_invalid")
            if key == "disallow" and value:
                denied_paths.append(unquote(value))
        if key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                raise PageCheckFailure("robots_invalid") from None
            if not math.isfinite(delay) or delay < 0:
                raise PageCheckFailure("robots_invalid")
            delays.append(delay)
        if key == "request-rate":
            match = re.fullmatch(r"([1-9]\d*)/([1-9]\d*)", value)
            if match is None:
                raise PageCheckFailure("robots_invalid")
            delays.append(int(match[2]) / int(match[1]))
    # Conservatively honor even other groups' delays; never shorten a declared delay.
    if not meaningful and any(line.partition("#")[0].strip() for line in text.splitlines()):
        raise PageCheckFailure("robots_invalid")
    policy = ConservativeRobotsPolicy(parse_robots(text, root_url=root), tuple(denied_paths))
    return policy, max(delays)


async def collect_page_implementation(plan: PageCheckPlan, *, observation_id: UUID,
                                      resolver=None, client_factory=None, now=None) -> PageObservation:
    plan = checked_plan(plan)
    if not isinstance(observation_id, UUID):
        raise ValueError("observation UUID required")
    clock = now or (lambda: datetime.now(timezone.utc))
    started = clock()
    if started.tzinfo is None or started.utcoffset() is None:
        raise ValueError("aware observation clock required")
    if started.astimezone(timezone.utc) < instant(plan.binding.implementation_recorded_at):
        raise ValueError("observation predates implementation record")
    parts = urlsplit(plan.target_url)
    request_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    root = plan.binding.site_url
    scope = SiteScope(root, "https", parts.hostname, frozenset({parts.hostname}))
    fetcher = BoundedSiteFetcher(connect_timeout=5, read_timeout=10, total_timeout=30,
        max_redirects=0, concurrency=1, requests_per_second=1, resolver=resolver,
        client_factory=client_factory, redirect_limit_code="redirect_requires_confirmation")
    stage = "robots"
    failure = None
    http_status = media_type = response_bytes = content_checksum = None
    states = ["unable_to_check"] * len(plan.conditions)
    try:
        async with asyncio.timeout(30):
            response = await fetcher.fetch(root + "robots.txt", scope=scope, max_bytes=262144)
            if response.status_code not in {404, 410}:
                if response.status_code != 200:
                    raise PageCheckFailure("robots_unavailable")
                policy, delay = robots_policy(response.body, root)
                if not policy.allowed(request_url):
                    raise PageCheckFailure("robots_disallowed")
                if delay >= 30:
                    raise PageCheckFailure("crawl_delay_exceeds_budget")
                fetcher.apply_crawl_delay(delay)
            stage = "page"
            response = await fetcher.fetch(request_url, scope=scope, max_bytes=1048576)
            http_status = response.status_code
            response_bytes = response.response_bytes
            if response.status_code != 200:
                raise PageCheckFailure("http_status")
            content_type = response.content_type.split(";", 1)[0].strip().casefold()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise PageCheckFailure("content_unsupported")
            media_type = content_type
            text, links, anchors = StaticPage(request_url).extract(response.decode())
            states = ["observed" if (
                normalized_text(c.value) in text if c.kind == "text_present" else
                c.value in links if c.kind == "link_present" else c.value in anchors
            ) else "not_observed" for c in plan.conditions]
            content_checksum = "sha256:" + hashlib.sha256(response.body).hexdigest()
    except PageCheckFailure as error:
        failure = error.code
    except SiteFetchError as error:
        failure = error.code if error.code in {
            "unsafe_target", "redirect_requires_confirmation", "page_timeout", "http_error",
            "content_unsupported", "response_too_large",
        } else "http_error"
    except UrlSafetyError:
        failure = "unsafe_target"
    except UrlUnreachableError:
        failure = "dns_unavailable"
    except TimeoutError:
        failure = "page_timeout"
    except (UnicodeError, ValueError, RecursionError):
        failure = "robots_invalid" if stage == "robots" else "parse_failed"
    return checksum_model(PageObservation, dict(
        schema_version="v22_page_implementation_observation_v1", id=str(observation_id),
        plan=plan.model_dump(mode="json"), observed_at=clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        status="unable_to_check" if failure else "checked", failure=failure,
        failure_stage=stage if failure else None, http_status=http_status, media_type=media_type,
        response_bytes=response_bytes, content_checksum=content_checksum,
        results=[dict(condition_id=c.id, state=s) for c, s in zip(plan.conditions, states, strict=True)],
        limitation="static_response_only_rendering_functionality_and_identity_not_verified",
        effect_conclusion=None, causal_eligible=False,
    ))
