"""v13-only GBP discovery: saved evidence first, positive D/P gate, three attempts.

The authenticated identity review entry uses this lookup. Legacy v12 preflight
is unchanged. The caller persists Case-scoped attempts before provider requests.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from app.core.config import Settings
from app.integrations.serpapi import serpapi_payload_error
from app.jobs_v22.customer_public_gbp_stage import (
    CustomerPublicGbpProvider, SerpApiCustomerPublicGbpProvider,
)
from app.preflight_v22.gbp_identity_models import (
    CandidateDecision, GbpIdentityRequest, GbpIdentityResult, GbpLookupLedger,
    IdentityObservation, LookupAttempt, SavedGbpRecord,
)
from app.report_common.google_business import data_cid, extract_data_id, extract_google_place_id
from app.report_v22.site_gbp_comparators import compare_phones, normalize_phone
from app.security_v22.urls import validate_gbp_url


def canonical_host(value: str | None) -> str | None:
    try:
        parsed = urlsplit(value or "")
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password:
            return None
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        return host.removeprefix("www.") or None
    except (ValueError, UnicodeError):
        return None


def _owned_observations(request, observations):
    domain = canonical_host(str(request.site_url))
    return [o for o in observations if o.belongs_to_business and o.purpose == "business"
            and canonical_host(str(o.page_url)) == domain]


def observations_from_site_selection(facts, selection):
    """Bridge the existing page/entity selector, never the old raw HTML regex.

    Text-pattern facts needing an already matched GBP are not eligible inputs:
    reusing them here would make the phone identity proof circular.
    """
    if facts.source_status.state != "ready":
        return {"phones": [], "names": []}
    qualified = {c.candidate_id for c in selection.eligibility if c.state == "eligible"}
    result = {"phones": [], "names": []}
    for candidate in facts.candidates:
        if (candidate.candidate_id not in qualified or candidate.source_kind == "text_pattern"
                or candidate.field not in {"phone", "business_name"}):
            continue
        observation = IdentityObservation(value=candidate.scalar_value, page_url=candidate.final_url,
            source_ref=f"{facts.source_payload_checksum}/{candidate.candidate_id}", belongs_to_business=True)
        result["phones" if candidate.field == "phone" else "names"].append(observation)
    return result


def _branch(record):
    return f"cid:{record.cid}" if record.cid else f"place_id:{record.place_id}" if record.place_id else None


def _address_only(record):
    name = re.sub(r"\W+", " ", record.business_name or "").casefold().strip()
    address = re.sub(r"\W+", " ", record.address or "").casefold().strip()
    # Saved map places may use a street address as their title. A CID is not
    # evidence of a business. Numeric brand names with distinct titles survive.
    return (not name or bool(address and (name == address or address.startswith(name + " ")))
            or (bool(record.categories) and all(c.casefold() in {
                "building", "street address", "premise", "route", "postal_code", "street_address",
            } for c in record.categories)))


def assess_candidate(request: GbpIdentityRequest, record: SavedGbpRecord) -> CandidateDecision:
    expected = canonical_host(str(request.site_url))
    observed = canonical_host(record.website_url)
    aliases = {canonical_host(str(a.url)): a.source_ref for a in request.aliases}
    domain = observed in {expected, *aliases} if observed else None
    phones = _owned_observations(request, request.phones)
    comparisons = [(o, compare_phones(o.value, record.phone, request.country_code).state)
                   for o in phones] if record.phone and request.country_code else []
    matches = [o for o, state in comparisons if state in {"exact_match", "semantic_match"}]
    phone = True if matches else False if any(s == "mismatch" for _, s in comparisons) else None
    current = bool(record.available and record.observed_at <= request.evaluated_at
                   and (record.expires_at is None or request.evaluated_at < record.expires_at))
    reasons = []
    if _address_only(record):
        reasons.append("address_only_place")
    if not _branch(record):
        reasons.append("business_entity_key_missing")
    identity = not reasons
    if domain is None:
        reasons.append("website_unavailable_or_invalid")
    if phone is None:
        reasons.append("phone_unavailable_or_incomparable")
    if domain is False:
        reasons.append("domain_conflict")
    if phone is False:
        reasons.append("phone_conflict")
    if not current:
        reasons.append("saved_evidence_unavailable_or_expired")
    refs = [o.source_ref for o in matches]
    if domain and observed in aliases:
        refs.append(aliases[observed])
    return CandidateDecision(record=record, domain_match=domain, phone_match=phone,
        eligible=identity and (domain is True or phone is True), current=current,
        reasons=reasons, matched_source_refs=refs)


def _link_key(url):
    if not url:
        return None
    try:
        safe = validate_gbp_url(url)
        cid = (parse_qs(urlsplit(safe).query).get("cid") or [None])[0] or data_cid(extract_data_id(safe))
        place = extract_google_place_id(safe)
        return ("cid", cid) if cid and cid.isdigit() else ("place_id", place) if place else None
    except (ValueError, TypeError):
        return None


def record_from_saved_mapping(row, *, source_ref, record_id, payload_checksum,
                              observed_at, expires_at=None, kind="lookup", available=True):
    """Adapt saved normalized candidates/market rows without guessing identity.

    Caller passes the original source lifetime/availability, not a new TTL.
    This also supports a whitelist of raw provider fields; no raw payload leaks.
    """
    def string(*keys):
        return next((row[k].strip() for k in keys if isinstance(row.get(k), str) and row[k].strip()), None)
    key = _link_key(string("public_gbp_url"))
    cid = string("provider_cid", "cid", "data_cid") or data_cid(string("provider_data_id", "data_id"))
    place = string("provider_place_id", "place_id")
    if key:
        if key[0] == "cid":
            cid = cid or key[1]
        else:
            place = place or key[1]
    categories = row.get("categories") or row.get("types") or row.get("type") or []
    if isinstance(categories, str):
        categories = [categories]
    return SavedGbpRecord(source_ref=source_ref, record_id=record_id, payload_checksum=payload_checksum,
        observed_at=observed_at, expires_at=expires_at, available=available, kind=kind,
        business_name=string("business_name", "display_name", "title", "name"),
        website_url=string("website_url", "website", "url"), phone=string("phone"),
        address=string("address"), categories=[c for c in categories if isinstance(c, str)]
            if isinstance(categories, list) else [], cid=cid, place_id=place)


class _BudgetExhausted(Exception):
    pass


class IdentityGbpLookup:
    def __init__(self, *, provider: CustomerPublicGbpProvider | None = None,
                 incremental_enabled: bool = False, clock=None, timeout_seconds: float = 35,
                 persist_ledger=None):
        self.provider = provider
        self.incremental_enabled = incremental_enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout_seconds = timeout_seconds
        self.persist_ledger = persist_ledger
        self._ledgers = {}
        self._locks = {}

    async def lookup(self, request: GbpIdentityRequest, *, ledger: GbpLookupLedger | None = None) -> GbpIdentityResult:
        key = (request.case_ref, canonical_host(str(request.site_url)))
        async with self._locks.setdefault(key, asyncio.Lock()):
            previous = self._ledgers.get(key)
            if previous:
                if ledger is not None and ledger.attempts[:len(previous.attempts)] != previous.attempts:
                    raise ValueError("lookup ledger cannot discard recorded attempts")
                ledger = ledger or previous
            return await self._lookup(request, ledger=ledger)

    async def _lookup(self, request, *, ledger):
        # Revalidate mutable inputs and return a copy of the ledger to the owner.
        request = GbpIdentityRequest.model_validate(request.model_dump())
        domain = canonical_host(str(request.site_url))
        if not domain:
            raise ValueError("valid business website required")
        ledger = (GbpLookupLedger.model_validate(ledger.model_dump()) if ledger else
                  GbpLookupLedger(case_ref=request.case_ref, normalized_domain=domain))
        if (ledger.case_ref, ledger.normalized_domain) != (request.case_ref, domain):
            raise ValueError("lookup ledger belongs to another Case/business")
        self._ledgers[(request.case_ref, domain)] = ledger
        stages = ["cache"]
        decisions = []
        seen = set()

        def add(records):
            for record in records:
                key = (record.source_ref, record.record_id, record.payload_checksum)
                if key not in seen:
                    seen.add(key)
                    decisions.append(assess_candidate(request, record))

        def result(status, reason, selected=None, stop_reason=None):
            return GbpIdentityResult(status=status, reason=reason, selected=selected,
                lookup_stop_reason=stop_reason, candidates=decisions,
                ledger=ledger.model_copy(deep=True), visited_stages=stages)

        def resolved():
            eligible = [c for c in decisions if c.eligible and c.current]
            groups = {}
            # The same record can expose both IDs. Join only on an explicit ID
            # relation, never on brand/phone/domain similarity across branches.
            place_cids = {}
            for c in eligible:
                if c.record.cid and c.record.place_id:
                    place_cids.setdefault(c.record.place_id, set()).add(c.record.cid)
            for c in eligible:
                branch = _branch(c.record)
                cids = place_cids.get(c.record.place_id, set())
                if not c.record.cid and len(cids) == 1:
                    branch = f"cid:{next(iter(cids))}"
                groups.setdefault(branch, []).append(c)
            # Keep the newest observation for one identity; no CID merging by name.
            chosen = request.selected_branch_ref
            if chosen and chosen.startswith("place_id:"):
                cids = place_cids.get(chosen.removeprefix("place_id:"), set())
                if len(cids) == 1:
                    chosen = f"cid:{next(iter(cids))}"
            if chosen and chosen in groups:
                record = max(groups[chosen], key=lambda c: c.record.observed_at).record
                return result("matched", "explicit_branch_domain_or_phone_match", record)
            if chosen and groups:
                return result("identity_unconfirmed", "selected_branch_not_verified")
            if len(groups) > 1:
                return result("identity_unconfirmed", "multiple_verified_branches")
            if len(groups) == 1:
                record = max(next(iter(groups.values())), key=lambda c: c.record.observed_at).record
                return result("matched", "verified_domain_or_phone_match", record)
            return None

        add(request.saved_records)
        cached = resolved()
        if cached:
            return cached
        stages.append("saved_link")
        link = request.saved_link_destinations.get(request.gbp_url, request.gbp_url)
        link_key = _link_key(link)
        # Address-only records stay explicitly unconfirmed even when paid search
        # is off. Other stop conditions remain not_checked, never not_found.
        def stopped(reason):
            address_only = any("address_only_place" in c.reasons for c in decisions)
            if address_only and reason in {"incremental_lookup_disabled", "awaiting_market_cache"}:
                return result("identity_unconfirmed", "address_only_place", stop_reason=reason)
            return result("lookup_not_checked", reason, stop_reason=reason)

        if not request.market_collected:
            return stopped("awaiting_market_cache")
        if not self.incremental_enabled:
            return stopped("incremental_lookup_disabled")
        if self.provider is None:
            return stopped("provider_unavailable")

        queue = []
        if link_key:
            queue.append(("link", {"type": "place", "data_cid" if link_key[0] == "cid" else "place_id": link_key[1]}))
        for c in decisions:
            if c.eligible and not c.current:
                r = c.record
                params = {"type": "place", "data_cid": r.cid} if r.cid else {"type": "place", "place_id": r.place_id}
                if not any(params == p for _, p in queue):
                    queue.append(("refresh", params))
        phones = _owned_observations(request, request.phones)
        numbers = [normalize_phone(o.value, request.country_code).normalized_key for o in phones] if request.country_code else []
        market = request.target_market.display_name
        country = request.country_code or request.target_market.country_code
        parts = [domain, next((n for n in numbers if n), ""), market, country]
        queue.append(("domain_phone", {"type": "search", "q": " ".join(p for p in parts if p)}))
        names = _owned_observations(request, request.names)
        name = request.seo_business_name or (names[0].value if names else None)
        if name:
            queue.append(("name", {"type": "search", "q": f"{name} {market} {country}"}))

        for stage, params in queue:
            if len(ledger.attempts) >= 3:
                return stopped("extra_request_budget_exhausted")
            stages.append(stage)
            attempt_start = len(ledger.attempts)

            async def before_attempt(slot, fingerprint):
                # Runs before EVERY HTTP request, including key failover retries.
                if len(ledger.attempts) >= 3:
                    raise _BudgetExhausted()
                ledger.attempts.append(LookupAttempt(number=len(ledger.attempts) + 1, stage=stage))
                if self.persist_ledger:
                    await self.persist_ledger(ledger.model_copy(deep=True), reserve=True)

            async def after_attempt(slot, fingerprint, failure_kind):
                ledger.attempts[-1].outcome = "failed" if failure_kind else "success"
                if self.persist_ledger:
                    await self.persist_ledger(ledger.model_copy(deep=True), reserve=False)

            try:
                response = await asyncio.wait_for(self.provider.request(
                    {"engine": "google_maps", "hl": "en", **params},
                    before_attempt=before_attempt, after_attempt=after_attempt), self.timeout_seconds)
                if len(ledger.attempts) == attempt_start:
                    return stopped("provider_attempt_provenance_missing")
                payload = response.payload
                if not isinstance(payload, dict) or serpapi_payload_error(payload):
                    return stopped("provider_invalid_response")
                if not any(k in payload for k in ("place_results", "local_results")):
                    return stopped("provider_invalid_response")
                rows = []
                for key in ("place_results", "local_results"):
                    value = payload.get(key, [])
                    if isinstance(value, dict):
                        rows.append(value)
                    elif isinstance(value, list) and all(isinstance(row, dict) for row in value):
                        rows.extend(value)
                    else:
                        return stopped("provider_invalid_response")
                now = self.clock()
                # A real request creates new observations. Frozen cache timestamps
                # remain untouched; injected clocks support deterministic replay.
                request.evaluated_at = now
                records = [record_from_saved_mapping(row,
                    source_ref=f"provider:{response.response_checksum}", record_id=f"record:{i}",
                    payload_checksum=response.response_checksum, observed_at=now,
                    expires_at=now + timedelta(days=30), kind="lookup") for i, row in enumerate(rows)]
                add(records)
            except _BudgetExhausted:
                return stopped("extra_request_budget_exhausted")
            except Exception:
                # Never expose provider exceptions (which can contain secrets).
                for attempt in ledger.attempts[attempt_start:]:
                    if attempt.outcome == "started":
                        attempt.outcome = "interrupted"
                return stopped("provider_unavailable_or_timed_out")
            found = resolved()
            if found:
                return found
        if any("address_only_place" in c.reasons for c in decisions) and all(
                "address_only_place" in c.reasons for c in decisions):
            return result("identity_unconfirmed", "address_only_place")
        return result("gbp_not_found", "no_verified_domain_or_phone_match")


def build_identity_gbp_lookup(settings: Settings) -> IdentityGbpLookup:
    """Explicit v2 factory; no network during construction or with switch off."""
    provider = None
    if settings.V22_IDENTITY_GBP_INCREMENTAL_ENABLED:
        provider = SerpApiCustomerPublicGbpProvider(keys=[settings.SERPAPI_KEY,
            settings.SERPAPI_KEY_SECONDARY, settings.SERPAPI_KEY_TERTIARY],
            base_url=settings.SERPAPI_BASE_URL, connect_timeout=5, read_timeout=10,
            total_timeout=30, max_response_bytes=2_000_000)
    return IdentityGbpLookup(provider=provider,
        incremental_enabled=settings.V22_IDENTITY_GBP_INCREMENTAL_ENABLED)


def exclude_customer_from_competitors(records: list[SavedGbpRecord], request: GbpIdentityRequest,
                                     result: GbpIdentityResult) -> list[SavedGbpRecord]:
    """Filter an already selected/ordered pool; do not invent replacement rivals."""
    domains = {canonical_host(str(request.site_url)), *(canonical_host(str(a.url)) for a in request.aliases)}
    selected = result.selected
    kept = []
    for row in records:
        same_key = selected and ((selected.cid and row.cid == selected.cid)
                                or (selected.place_id and row.place_id == selected.place_id))
        if same_key or canonical_host(row.website_url) in domains:
            continue
        kept.append(row)
    return kept[:3]
