"""Fail-closed snapshot and context validation, without store or provider IO."""

from datetime import date, datetime
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_models import EvidenceBuildInput, EvidenceSource, reject_nonfinite


def host(url) -> str:
    return (urlsplit(str(url)).hostname or "").casefold().removeprefix("www.")


def require(condition: bool) -> None:
    if not condition:
        raise EvidenceError("SNAPSHOT_BINDING_INVALID")


def eligibility(source: EvidenceSource, evaluated_at: datetime):
    binding = source.binding
    if binding.expires_at is not None and binding.expires_at <= evaluated_at:
        return "expired"
    if binding.health_status == "expired":
        return "expired"
    if source.kind == "first_party":
        if binding.identity_match_status == "mismatch":
            return "identity_mismatch"
        if binding.identity_match_status != "matched":
            return "identity_unconfirmed"
        if binding.health_status == "unavailable":
            return "unavailable"
        if binding.health_status != "healthy":
            return "unhealthy"
    elif binding.health_status not in {"healthy", "not_checked"}:
        return "unavailable" if binding.health_status == "unavailable" else "unhealthy"
    return None


def _validate_times(value, latest):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"fetched_at", "collected_at", "observed_at", "completed_at", "started_at", "resolved_at", "created_at"} and isinstance(item, datetime):
                require(item <= latest)
            _validate_times(item, latest)
    elif isinstance(value, list):
        for item in value:
            _validate_times(item, latest)


def validate_sources(value: EvidenceBuildInput) -> list[EvidenceSource]:
    """Validate *all* sources before any adapter runs; deduplicate exact inputs."""
    try:
        reject_nonfinite(value)
        value = EvidenceBuildInput.model_validate(value.model_dump(mode="python"))
        context = value.context
        registry = {}
        sources = {}
        for source in value.sources:
            b, p = source.binding, source.payload
            require(b.case_id == context.case_id and b.fetched_at <= context.evaluated_at)
            kind = p.source_type if source.kind == "first_party" else source.kind
            require(b.source_type == kind and b.schema_version == p.schema_version)
            _validate_times(p.model_dump(mode="python"), b.fetched_at)
            if source.kind == "first_party":
                require(context.report_type == "verified_execution")
                for field in ("snapshot_id", "source_type", "schema_version", "fetched_at", "expires_at", "health_status", "identity_match_status", "payload_checksum"):
                    require(getattr(b, field) == getattr(p, field))
                dates = [date.fromisoformat(d) if d else None for d in (p.provider_request_context.start_date, p.provider_request_context.end_date)]
                require(not all(dates) or dates[0] <= dates[1])
                for row in p.normalized_payload.rows:
                    require(len({d.key for d in row.dimensions}) == len(row.dimensions))
                checksum = request_digest(p.normalized_payload)
            else:
                require(b.fetched_at == p.completed_at)
                checksum = request_digest(p)
            if checksum != b.payload_checksum:
                raise EvidenceError("CHECKSUM_MISMATCH")
            if source.kind == "site":
                require(host(context.site_url) == p.canonical_host == host(p.root_url))
                require(str(context.site_url) == str(p.root_url))
            elif source.kind == "serp":
                require(p.queries == context.queries)
                require(p.device == context.search_device and p.language == context.search_language)
                require(p.country_code == context.target_market.country_code)
                require(p.target_point.requested_label == context.target_market.display_name)
                for field in ("latitude", "longitude"):
                    coordinate = getattr(context.target_market, field)
                    require(coordinate is None or coordinate == getattr(p.target_point, field))
                for run in p.query_runs:
                    for result in run.results:
                        call = run.maps_call if result.call_id == run.maps_call.call_id else run.google_call
                        require(call.status == "succeeded")
                        require(result.response_checksum == call.response_checksum)
                shared = source.shared_snapshot
                if shared is not None:
                    require(shared.snapshot_id == b.snapshot_id and shared.expires_at == b.expires_at)
                    require(shared.created_at <= context.evaluated_at)
                    require(shared.snapshot_checksum == checksum)
                    require(canonical_json_bytes(shared.snapshot) == canonical_json_bytes(p))
            elif source.kind == "competitor":
                expected = {c.competitor_id: c for c in context.competitors}
                require({c.competitor.competitor_id for c in p.competitors} == set(expected))
                for item in p.competitors:
                    require(item.competitor == expected[item.competitor.competitor_id])
                    if item.public_gbp and item.public_gbp.website_url:
                        require(host(item.public_gbp.website_url) == host(item.competitor.website_url))
            encoded = canonical_json_bytes(source)
            if b.snapshot_id in registry:
                require(registry[b.snapshot_id] == encoded)
            registry[b.snapshot_id] = encoded
            sources[b.snapshot_id] = source
        for source in sources.values():
            if source.kind == "competitor":
                market = sources.get(source.payload.market_snapshot_id)
                require(market is not None and market.kind == "serp")
                require(market.binding.payload_checksum == source.payload.market_snapshot_checksum)
        for missing in value.missing_sources:
            require(missing.competitor_id is None or (missing.source_type == "competitor" and missing.competitor_id in {c.competitor_id for c in context.competitors}))
            require(not any(s.binding.source_type == missing.source_type for s in sources.values()))
        return [sources[key] for key in sorted(sources, key=str)]
    except (ValueError, TypeError, ValidationError):
        raise EvidenceError("SOURCE_INVALID") from None
