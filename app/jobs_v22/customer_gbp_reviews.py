"""Collect a bounded Google Maps review prefix for an A3-retained profile."""
import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from time import monotonic

import httpx

from app.integrations.serpapi import (
    SerpApiHttpError, SerpApiInvalidResponse, SerpApiKeysUnavailable, SerpApiTransportError,
)
from app.jobs_v22.cost_ledger import CostLimitExceeded
from app.jobs_v22.customer_public_gbp_stage import SerpApiCustomerPublicGbpProvider
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_common.google_business import data_cid
from app.report_v22.customer_review_sample import CustomerReview, CustomerReviewPage, CustomerReviewSample, PAGE_LIMIT, SAMPLE_LIMIT

DATA_ID = re.compile(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+")
OPERATION = "serpapi_customer_gbp_reviews"
LOOKUP_OPERATION = "serpapi_customer_gbp_review_lookup"


class _DetailAttemptExhausted(Exception):
    pass


class _WrongBranch(ValueError):
    pass


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" UTC", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def branch_keys(snapshot, branch_ref):
    requested = {key.kind: key.value for key in snapshot.request_target.entity_keys}
    observed = {key.kind: key.value for key in snapshot.record.observed_entity_keys}
    shared = requested.keys() & observed.keys()
    if (snapshot.identity_match_status != "matched" or snapshot.collection_status != "succeeded"
            or not branch_ref or not any(requested.get(kind) == observed.get(kind) == branch_ref
                       for kind in ("cid", "place_id"))
            or any(requested[kind] != observed[kind] for kind in shared)):
        return None
    return observed


def _valid_data_id(value, keys):
    return (isinstance(value, str) and len(value) <= 200 and DATA_ID.fullmatch(value)
            and ("cid" not in keys or data_cid(value) == keys["cid"]))


def _normalize_review(raw):
    if not isinstance(raw, dict) or type(raw.get("rating")) not in (int, float) or not 0 <= raw["rating"] <= 5:
        return None
    review_id = raw.get("review_id")
    has_id = isinstance(review_id, str) and bool(review_id.strip()) and len(review_id) <= 500
    date = _timestamp(raw.get("iso_date"))
    if has_id:
        digest = sha256(review_id.encode("utf-8")).hexdigest()
    else:
        body = raw.get("snippet") or raw.get("text")
        digest = request_digest([raw["rating"], date.isoformat() if date else None,
                                 body[:10_000] if isinstance(body, str) else None])[7:]
    response = raw.get("response")
    reply_state = "unknown"
    if response is None:
        reply_state = "not_visible"
    elif isinstance(response, dict):
        text = response.get("snippet") or response.get("text")
        if isinstance(text, str) and text.strip():
            reply_state = "visible"
    return CustomerReview(id=digest, provider_id=has_id, iso_date=date, reply_state=reply_state)


class CustomerGbpReviewsStage:
    def __init__(self, provider, *, clock=None):
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _request(self, params, ledger, *, deadline_at, operation=OPERATION):
        active = {}

        async def before(slot, fingerprint):
            claim = await ledger.claim(operation)
            active[slot, fingerprint] = (claim.claim_id, monotonic())

        async def after(slot, fingerprint, failure):
            entry = active.pop((slot, fingerprint), None)
            if entry is not None:
                claim, started = entry
                await ledger.complete(claim, outcome="success" if failure is None else "failure",
                                      duration_ms=max(0, int((monotonic() - started) * 1000)))

        try:
            remaining = (deadline_at - self.clock()).total_seconds() - 180
            if remaining <= 0:
                raise _DetailAttemptExhausted()
            async with asyncio.timeout(remaining):
                return await self.provider.request(params, before_attempt=before, after_attempt=after)
        finally:
            for slot, fingerprint in list(active):
                await after(slot, fingerprint, "unresolved")

    async def collect(self, *, job_id, snapshot, branch_ref, checkpoints, cost_ledger, deadline_at):
        keys = branch_keys(snapshot, branch_ref)
        if keys is None:
            return None
        provenance = request_digest(dict(branch=branch_ref, profile_checksum=request_digest(snapshot)))
        key = f"v22_customer_gbp_reviews_v1:{provenance[7:]}"
        saved = await checkpoints.get(job_id, key)
        if saved is not None:
            await cost_ledger.record_checkpoint_hit()
        else:
            async def operation():
                value = await self._sample(keys, branch_ref, cost_ledger, deadline_at)
                payload = value.model_dump(mode="json")
                return {"provenance": provenance, "checksum": request_digest(payload), "sample": payload}
            saved = await checkpoints.run_once(job_id, key, operation)
        try:
            sample = CustomerReviewSample.model_validate_json(canonical_json_bytes(saved["sample"]))
            if (saved["provenance"] != provenance or sample.branch_ref != branch_ref
                    or saved["checksum"] != request_digest(sample)
                    or (sample.data_id is not None and (not _valid_data_id(sample.data_id, keys)
                        or keys.get("data_id") not in (None, sample.data_id)))):
                raise ValueError("invalid saved GBP review sample")
            return sample
        except (KeyError, TypeError, ValueError):
            return None

    async def _sample(self, keys, branch_ref, ledger, deadline_at):
        pages = []
        reviews = {}
        invalid = 0
        stop = "branch_unavailable"
        data_id = keys.get("data_id")
        if not _valid_data_id(data_id, keys):
            data_id = None
        try:
            if data_id is None and "data_id" not in keys:
                selector = {"place_id": branch_ref} if keys.get("place_id") == branch_ref else {"data_cid": branch_ref}
                result = await self._request({"engine": "google_maps", "type": "place", "hl": "en",
                                              "no_cache": "true", **selector}, ledger, deadline_at=deadline_at, operation=LOOKUP_OPERATION)
                place = result.payload.get("place_results")
                if isinstance(place, dict):
                    found = place.get("data_id")
                    returned = {"place_id": place.get("place_id"), "cid": place.get("data_cid") or place.get("cid") or data_cid(found)}
                    if (_valid_data_id(found, keys) and branch_ref in returned.values()
                            and all(not returned[k] or returned[k] == keys[k] for k in returned if k in keys)):
                        data_id = found
            token = None
            seen_tokens = set()
            while data_id and len(pages) < PAGE_LIMIT:
                params = {"engine": "google_maps_reviews", "data_id": data_id, "sort_by": "newestFirst",
                          "hl": "en", "no_cache": "true"}
                if token:
                    params["next_page_token"] = token
                started = self.clock()
                result = await self._request(params, ledger, deadline_at=deadline_at)
                raw, page, next_token = self._page(result, data_id, started)
                pages.append(page)
                for record in raw:
                    record = _normalize_review(record)
                    if record is None:
                        invalid += 1
                    else:
                        reviews.setdefault(record.id, record)
                if next_token is not None and (not isinstance(next_token, str) or not 1 <= len(next_token) <= 500
                                               or next_token in seen_tokens):
                    stop = "invalid_pagination"
                    break
                if next_token is None:
                    stop = "natural_end"
                    break
                if len(reviews) >= SAMPLE_LIMIT:
                    stop = "sample_cap"
                    break
                stop = "page_cap"
                token = next_token
                seen_tokens.add(token)
        except _WrongBranch:
            pages.clear()
            reviews.clear()
            invalid = 0
            stop = "branch_unavailable"
        except (CostLimitExceeded, _DetailAttemptExhausted):
            stop = "budget"
        except (SerpApiHttpError, SerpApiInvalidResponse, SerpApiKeysUnavailable, SerpApiTransportError,
                httpx.HTTPError, TimeoutError, ValueError, TypeError):
            stop = "provider_failure"

        t = pages[0].sampled_at if pages else None
        samples = list(reviews.values())
        discarded = bool(t and any(r.iso_date is None or r.iso_date >= t - timedelta(days=90)
                                   for r in samples[SAMPLE_LIMIT:]))
        return CustomerReviewSample(branch_ref=branch_ref, data_id=data_id, pages=pages,
            reviews=samples[:SAMPLE_LIMIT], sampled_at=t, completed_at=self.clock(), stop=stop,
            invalid_records=invalid, discarded_recent_or_invalid=discarded)

    def _page(self, result, data_id, started):
        payload = result.payload
        search = payload.get("search_parameters")
        place = payload.get("place_info")
        metadata = payload.get("search_metadata")
        raw = payload.get("reviews")
        if (not isinstance(search, dict) or search.get("data_id") != data_id
                or (isinstance(place, dict) and place.get("data_id") not in (None, data_id))):
            raise _WrongBranch()
        if (not isinstance(metadata, dict) or metadata.get("status") != "Success" or payload.get("error")
                or not isinstance(raw, list) or len(raw) > 100):
            raise ValueError("unverified customer GBP reviews response")
        t = _timestamp(metadata.get("created_at")) or started
        if t > self.clock():
            raise ValueError("future customer GBP review sample")
        pagination = payload.get("serpapi_pagination")
        token = pagination.get("next_page_token") or None if isinstance(pagination, dict) else None
        if pagination is not None and not isinstance(pagination, dict):
            token = False
        page = CustomerReviewPage(sampled_at=t, response_checksum=result.response_checksum,
            sort_verified=search.get("sort_by") == "newestFirst", has_next=token is not None, returned=len(raw))
        return raw, page, token


def build_customer_gbp_reviews_stage(settings):
    return CustomerGbpReviewsStage(SerpApiCustomerPublicGbpProvider(
        keys=[settings.SERPAPI_KEY, settings.SERPAPI_KEY_SECONDARY, settings.SERPAPI_KEY_TERTIARY],
        base_url=settings.SERPAPI_BASE_URL,
        connect_timeout=settings.V22_COMPETITOR_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.V22_COMPETITOR_READ_TIMEOUT_SECONDS,
        total_timeout=settings.V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS,
        max_response_bytes=settings.V22_COMPETITOR_MAX_RESPONSE_BYTES,
    ))
