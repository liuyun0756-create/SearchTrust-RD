"""Local candidate-only collection. Never connects to a database or confirms identity."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Provider exception strings and HTTP URLs must never reach log handlers.
logging.disable(logging.CRITICAL)

from dotenv import dotenv_values
import fakeredis.aioredis
import httpx
from app.core.config import Settings
from app.collectors.serp_market_models import SerpMarketContext
from app.competitors_v22.candidates import _BLOCKED_PLATFORM_DOMAINS
from app.competitors_v22.normalization import normalize_domain
from app.integrations.serpapi import configured_serpapi_keys, execute_serpapi_get
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.serp_market_stage import build_serp_market_stage
from app.preflight_v22.extractors import extract_site_signals
from app.preflight_v22.gbp import LimitedGbpLookup

SITES = [
    dict(id="pr-plumbing-fresno", name="PR Plumbing Fresno", url="https://www.prplumbingfresno.com/", service="Plumbing repair", city="Fresno", region="CA", country="US", queries=["Plumbing repair Fresno", "Plumbing repair near me", "best Plumbing repair Fresno"], gbp="https://www.google.com/maps?data=0x80945db06cd06e31:0x812f6a121ffb2bc6"),
    dict(id="all-star-plumbing-fresno", name="All Star Plumbing Fresno", url="https://allstarplumbingfresno.com/", service="Plumbing repair", city="Fresno", region="CA", country="US", queries=["Plumbing repair Fresno", "Plumbing repair near me", "best Plumbing repair Fresno"]),
    dict(id="vision-tech-swansea", name="Vision-Tech AV", url="https://visiontechav.com/", target="https://visiontechav.com/swansea/", service="Commercial AV installation", city="Swansea", region=None, country="GB", queries=["AV installation Swansea", "audio visual company Swansea", "commercial AV integration Swansea"]),
    dict(id="seamless-gutters-pensacola", name="Seamless Gutters", url="https://seamlessgutterspensacola.com/", target="https://seamlessgutterspensacola.com/gutter-installation/", service="Seamless gutter installation", city="Pensacola", region="FL", country="US", queries=["gutter installation Pensacola", "seamless gutters Pensacola", "gutter installation near me"], gbp="https://maps.app.goo.gl/8MMJyNe2na4og54U7"),
    dict(id="volcanoes-hiking-guides", name="Volcanoes Hiking Guides", url="https://www.volcanoeshikingguides.com/", service="Guided volcano hiking tours", city="Hilo", region="HI", country="US", queries=["volcano tours Big Island", "guided volcano hike Hawaii", "Hawaii Volcanoes National Park tour"]),
]


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_text(value, secrets):
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r'(?i)(api_key(?:=|%3d))[^&\s"<>]+', r'\1[REDACTED]', text)
    return text + "\n"


class Budget:
    """Durably reserve each attempt BEFORE any HTTP call; never reset a run."""
    def __init__(self, directory, secrets):
        self.directory, self.secrets, self.events = directory, secrets, []

    async def claim(self, operation):
        if len(self.events) >= 185 or Decimal(len(self.events) + 1) * Decimal("0.025") > Decimal("5"):
            raise RuntimeError("approved_budget_exhausted")
        event = dict(id=str(uuid4()), operation=operation, reserved_at=now(), outcome="reserved")
        # One exclusive file per event: an interrupted call stays charged to the
        # local allowance. Existing output directories cannot be reused/reset.
        (self.directory / f"attempt-{len(self.events)+1:03}.json").write_text(safe_text(event, self.secrets))
        self.events.append(event)
        return SimpleNamespace(claim_id=event["id"])

    async def complete(self, claim_id, *, outcome, duration_ms):
        event = next(e for e in self.events if e["id"] == str(claim_id))
        # Completion is a new record, never an overwrite of a reservation.
        (self.directory / f"completed-{event['id']}.json").write_text(safe_text(dict(event, outcome=outcome, duration_ms=duration_ms), self.secrets))

    async def record_checkpoint_hit(self, **kwargs):
        pass


def q1_candidates(snapshot, site):
    """Preserve source order, including exclusion reasons; no entity is confirmed."""
    rows = []
    for result in snapshot.query_runs[0].results:
        row = result.model_dump(mode="json")
        domain = normalize_domain(str(result.url)) if result.url else None
        reason = None
        if domain == normalize_domain(site["url"]):
            reason = "client_domain"
        elif domain and any(domain == d or domain.endswith('.' + d) for d in _BLOCKED_PLATFORM_DOMAINS):
            reason = "directory_or_platform"
        elif domain and (domain == "nps.gov" or domain.endswith(".nps.gov")):
            reason = "national_park_not_business"
        elif not domain:
            reason = "website_identity_unresolved"
        row.update(exclusion_reason=reason, confirmation="not_confirmed",
                   manual_checks=["same_service_and_market", "not_chain_lead_generation_page", "unique_business_identity"])
        rows.append(row)
    return rows


async def collect(output):
    # Only whitelisted provider secrets are used. No database configuration is
    # instantiated into a client, and no real Redis/website account is used.
    env = dotenv_values(ROOT / ".env")
    names = ("SERPAPI_KEY", "SERPAPI_KEY_SECONDARY", "SERPAPI_KEY_TERTIARY")
    values = {name: env.get(name) or "" for name in names}
    keys = configured_serpapi_keys(*values.values())
    secrets = [*keys, env.get("FIRECRAWL_API_KEY") or ""]
    if not keys:
        raise RuntimeError("serpapi_keys_unavailable")
    # Refuse a second run that could reset the approved cumulative counters.
    output.mkdir(parents=True, exist_ok=False)
    attempts = output / "attempts"; attempts.mkdir()
    budget = Budget(attempts, secrets)
    settings = Settings(_env_file=None, **values)
    redis = fakeredis.aioredis.FakeRedis()
    checkpoints = JobCheckpoints(redis, prefix="p009-local-candidates", ttl_seconds=86400)
    stage = build_serp_market_stage(settings)
    results = []
    try:
        for site in SITES:
            directory = output / site["id"]; directory.mkdir()
            def save(name, value):
                with (directory / name).open("x") as file:
                    file.write(safe_text(value, secrets))
            start = len(budget.events)
            result = dict(site=site, started_at=now(), status="awaiting_reviewer_confirmation")
            async def provider(params):
                async def before(slot, fingerprint):
                    await budget.claim("serpapi_gbp_candidate")
                async with httpx.AsyncClient(timeout=40, trust_env=False, follow_redirects=False) as client:
                    response = await execute_serpapi_get(client, params, keys=keys, base_url="https://serpapi.com/search", before_attempt=before)
                return response.payload
            try:
                html = (ROOT / "tests/fixtures/gold" / site["id"] / "page.html").read_text()
                signals = extract_site_signals(html)
                lookup = await LimitedGbpLookup(provider=provider, configured=True).lookup(
                    site_url=site["url"], signals=signals, gbp_url=site.get("gbp") or signals.gbp_url)
                result["gbp_lookup"] = asdict(lookup)
                result["gbp_matching_hints"] = dict(site_phone_observations=[asdict(p) for p in signals.phones],
                    note="Hints and API mode candidates are observations only; no BusinessIdentity or truth is confirmed.")
            except Exception as exc:
                result["gbp_error_type"] = type(exc).__name__
            # MarketContext requires no operating model or confirmed identity.
            context = SerpMarketContext(target_market=dict(display_name=", ".join(p for p in (site["city"],site["region"],site["country"]) if p),
                city=site["city"], region=site["region"], country_code=site["country"]), queries=site["queries"], device="mobile", language="en")
            try:
                snapshot = await stage.collect_context(job_id=uuid4(), context=context, checkpoints=checkpoints, cost_ledger=budget)
                save("market-snapshot.json", snapshot.model_dump(mode="json"))
                result["q1_competitor_observations"] = q1_candidates(snapshot, site)
                result["selection_note"] = "Q1 observations retain source ordering and exclusions. Reviewer must resolve identities and select up to three; no automatic confirmation."
            except Exception as exc:
                result["market_error_type"] = type(exc).__name__
                result["market_error_code"] = getattr(exc, "error_code", None)
            result.update(completed_at=now(), serpapi_attempts=len(budget.events)-start)
            save("candidates.json", result)
            results.append(dict(site_id=site["id"], serpapi_attempts=result["serpapi_attempts"],
                                gbp_candidates=len(result.get("gbp_lookup",{}).get("candidates",[])),
                                q1_observations=len(result.get("q1_competitor_observations",[])),
                                market_error_type=result.get("market_error_type")))
            print(json.dumps(results[-1]), flush=True)
        summary = dict(status="STOPPED_AWAITING_CONFIRMATION", sites=results, serpapi_attempts=len(budget.events),
            firecrawl_calls=0, firecrawl_credits=0, usd_usage_reserve=str(Decimal(len(budget.events))*Decimal("0.025")),
            actual_invoice_usd=None, billing_note="Request attempts counted; successful billing units and shared-account invoice not queried.",
            remaining_serpapi_attempts=185-len(budget.events), remaining_firecrawl_credits=30,
            remaining_usd_reserve=str(Decimal("5")-Decimal(len(budget.events))*Decimal("0.025")))
        with (output / "summary.json").open("x") as file: file.write(safe_text(summary, secrets))
        print(safe_text(summary, secrets), flush=True)
    finally:
        await redis.aclose()


def self_test():
    import tempfile
    from types import SimpleNamespace as Obj
    secret = "synthetic-secret-for-test"
    assert secret not in safe_text(dict(url="https://test/?api_key="+secret), [secret])
    assert "unlisted-secret" not in safe_text(dict(url="https://test/?api_key=unlisted-secret"), [])
    def item(url):
        return Obj(url=url, model_dump=lambda **kw: dict(url=url))
    rows = q1_candidates(Obj(query_runs=[Obj(results=[item("https://www.prplumbingfresno.com/"), item("https://yelp.com/"), item("https://nps.gov/"), item("https://other.test/")])]), SITES[0])
    assert [r["exclusion_reason"] for r in rows] == ["client_domain", "directory_or_platform", "national_park_not_business", None]
    assert all(r["confirmation"] == "not_confirmed" for r in rows)
    async def budget_test():
        with tempfile.TemporaryDirectory() as directory:
            b = Budget(Path(directory), [secret])
            for _ in range(185): await b.claim("test")
            try: await b.claim("test")
            except RuntimeError: pass
            else: raise AssertionError("Budget exceeded")
            assert len(list(Path(directory).glob("attempt-*.json"))) == 185
    asyncio.run(budget_test())
    print("PASS: secret redaction, source ordering/exclusions, no confirmation, durable 185-attempt budget")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test: self_test()
        elif args.output: asyncio.run(collect(args.output))
        else: parser.error("--output or --self-test is required")
    except Exception as exc:
        # Never print tracebacks or provider exception strings with live keys.
        print(json.dumps(dict(status="STOPPED", error_type=type(exc).__name__)), flush=True)
        raise SystemExit(1) from None
