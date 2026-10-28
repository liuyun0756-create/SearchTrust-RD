"""Bind freshly collected public sources without refreshing historical evidence."""
from datetime import timezone
from uuid import NAMESPACE_URL, uuid5

from app.api.v2.models import AnalyzeRequest
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.jobs_v22.capability_inputs import validate_generation_input
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.prospect_report_pipeline import build_persisted_public_findings_input
from app.jobs_v22.result_persistence import result_snapshot_id
from app.report_v22.capability import AnalysisInputManifest, BusinessScope, instant
from app.report_v22.capability_public import RULESET
from app.report_v22.evidence_bindings import validate_sources
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.public_gbp_models import CustomerPublicGbpReference, CustomerPublicGbpSnapshot
from app.report_v22.shared_market_binding import case_market_binding_id


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_acquisition_input(*, user_id, job_id, business, request, collected,
                            created_at, deadline_at, now):
    """Internal adapter, not an authorization gate. SQL still checks entitlement."""
    return _build_collected_input(user_id=user_id, job_id=job_id, business=business,
        request=request, collected=collected, created_at=created_at, deadline_at=deadline_at, now=now)


def _build_collected_input(*, user_id, job_id, business, request, collected,
                           created_at, deadline_at, now, private_snapshots=None):
    try:
        scope = BusinessScope.model_validate_json(canonical_json_bytes(business))
        if private_snapshots is None:
            req = AnalyzeRequest.model_validate_json(canonical_json_bytes(request))
            if req.report_type != "prospect":
                raise ValueError()
        else:
            from app.jobs_v22.first_service_collection_models import FirstServiceCollectionRequest
            req = FirstServiceCollectionRequest.model_validate_json(canonical_json_bytes(request))
        site = SiteInventorySnapshot.model_validate_json(canonical_json_bytes(collected.site_inventory))
        market = SharedMarketSnapshot.model_validate_json(canonical_json_bytes(collected.shared_market))
        rivals = CompetitorCollectionSnapshot.model_validate_json(canonical_json_bytes(collected.competitor_collection))
        gbp = CustomerPublicGbpSnapshot.model_validate_json(canonical_json_bytes(collected.customer_public_gbp.snapshot))
        reference = CustomerPublicGbpReference.model_validate_json(canonical_json_bytes(collected.customer_public_gbp.reference))
        if (any(t.tzinfo is None for t in (created_at, deadline_at, now))
                or not created_at <= now < deadline_at
                or scope.user_id != user_id or scope.case_id != req.case_id
                or scope.site_url != str(req.business_identity.site_url)
                or scope.primary_service != req.primary_service or scope.target_market != req.target_market.display_name
                or sorted(scope.competitor_sites) != sorted(str(c.website_url) for c in req.competitors)
                or rivals.job_id != job_id
                or rivals.discovery_id != collected.discovery.discovery_id
                or rivals.candidate_digest != collected.discovery.candidate_digest
                or rivals.market_snapshot_id != market.snapshot_id
                or rivals.market_snapshot_checksum != market.snapshot_checksum
                or site.started_at < created_at or rivals.started_at < created_at):
            raise ValueError()
        # Evaluation is determined by the fixed inputs, not by a retry's clock.
        evaluated = max(created_at, instant(scope.confirmed_at), site.completed_at,
                        rivals.completed_at, market.snapshot.completed_at, gbp.completed_at)
        if evaluated > now or market.expires_at <= now or gbp.expires_at <= now:
            raise ValueError()
        value = build_persisted_public_findings_input(
            request=req, site_inventory=site,
            site_snapshot_id=result_snapshot_id("site", req.case_id, request_digest(site)),
            shared_market=market, competitor_collection=rivals,
            competitor_snapshot_id=result_snapshot_id("competitor", req.case_id, request_digest(rivals)),
            public_gbp_snapshot=gbp,
            public_gbp_snapshot_id=result_snapshot_id("public_gbp", req.case_id, request_digest(gbp)),
            public_gbp_reference=reference, evaluated_at=evaluated)
        for source in value.evidence_input.sources:
            if source.kind == "serp":
                # Keep shared_snapshot and competitor.market_snapshot_id intact.
                # Only the Case's storage reference receives a local identity.
                source.binding.snapshot_id = case_market_binding_id(req.case_id, market.snapshot_id)
            if source.kind in {"site", "competitor"}:
                # These snapshots have no source expiry. They are accepted only
                # from this run, and only until its ORIGINAL processing deadline.
                source.binding.expires_at = deadline_at
        validate_sources(value.evidence_input)
        refs = [dict(snapshot_id=str(s.binding.snapshot_id), case_id=str(req.case_id),
            source_kind=s.kind, schema_version=s.binding.schema_version,
            payload_checksum=s.binding.payload_checksum, fetched_at=stamp(s.binding.fetched_at),
            expires_at=stamp(s.binding.expires_at), health="healthy", identity_match="matched", binding_id=None,
            scope=f"{s.kind} · {scope.primary_service} · {scope.target_market}") for s in value.evidence_input.sources]
        mode, ruleset = "acquisition", RULESET
        if private_snapshots is not None:
            from app.report_v22.capability_service import RULESET as SERVICE_RULESET
            from app.report_v22.capability_service_inputs import CapabilityServiceInput, ServiceFirstPartyInput
            first = ServiceFirstPartyInput(case_id=req.case_id, parent_report_id=None,
                evaluated_at=evaluated, snapshots=private_snapshots)
            if {s.source_type for s in first.snapshots} != {"gsc", "ga4"}:
                raise ValueError()
            for s in first.snapshots:
                if s.health_status != "healthy" or s.identity_match_status != "matched" or s.expires_at <= now:
                    raise ValueError()
                refs.append(dict(snapshot_id=str(s.snapshot_id), case_id=str(req.case_id), source_kind=s.source_type,
                    schema_version=s.schema_version, payload_checksum=s.payload_checksum, fetched_at=stamp(s.fetched_at),
                    expires_at=stamp(s.expires_at), health=s.health_status, identity_match=s.identity_match_status,
                    binding_id=str(s.binding_id), scope=f"{s.external_resource_id} · {s.coverage_start} to {s.coverage_end}"))
            value = CapabilityServiceInput(public_input=value, first_party_input=first)
            mode, ruleset = "first_service", SERVICE_RULESET
        manifest = dict(schema_version="v22_analysis_input_v1",
            id=str(uuid5(NAMESPACE_URL, f"searchtrust:capability:{mode}:{job_id}")),
            job_id=str(job_id), user_id=str(user_id), case_id=str(req.case_id), mode=mode,
            business=scope.model_dump(mode="json"), snapshots=refs, evaluated_at=stamp(evaluated),
            ruleset_version=ruleset, generation_input_checksum=verified_request_digest(value.model_dump(mode="json")),
            previous_report=None, implementation=None)
        manifest["input_checksum"] = verified_request_digest(manifest)
        return validate_generation_input(manifest, value)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, EvidenceError):
        raise DeterministicJobError("V22_ACQUISITION_INPUT_INVALID" if private_snapshots is None else "V22_FIRST_SERVICE_INPUT_INVALID",
            "The collected sources do not match this confirmed analysis or are no longer eligible.") from None
