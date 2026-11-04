#!/usr/bin/env python3
"""Derive P0-10 inputs from saved sources and archive raw files; never fetch data."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GOLD = ROOT / "tests/fixtures/gold"
PRIVATE = ROOT.parent / ".release-private/2026-11-03-gold"
FULL = "p0-09-full-continuation-2026-11-03"
TAIL = "p0-09-volcanoes-partial-2026-11-03"
SITES = ("pr-plumbing-fresno", "all-star-plumbing-fresno", "vision-tech-swansea", "seamless-gutters-pensacola", "volcanoes-hiking-guides")

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.capability_inputs import validate_generation_input
from app.competitors_v22.normalization import normalize_domain
from app.competitors_v22.models import CompetitorCollectionSnapshot


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_saved(relative):
    path = GOLD / relative
    if not path.is_file():
        path = PRIVATE / relative
    return path.read_bytes()


def load(relative):
    return json.loads(read_saved(relative))


def save_once(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"refusing_to_change_saved_output:{path.name}")
    else:
        path.write_bytes(data)


def recovered_competitors(payload, checkpoints):
    result = deepcopy(payload)
    recovered = []
    for item in result["competitors"]:
        domain = normalize_domain(item["competitor"]["website_url"])
        matches = []
        for row in checkpoints:
            if payload["job_id"] not in row["key"]:
                continue
            try:
                value = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            if (isinstance(value, dict) and value.get("schema_version") == "site_inventory_snapshot_v1"
                    and value["deep_analysis_limit"] == 10
                    and normalize_domain("https://" + value["canonical_host"]) == domain):
                matches.append(value)
        if item["site_inventory"] is None and matches:
            if len(matches) != 1:
                raise ValueError("ambiguous_saved_competitor_inventory")
            inventory = matches[0]
            item.update(site_inventory=inventory, analyzed_page_count=inventory["deep_analyzed_count"],
                        site_status="partial" if inventory["limitations"] else "available")
            item["limitations"] = [n for n in item["limitations"] if n != "The competitor website could not be collected within safe limits."]
            item["limitations"] = list(dict.fromkeys([*item["limitations"], *inventory["limitations"]]))
            recovered.append(dict(domain=domain, payload_sha256=sha(canonical_json_bytes(inventory)),
                                  started_at=inventory["started_at"], completed_at=inventory["completed_at"]))
    CompetitorCollectionSnapshot.model_validate_json(canonical_json_bytes(result))
    return result, recovered


def derive_existing(site, checkpoints):
    relative = f"{site}/bound-p0-09-2026-11-03.json"
    raw = read_saved(relative)
    bound = json.loads(raw)
    manifest, request = bound["manifest"], bound["request"]
    source = next(s for s in request["evidence_input"]["sources"] if s["kind"] == "competitor")
    original_id = source["binding"]["snapshot_id"]
    source["payload"], recovered = recovered_competitors(source["payload"], checkpoints)
    checksum = request_digest(source["payload"])
    source["binding"].update(payload_checksum=checksum, snapshot_id=str(uuid5(NAMESPACE_URL, "p010-competitor:" + checksum)))
    ref = next(s for s in manifest["snapshots"] if s["snapshot_id"] == original_id)
    ref.update(payload_checksum=checksum, snapshot_id=source["binding"]["snapshot_id"])
    manifest["ruleset_version"] = "v22_capability_public_v9"
    manifest["generation_input_checksum"] = verified_request_digest(request)
    manifest["input_checksum"] = verified_request_digest({k: v for k, v in manifest.items() if k != "input_checksum"})
    validate_generation_input(manifest, request)
    return bound, dict(source=relative, source_sha256=sha(raw), recovered_competitor_sites=recovered,
                      sources=[dict(kind=s["kind"], fetched_at=s["binding"]["fetched_at"], payload_checksum=s["binding"]["payload_checksum"]) for s in request["evidence_input"]["sources"]])


async def derive_volcanoes():
    from app.api.v2.models import AnalyzeRequest
    from app.api.v2.competitor_models import CompetitorDiscoveryResult
    from app.collectors.site_inventory import SiteInventoryCollector
    from app.collectors.site_inventory_fetcher import SiteFetchResponse, SiteFetchError
    from app.competitors_v22.models import SharedMarketSnapshot
    from app.jobs_v22.customer_public_gbp_stage import CustomerPublicGbpCollection
    from app.jobs_v22.capability_acquisition_input import build_acquisition_input
    site = "volcanoes-hiking-guides"
    paths = [f"{site}/page.html", f"{site}/capture-retry.json", f"{FULL}/{site}/confirmed-request.json",
             f"{FULL}/{site}/shared-market.json", f"{FULL}/{site}/discovery.json", f"{FULL}/{site}/customer-gbp.json", f"{TAIL}/competitors.json"]
    body, capture = read_saved(paths[0]), load(paths[1])
    assert sha(body) == capture["sha256"]
    observed = datetime.fromisoformat(capture["completed_at"])
    class SavedFetcher:
        async def fetch(self, url, *, max_bytes, allow_prefix=False, **kwargs):
            if url != capture["requested_url"]:
                # No saved response does not mean an HTTP 404 or missing page.
                raise SiteFetchError("http_error", "No saved response for offline replay.")
            if len(body) > max_bytes and not allow_prefix:
                raise SiteFetchError("response_too_large", "Saved response exceeds the unchanged limit.")
            return SiteFetchResponse(url, capture["final_url"], capture["http_status"], capture["content_type"],
                                     body[:max_bytes], truncated=len(body) > max_bytes)
        def apply_crawl_delay(self, seconds):
            pass
    inventory = await SiteInventoryCollector(fetcher=SavedFetcher(), firecrawl=None, structural_max_bytes=256_000,
        deep_max_bytes=2_000_000, sitemap_max_bytes=2_000_000, sitemap_decompressed_max_bytes=5_000_000,
        sitemap_max_files=5, sitemap_max_depth=2, batch_size=2, resource_limits=True, clock=lambda: observed).collect(
            site_url=capture["requested_url"], discovery_limit=500, deep_analysis_limit=50)
    original = load(paths[2])
    original["business_identity"]["primary_location"] = original["target_market"]
    request = AnalyzeRequest.model_validate_json(canonical_json_bytes(original))
    # Local binding envelopes are derived anew; observations and their times stay original.
    from app.competitors_v22.selection import analysis_discovery_input_digest
    shared_raw, discovery_raw, rivals_raw = load(paths[3]), load(paths[4]), load(paths[6])
    shared_raw["input_digest"] = analysis_discovery_input_digest(request)
    shared_raw["snapshot_id"] = str(uuid5(NAMESPACE_URL, "p010-volcanoes-market:" + shared_raw["input_digest"]))
    discovery_raw.update(input_digest=shared_raw["input_digest"], market_snapshot_id=shared_raw["snapshot_id"],
        discovery_id=str(uuid5(NAMESPACE_URL, "p010-volcanoes-discovery")))
    discovery_raw["candidate_digest"] = request_digest(dict(input_digest=shared_raw["input_digest"], candidates=discovery_raw["candidates"]))
    job = uuid5(NAMESPACE_URL, "p010-volcanoes-offline-derived")
    rivals_raw.update(job_id=str(job), discovery_id=discovery_raw["discovery_id"], candidate_digest=discovery_raw["candidate_digest"], market_snapshot_id=shared_raw["snapshot_id"])
    shared = SharedMarketSnapshot.model_validate_json(canonical_json_bytes(shared_raw))
    rivals = CompetitorCollectionSnapshot.model_validate_json(canonical_json_bytes(rivals_raw))
    gbp = CustomerPublicGbpCollection.model_validate_json(read_saved(paths[5]))
    created = observed
    now = max(observed, shared.snapshot.completed_at, rivals.completed_at, gbp.snapshot.completed_at) + timedelta(seconds=1)
    user = uuid5(NAMESPACE_URL, "p009-local-test-no-account")
    business = dict(id=str(uuid5(NAMESPACE_URL, "p010-volcanoes-scope")), user_id=str(user), case_id=str(request.case_id), revision=1,
        site_url=str(request.business_identity.site_url), primary_service=request.primary_service, target_market=request.target_market.display_name,
        target_pages=[str(request.business_identity.site_url)], competitor_sites=[str(c.website_url) for c in request.competitors], priority=None,
        confirmation_source="seo_confirmation", confirmed_at=created.isoformat().replace("+00:00", "Z"))
    collected = SimpleNamespace(site_inventory=inventory, shared_market=shared, competitor_collection=rivals,
        customer_public_gbp=gbp, discovery=CompetitorDiscoveryResult.model_validate_json(canonical_json_bytes(discovery_raw)))
    manifest, value = build_acquisition_input(user_id=user, job_id=job, business=business, request=request,
        collected=collected, created_at=created, deadline_at=now+timedelta(hours=1), now=now)
    return dict(manifest=manifest.model_dump(mode="json"), request=value.model_dump(mode="json")), dict(
        derivation="Offline replay of P0-08 captured homepage with P0-09 confirmed observations; no new collection.",
        sources=[dict(path=p, sha256=sha(read_saved(p))) for p in paths],
        homepage_capture=capture, market_observed_at=shared.snapshot.completed_at.isoformat(),
        competitors_observed_at=rivals.completed_at.isoformat(), gbp_observed_at=gbp.snapshot.completed_at.isoformat(),
        render="not_run; no complete static body; not forced", unsaved_responses="not_checked",
        structural_links=len(inventory.pages[0].internal_links), deep_analyzed_count=inventory.deep_analyzed_count)


async def prepare():
    # Enforce offline even if future helper implementations attempt network access.
    def denied(*args, **kwargs):
        raise RuntimeError("P0-10 replay must remain offline")
    socket.socket.connect = denied
    socket.getaddrinfo = denied
    checkpoints = load(f"{FULL}/local-checkpoints.json")
    prepared = []
    for site in SITES:
        bound, provenance = await derive_volcanoes() if site == SITES[-1] else derive_existing(site, checkpoints)
        validate_generation_input(bound["manifest"], bound["request"])
        prepared.append((site, canonical_json_bytes(bound), provenance))
    for site, data, provenance in prepared:
        directory = GOLD / site
        raw_descriptor = (directory / "input.json").read_bytes()
        descriptor = json.loads(raw_descriptor)
        if (descriptor.get("generation") or {}).get("input", {}).get("path") != "bound-p0-10.json.gz":
            save_once(PRIVATE / site / "input-before-p0-10.json", raw_descriptor)
        compressed = gzip.compress(data, mtime=0)
        save_once(directory / "bound-p0-10.json.gz", compressed)
        provenance.update(bound_sha256=sha(data), compressed_sha256=sha(compressed), external_calls=0)
        save_once(directory / "p0-10-provenance.json", canonical_json_bytes(provenance))
        ref = dict(base="case", path="bound-p0-10.json.gz", sha256=sha(compressed), encoding="gzip")
        descriptor["snapshot_files"] = [r for r in descriptor["snapshot_files"] if not r["path"].startswith("bound")]+[ref]
        descriptor["generation"] = dict(kind="bound_input", input=ref)
        descriptor.pop("pending_input", None)
        (directory / "input.json").write_text(json.dumps(descriptor, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(dict(bound_sites=list(SITES), external_calls=0)))


def archive():
    retained_names = {"summary.json", "final-budget.json", "checksums.json", "checksums-v2.json", "delivery-checksums.json"}
    files = list(GOLD.glob("p0-09-*/**/*")) + list(GOLD.glob("*/bound-p0-09-2026-11-03.json"))
    rows = []
    for path in sorted(p for p in files if p.is_file()):
        rel = path.relative_to(GOLD)
        data = path.read_bytes()
        destination = PRIVATE / rel
        save_once(destination, data)
        keep = path.name in retained_names or path.suffix == ".md"
        rows.append(dict(original_path=str(rel), archive_path=str(rel), sha256=sha(data), bytes=len(data), retained_in_repo=keep))
        if not keep:
            path.unlink()
    manifest = GOLD / "p0-10-storage-manifest.json"
    if manifest.exists():
        old = json.loads(manifest.read_text())["files"]
        rows = list({r["original_path"]: r for r in [*old, *rows]}.values())
    manifest.write_text(json.dumps(dict(private_root=".release-private/2026-11-03-gold", files=sorted(rows, key=lambda r:r['original_path'])), indent=2)+"\n")
    for row in rows:
        assert sha((PRIVATE / row["archive_path"]).read_bytes()) == row["sha256"]
    size = sum(p.stat().st_size for p in GOLD.rglob("*") if p.is_file())
    assert size < 30_000_000, size
    print(json.dumps(dict(gold_bytes=size, archived_files=len(rows), checksums_verified=True)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "archive"])
    args = parser.parse_args()
    if args.operation == "prepare":
        asyncio.run(prepare())
    else:
        archive()
