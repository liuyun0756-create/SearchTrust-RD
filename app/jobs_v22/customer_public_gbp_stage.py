"""Checkpointed, fail-closed collection of the customer's confirmed public GBP."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.api.v2.models import AnalyzeRequest
from app.core.config import Settings
from app.integrations.serpapi import (
    SerpApiHttpError,
    SerpApiInvalidResponse,
    SerpApiKeysUnavailable,
    SerpApiKeyState,
    SerpApiTransportError,
    configured_serpapi_keys,
    execute_serpapi_get,
)
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import CostLedgerError, JobCostLedger
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_common.google_business import data_cid, extract_data_id, extract_google_place_id
from app.report_v22.models import StrictModel
from app.report_v22.public_gbp_models import (
    CustomerPublicGbpReference,
    CustomerPublicGbpSnapshot,
    CustomerPublicGbpSnapshotInput,
    PublicGbpEntityKey,
)
from app.report_v22.public_gbp_errors import PublicGbpError
from app.report_v22.public_gbp_snapshot import build_customer_public_gbp_snapshot
from app.security_v22.urls import validate_gbp_url


STAGE_VERSION = "v22_customer_public_gbp_collection_v1"
CHECKPOINT_VERSION = "customer_public_gbp_checkpoint_v1"
MAX_PROVIDER_ATTEMPTS = 3


class CustomerPublicGbpProvider(Protocol):
    async def request(self, params: dict[str, str], *, before_attempt, after_attempt) -> dict[str, Any]: ...


class _BoundedRawClient:
    def __init__(self, client: httpx.AsyncClient, maximum: int) -> None:
        self.client = client
        self.maximum = maximum

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Accept-Encoding"] = "identity"
        async with self.client.stream("GET", url, headers=headers, **kwargs) as response:
            encoding = response.headers.get("content-encoding", "").strip().casefold()
            if encoding not in {"", "identity"}:
                raise ValueError("compressed provider responses are not accepted")
            body = bytearray()
            if response.is_stream_consumed:
                body.extend(response.content)
            else:
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > self.maximum:
                        raise ValueError("provider response exceeded its safe byte limit")
            if len(body) > self.maximum:
                raise ValueError("provider response exceeded its safe byte limit")
            return httpx.Response(response.status_code, headers=response.headers,
                content=bytes(body), request=response.request)


class SerpApiCustomerPublicGbpProvider:
    """One bounded SerpAPI lookup with the existing three-key rotation."""

    def __init__(self, *, keys: list[str], base_url: str, connect_timeout: float,
                 read_timeout: float, total_timeout: float, max_response_bytes: int,
                 client_factory: Callable[[], httpx.AsyncClient] | None = None) -> None:
        self.keys = configured_serpapi_keys(*keys)[:MAX_PROVIDER_ATTEMPTS]
        self.base_url = base_url
        self.total_timeout = total_timeout
        self.max_response_bytes = max_response_bytes
        self.state = SerpApiKeyState()
        self.timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout,
            write=read_timeout, pool=connect_timeout)
        self.client_factory = client_factory

    async def request(self, params: dict[str, str], *, before_attempt, after_attempt) -> dict[str, Any]:
        client = self.client_factory() if self.client_factory else httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=False, trust_env=False)
        should_close = self.client_factory is None
        try:
            response = await asyncio.wait_for(execute_serpapi_get(
                _BoundedRawClient(client, self.max_response_bytes), params,
                keys=self.keys, base_url=self.base_url, state=self.state,
                before_attempt=before_attempt, after_attempt=after_attempt,
            ), timeout=self.total_timeout)
            return response.payload
        finally:
            if should_close:
                await client.aclose()


class CustomerPublicGbpCollection(StrictModel):
    schema_version: Literal["customer_public_gbp_checkpoint_v1"] = CHECKPOINT_VERSION
    reference: CustomerPublicGbpReference
    snapshot: CustomerPublicGbpSnapshot


def _reference(request: AnalyzeRequest, confirmed_at: datetime) -> CustomerPublicGbpReference:
    raw_url = request.business_identity.public_gbp_url
    if raw_url is None:
        raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_REQUIRED",
            "A confirmed Google Business Profile is required for this report.")
    try:
        url = validate_gbp_url(str(raw_url))
    except (TypeError, ValueError):
        raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_IDENTITY_INVALID",
            "The confirmed Google Business Profile identity is invalid.") from None
    keys: list[PublicGbpEntityKey] = []
    place_id = extract_google_place_id(url)
    raw_data_id = extract_data_id(url)
    query = parse_qs(urlsplit(url).query)
    cid = (query.get("cid") or [None])[0] or data_cid(raw_data_id)
    if place_id:
        keys.append(PublicGbpEntityKey(kind="place_id", value=place_id))
    if raw_data_id:
        keys.append(PublicGbpEntityKey(kind="data_id", value=raw_data_id))
    if cid:
        keys.append(PublicGbpEntityKey(kind="cid", value=str(cid)))
    if not keys:
        raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_IDENTITY_INVALID",
            "The confirmed Google Business Profile needs a traceable place identity.")
    return CustomerPublicGbpReference(case_id=request.case_id,
        site_url=request.business_identity.site_url, public_gbp_url=url,
        entity_keys=keys, confirmation_source="user", confirmed_at=confirmed_at)


def _params(reference: CustomerPublicGbpReference) -> dict[str, str]:
    values = {item.kind: item.value for item in reference.entity_keys}
    params = {"engine": "google_maps", "type": "place", "hl": "en"}
    if values.get("place_id"):
        params["place_id"] = values["place_id"]
    elif values.get("data_id"):
        params["data"] = values["data_id"]
    else:
        params["data_cid"] = values["cid"]
    return params


def _text(value: Any, maximum: int) -> str | None:
    text = str(value or "").strip()
    return text if text and len(text) <= maximum else None


def _sanitize(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("place_results")
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, limit in (("title", 240), ("name", 240), ("website", 2083),
                       ("address", 500), ("phone", 120), ("place_id", 480),
                       ("data_id", 480), ("cid", 480), ("place_url", 2083),
                       ("link", 2083)):
        value = _text(raw.get(key), limit)
        if value is not None:
            clean[key] = value
    areas = raw.get("service_area") or raw.get("service_areas")
    if isinstance(areas, list):
        clean["service_areas"] = [value for item in areas[:50]
            if (value := _text(item, 240)) is not None]
    for key in ("service_area_business", "pure_service_area_business"):
        if isinstance(raw.get(key), bool):
            clean["service_area_business"] = raw[key]
            break
    return {"place_results": clean} if clean else {}


def _field(raw: dict[str, Any], key: str, *, empty: Any = None) -> dict[str, Any]:
    if key not in raw:
        return {"state": "not_returned", "value": empty}
    value = raw[key]
    if value is None or value == "" or value == []:
        return {"state": "returned_empty", "value": empty}
    return {"state": "observed", "value": value}


def _snapshot(*, reference: CustomerPublicGbpReference, payload: dict[str, Any],
              started_at: datetime, completed_at: datetime,
              request_record_id: str) -> CustomerPublicGbpSnapshot:
    raw = payload.get("place_results")
    if not isinstance(raw, dict) or not (_text(raw.get("title") or raw.get("name"), 240)):
        raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_IDENTITY_INVALID",
            "The public Google Business Profile response could not be verified.")
    observed: list[PublicGbpEntityKey] = []
    place_id = _text(raw.get("place_id"), 480)
    data_id = _text(raw.get("data_id"), 480)
    cid = _text(raw.get("cid"), 480) or data_cid(data_id)
    if place_id:
        observed.append(PublicGbpEntityKey(kind="place_id", value=place_id))
    if data_id:
        observed.append(PublicGbpEntityKey(kind="data_id", value=data_id))
    if cid:
        observed.append(PublicGbpEntityKey(kind="cid", value=cid))
    fields = {
        "business_name": _field({"business_name": raw.get("title") or raw.get("name")}, "business_name"),
        "website_url": _field(raw, "website"),
        "address": _field(raw, "address"),
        "phone": _field(raw, "phone"),
        "service_areas": _field(raw, "service_areas", empty=[]),
        "service_area_business": _field(raw, "service_area_business"),
    }
    try:
        result = build_customer_public_gbp_snapshot(CustomerPublicGbpSnapshotInput(
            reference=reference,
            request_target={"public_gbp_url": reference.public_gbp_url,
                "entity_keys": reference.entity_keys},
            started_at=started_at, completed_at=completed_at,
            expires_at=completed_at + timedelta(days=30), provider="serpapi_public",
            request_record_id=request_record_id, response_checksum=request_digest(payload),
            collection_status="succeeded", failure_code=None,
            record={"observed_entity_keys": observed,
                "observed_public_gbp_url": reference.public_gbp_url, "fields": fields},
        ))
    except (PublicGbpError, ValidationError, TypeError, ValueError):
        raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_IDENTITY_INVALID",
            "The public Google Business Profile response could not be verified.") from None
    if result.health_status != "healthy" or result.identity_match_status != "matched":
        raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_IDENTITY_INVALID",
            "The public Google Business Profile did not match the confirmed business.")
    return result


class CheckpointedCustomerPublicGbpStage:
    def __init__(self, provider: CustomerPublicGbpProvider, *, clock=None) -> None:
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(self, *, job_id: UUID, request: AnalyzeRequest, submitted_at: datetime,
                      checkpoints: JobCheckpoints,
                      cost_ledger: JobCostLedger | None = None) -> CustomerPublicGbpCollection:
        reference = _reference(request, submitted_at)
        params = _params(reference)
        key = f"{STAGE_VERSION}:result:{request_digest(reference)[7:]}"
        saved = await checkpoints.get(job_id, key)
        if saved is not None:
            try:
                checked = CustomerPublicGbpCollection.model_validate_json(canonical_json_bytes(saved))
                if (checked.reference != reference
                        or checked.snapshot.subject_reference_checksum != request_digest(reference)
                        or checked.snapshot.health_status != "healthy"
                        or checked.snapshot.identity_match_status != "matched"):
                    raise ValueError
            except (TypeError, ValueError, ValidationError):
                raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_CHECKPOINT_INVALID",
                    "The saved public GBP checkpoint could not be validated.") from None
            if cost_ledger is not None:
                await cost_ledger.record_checkpoint_hit()
            return checked

        started_at = self.clock()
        active: dict[tuple[int, str], tuple[UUID, float]] = {}

        async def before_attempt(slot: int, fingerprint: str) -> None:
            if cost_ledger is not None:
                claim = await cost_ledger.claim("serpapi_customer_public_gbp")
                active[(slot, fingerprint)] = (claim.claim_id, monotonic())

        async def after_attempt(slot: int, fingerprint: str, failure: str | None) -> None:
            item = active.pop((slot, fingerprint), None)
            if cost_ledger is not None and item is not None:
                claim_id, began = item
                await cost_ledger.complete(claim_id, outcome="success" if failure is None else "failure",
                    duration_ms=max(int((monotonic() - began) * 1000), 0))

        async def fail_active_claims() -> None:
            if cost_ledger is None:
                return
            for (slot, fingerprint), (claim_id, began) in list(active.items()):
                active.pop((slot, fingerprint), None)
                await cost_ledger.complete(claim_id, outcome="failure",
                    duration_ms=max(int((monotonic() - began) * 1000), 0))

        try:
            try:
                raw = await self.provider.request(params, before_attempt=before_attempt,
                    after_attempt=after_attempt)
            except BaseException:
                await fail_active_claims()
                raise
            payload = _sanitize(raw)
            completed_at = self.clock()
            collection = CustomerPublicGbpCollection(reference=reference,
                snapshot=_snapshot(reference=reference, payload=payload,
                    started_at=started_at, completed_at=completed_at,
                    request_record_id=f"req_{request_digest(params)[7:31]}"))
        except asyncio.CancelledError:
            raise
        except CostLedgerError:
            raise
        except DeterministicJobError:
            raise
        except SerpApiHttpError as exc:
            error = (TransientJobError if exc.status_code >= 500 else DeterministicJobError)(
                "V22_CUSTOMER_PUBLIC_GBP_UNAVAILABLE" if exc.status_code >= 500
                else "V22_CUSTOMER_PUBLIC_GBP_REJECTED",
                "The public GBP provider is temporarily unavailable."
                if exc.status_code >= 500 else "The public GBP provider rejected the safe lookup.")
            raise error from None
        except (TimeoutError, SerpApiKeysUnavailable, SerpApiTransportError,
                SerpApiInvalidResponse, httpx.TimeoutException, httpx.NetworkError,
                ValueError, TypeError):
            raise TransientJobError("V22_CUSTOMER_PUBLIC_GBP_UNAVAILABLE",
                "Public Google Business Profile data is temporarily unavailable.") from None

        saved_value = collection.model_dump(mode="json")
        await checkpoints.save(job_id, key, saved_value)
        persisted = await checkpoints.get(job_id, key)
        try:
            return CustomerPublicGbpCollection.model_validate_json(
                canonical_json_bytes(persisted or saved_value))
        except (TypeError, ValueError, ValidationError):
            raise DeterministicJobError("V22_CUSTOMER_PUBLIC_GBP_CHECKPOINT_INVALID",
                "The saved public GBP checkpoint could not be validated.") from None


def build_customer_public_gbp_stage(settings: Settings) -> CheckpointedCustomerPublicGbpStage:
    return CheckpointedCustomerPublicGbpStage(SerpApiCustomerPublicGbpProvider(
        keys=[settings.SERPAPI_KEY, settings.SERPAPI_KEY_SECONDARY, settings.SERPAPI_KEY_TERTIARY],
        base_url=settings.SERPAPI_BASE_URL,
        connect_timeout=settings.V22_COMPETITOR_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.V22_COMPETITOR_READ_TIMEOUT_SECONDS,
        total_timeout=settings.V22_COMPETITOR_TOTAL_TIMEOUT_SECONDS,
        max_response_bytes=settings.V22_COMPETITOR_MAX_RESPONSE_BYTES,
    ))
