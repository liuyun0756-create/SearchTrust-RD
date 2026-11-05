"""One approved, bounded static contact capture. Never loads service credentials.

Raw bytes stay outside both repositories. A pre-existing destination prevents a
second run from silently consuming another approved request budget.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import gzip
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.collectors.site_inventory_discovery import parse_robots
from app.collectors.site_inventory_fetcher import BoundedSiteFetcher
from app.collectors.site_inventory_urls import SiteScope, canonicalize_inventory_url

HOME = "https://www.volcanoeshikingguides.com/"
CONTACT = HOME + "contact-us"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.urls.append(href)


async def capture(private_dir, summary_dir, *, resume_robots_sha256=None):
    private_dir = private_dir.resolve()
    summary_dir = summary_dir.resolve()
    audit_name = "capture-resumed.json" if resume_robots_sha256 else "capture.json"
    saved_robots = None
    if resume_robots_sha256:
        if (private_dir / audit_name).exists() or (summary_dir / audit_name).exists():
            raise FileExistsError("resume_already_attempted")
        audit = json.loads((private_dir / "capture.json").read_text())
        saved_robots = (private_dir / "robots.body").read_bytes()
        if hashlib.sha256(saved_robots).hexdigest() != resume_robots_sha256:
            raise ValueError("robots_checksum_mismatch")
        if (audit["status"], audit["current_step"], audit["reason"], audit["logical_fetches"], len(audit["http_requests"])) != ("stopped", "robots", "ValueError", 1, 1):
            raise ValueError("unexpected_resume_state")
        if audit["http_requests"][0].get("status_code") != 200:
            raise ValueError("robots_response_not_successful")
        audit.update(status="running", resumed_at=now(), reused_robots_sha256=resume_robots_sha256)
        audit.pop("reason", None)
    else:
        private_dir.mkdir(parents=True, exist_ok=False)
        summary_dir.mkdir(parents=True, exist_ok=False)
        audit = dict(schema_version="p1_00b_contact_capture_v1", started_at=now(),
                     status="running", logical_fetches=0, http_requests=[], responses=[],
                     serpapi_calls=0, firecrawl_credits=0, llm_calls=0, api_cost_usd=0)
    scope = SiteScope.from_root(HOME)
    policy = None

    def save():
        data = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
        (private_dir / audit_name).write_text(data)
        (summary_dir / audit_name).write_text(data)

    async def request_hook(request):
        if len(audit["http_requests"]) >= 24:
            raise RuntimeError("http_budget_exhausted")
        if policy is not None and not policy.allowed(str(request.url)):
            raise RuntimeError("robots_disallowed")
        audit["http_requests"].append(dict(url=str(request.url), requested_at=now()))
        save()

    async def response_hook(response):
        audit["http_requests"][-1].update(status_code=response.status_code,
            content_type=response.headers.get("content-type"),
            location=response.headers.get("location"))
        save()

    fetcher = BoundedSiteFetcher(connect_timeout=5, read_timeout=15,
        total_timeout=30, max_redirects=5, concurrency=1, requests_per_second=1)
    default_client = fetcher.client_factory

    def client_factory(target):
        client = default_client(target)
        client.event_hooks = dict(request=[request_hook], response=[response_hook])
        return client

    fetcher.client_factory = client_factory

    async def fetch(name, url, max_bytes, *, prefix=False):
        if audit["logical_fetches"] >= 4:
            raise RuntimeError("logical_budget_exhausted")
        audit["logical_fetches"] += 1
        audit["current_step"] = name
        save()
        response = await fetcher.fetch(url, scope=scope, max_bytes=max_bytes,
            allow_prefix=prefix)
        raw_path = private_dir / (name + ".body")
        raw_path.write_bytes(response.body)
        entry = dict(name=name, requested_url=url, final_url=response.final_url,
            collected_at=now(), status_code=response.status_code,
            content_type=response.content_type, bytes=len(response.body),
            truncated=response.truncated,
            sha256="sha256:" + hashlib.sha256(response.body).hexdigest(),
            private_file=str(raw_path.relative_to(ROOT.parent)))
        audit["responses"].append(entry)
        save()
        if response.status_code != 200:
            raise RuntimeError("unexpected_http_status")
        if name != "robots" and response.content_type.split(";", 1)[0].lower() not in {"text/html", "application/xhtml+xml"}:
            raise RuntimeError("unexpected_content_type")
        if name == "contact":
            data = gzip.compress(response.body, mtime=0)
            (summary_dir / "contact.html.gz").write_bytes(data)
            entry["snapshot_file"] = "contact.html.gz"
            entry["snapshot_sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
            save()
        return response

    try:
        # Fetch policy first; the production helper's allow-on-error behaviour
        # is deliberately not used for this approved fail-and-stop exercise.
        if saved_robots is None:
            robots = await fetch("robots", HOME + "robots.txt", 256_000)
            robots_text = robots.decode()
        else:
            robots_text = saved_robots.decode("utf-8")
        if "<html" in robots_text.lower():
            raise RuntimeError("unexpected_robots_content")
        policy = parse_robots(robots_text, root_url=HOME)
        audit["robots_checks"] = {url: policy.allowed(url) for url in (HOME, CONTACT)}
        save()
        if not all(audit["robots_checks"].values()):
            raise RuntimeError("robots_disallowed")
        fetcher.apply_crawl_delay(policy.crawl_delay)
        page = await fetch("home-prefix", HOME, 256_000, prefix=True)

        def has_contact(response):
            links = Links()
            links.feed(response.decode())
            return any(canonicalize_inventory_url(url, scope=scope, base_url=response.final_url) == CONTACT for url in links.urls)

        if not has_contact(page) and page.truncated:
            page = await fetch("home-link-scan", HOME, 4_000_000)
        if not has_contact(page):
            raise RuntimeError("contact_link_not_found")
        await fetch("contact", CONTACT, 4_000_000)
        audit["status"] = "captured_pending_content_review"
    except Exception as exc:
        audit["status"] = "stopped"
        # Only local/static error codes. Do not serialize arbitrary exception text.
        audit["reason"] = getattr(exc, "code", None) or (str(exc) if type(exc) is RuntimeError else type(exc).__name__)
    finally:
        audit["completed_at"] = now()
        save()
    print(json.dumps({k: audit[k] for k in ("status", "logical_fetches", "api_cost_usd")} | {
        "http_requests": len(audit["http_requests"]), "reason": audit.get("reason")}, ensure_ascii=False))
    return 0 if audit["status"] == "captured_pending_content_review" else 1


def now():
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, required=True)
    parser.add_argument("--resume-robots-sha256", help="Explicitly approved resume; preserves the failed capture and cumulative counters")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(capture(args.private_dir, args.summary_dir, resume_robots_sha256=args.resume_robots_sha256)))
