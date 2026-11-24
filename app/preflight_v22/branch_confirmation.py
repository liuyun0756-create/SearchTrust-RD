"""Short-lived proof of GBP evidence already obtained by preflight."""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from app.api.v2.models import PreflightBranchOption
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_common.google_business import data_cid, extract_data_id, extract_google_place_id
from app.report_v22.evidence_bindings import host
from app.report_v22.identity_inheritance import mode_material
from app.report_v22.operating_model import extract_mode_evidence
from app.report_v22.site_business_html import ParseBudget
from app.report_v22.site_business_models import SiteBusinessLimits
from app.report_v22.site_gbp_comparators import normalize_phone


def entity_keys(url):
    query = parse_qs(urlsplit(url).query)
    data_id = extract_data_id(url)
    cid = query.get("cid", [None])[0] or data_cid(data_id)
    place_id = extract_google_place_id(url)
    return {kind: value for kind, value in (
        ("cid", cid if cid and cid.isascii() and cid.isdigit() else None),
        ("place_id", place_id), ("data_id", data_id),
    ) if value}


def signed_evidence(proof, key):
    payload = base64.urlsafe_b64encode(canonical_json_bytes(proof)).decode().rstrip("=")
    if len(payload) > 24935:
        proof = {**proof, "mode_material": [], "mode_evidence_complete": False}
        payload = base64.urlsafe_b64encode(canonical_json_bytes(proof)).decode().rstrip("=")
    if len(payload) > 24935:
        return None
    signature = hmac.new(key.encode(), f"v22-preflight-branch:{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def branch_options(*, preflight_id, snapshot, signals, candidates, key, ttl_seconds, now=None):
    if not snapshot or not key or not candidates:
        return []
    now = now or datetime.now(UTC)
    site = snapshot.normalized_site_url
    domain = host(site)
    source_checksum = request_digest(snapshot.html)
    evidence, notes = extract_mode_evidence(snapshot.html, url=site, domain=domain,
        checksum=source_checksum, path="/preflight/homepage", budget=ParseBudget(SiteBusinessLimits()))
    options = {}
    ambiguous = set()
    for candidate in candidates:
        if not candidate.public_gbp_url:
            continue
        keys = entity_keys(candidate.public_gbp_url)
        branch = keys.get("cid") or keys.get("place_id")
        if not branch:
            continue
        country = candidate.market.country_code if candidate.market else None
        gbp_domain = host(candidate.website_url) if candidate.website_url else None
        domain_match = gbp_domain == domain if gbp_domain else None
        phone_match = None
        if country and candidate.phone and signals.phones:
            gbp_phone = normalize_phone(candidate.phone, country).normalized_key
            phones = {normalize_phone(s.value, country).normalized_key for s in signals.phones} - {None}
            if gbp_phone and phones:
                phone_match = gbp_phone in phones
        if (domain_match is not True and phone_match is not True) or domain_match is False or phone_match is False:
            continue
        proof = dict(preflight_id=str(preflight_id), observed_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
            branch_ref=branch, public_gbp_url=candidate.public_gbp_url, domain=domain,
            entity_keys=keys, gbp_website_url=candidate.website_url, gbp_phone=candidate.phone,
            gbp_address=candidate.address, country_code=country, phone_matched=phone_match is True,
            mode_material=sorted(mode_material(evidence)), mode_evidence_complete=not notes)
        token = signed_evidence(proof, key)
        if token is None or (candidate.address and len(candidate.address) > 1000) or not 1 <= len(candidate.business_name) <= 240:
            continue
        option = PreflightBranchOption(ref=branch, public_gbp_url=candidate.public_gbp_url,
            name=candidate.business_name, address=candidate.address,
            domain_matched=domain_match is True, phone_matched=phone_match is True,
            evidence_token=token)
        if branch in options and options[branch] != option:
            ambiguous.add(branch)
        options[branch] = option
    return [value for branch, value in options.items() if branch not in ambiguous][:5]
