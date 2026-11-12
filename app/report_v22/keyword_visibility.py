"""V14 saved first-page observations. No provider, clock or persistence calls."""
from copy import deepcopy
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.models import StrictModel

KEYWORD_RULESET = "v22_capability_public_v14"


class KeywordPosition(StrictModel):
    status: Literal["observed", "not_detected", "not_checked"]
    position: int | None = Field(default=None, ge=1, le=10000)
    rank_source: Literal["provider_position", "response_order"] | None = None
    reason: Literal["observed", "first_page_absent", "source_unavailable", "request_failed", "section_unavailable", "parse_incomplete", "identity_unresolved", "rank_basis_mismatch", "sample_not_local"]
    checked_count: int = Field(ge=0, le=20)
    record_ids: list[str] = Field(max_length=20)
    evidence_paths: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def coherent(self):
        if (self.status == "observed") != (self.position is not None and self.rank_source is not None):
            raise ValueError("position must be an observed saved value")
        if self.status != "observed" and (self.position is not None or self.rank_source is not None or self.record_ids):
            raise ValueError("unobserved position must remain unknown")
        if (self.status == "observed") != (self.reason == "observed"):
            raise ValueError("observation reason mismatch")
        if self.status == "observed" and not self.record_ids:
            raise ValueError("observation needs saved records")
        if (self.status == "not_detected") != (self.reason == "first_page_absent"):
            raise ValueError("absence requires an eligible first-page check")
        if any(not p.startswith("/payload/") for p in self.evidence_paths) or len(set(self.evidence_paths)) != len(self.evidence_paths):
            raise ValueError("invalid keyword evidence paths")
        return self


class KeywordRow(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    local_pack: KeywordPosition
    organic: KeywordPosition
    maps: KeywordPosition


class KeywordAssessment(StrictModel):
    kind: Literal["inference"] = "inference"
    assessment: Literal["strong", "medium", "weak", "not_assessed"]
    checked_keywords: int = Field(ge=0, le=5)
    observed_keywords: int = Field(ge=0, le=5)
    total_keywords: int = Field(ge=3, le=5)
    ratio: float | None = Field(default=None, ge=0, le=1)


def assessment(rows, channel):
    checked = [getattr(r, channel) for r in rows if getattr(r, channel).status != "not_checked"]
    k, h, n = len(checked), sum(r.status == "observed" for r in checked), len(rows)
    # Integer comparisons avoid floating boundary drift at exactly 1/3 and 2/3.
    grade = "not_assessed" if k < 2 else "strong" if 3*h >= 2*k else "medium" if 3*h >= k else "weak"
    return KeywordAssessment(assessment=grade, checked_keywords=k, observed_keywords=h,
        total_keywords=n, ratio=h/k if k >= 2 else None)


class KeywordSample(StrictModel):
    snapshot_id: UUID | None
    payload_checksum: str | None = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    observed_at: str | None = Field(max_length=64)
    latitude: float | None = Field(ge=-90, le=90)
    longitude: float | None = Field(ge=-180, le=180)
    language: str
    device: Literal["mobile", "desktop"]
    selected_branch_ref: str | None

    @field_validator("observed_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value is not None and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("keyword observation timestamp requires timezone")
        return value


class KeywordVisibility(StrictModel):
    schema_version: Literal["keyword_visibility_v1"] = "keyword_visibility_v1"
    window: Literal["first_page"] = "first_page"
    rows: list[KeywordRow] = Field(min_length=3, max_length=5)
    local_pack: KeywordAssessment
    organic: KeywordAssessment
    sample: KeywordSample

    @model_validator(mode="after")
    def valid_assessments(self):
        if len({r.query.casefold() for r in self.rows}) != len(self.rows):
            raise ValueError("duplicate target keywords")
        for channel in ("local_pack", "organic"):
            if getattr(self, channel) != assessment(self.rows, channel):
                raise ValueError("keyword assessment does not match checked rows")
        return self


def prepare_keyword_input(manifest, request, *, confirmed_queries=None):
    """Derive a new V14 envelope; never alters the saved request or evidence."""
    from app.report_v22.capability import AnalysisInputManifest
    from app.report_v22.business_identity_v2 import BusinessIdentityV2
    from app.report_v22.identity_input import prepare_identity_input
    if not isinstance(request.business_identity, BusinessIdentityV2):
        manifest, request, _, _ = prepare_identity_input(manifest, request)
    queries = request.evidence_input.context.queries if confirmed_queries is None else confirmed_queries
    if queries != request.evidence_input.context.queries:
        raise ValueError("TARGET_KEYWORDS_DISCOVERY_CONFLICT")
    raw = deepcopy(manifest.model_dump(mode="json"))
    raw.update(schema_version="v22_analysis_input_v4", ruleset_version=KEYWORD_RULESET,
        keyword_queries=list(queries))
    raw["input_checksum"] = verified_request_digest({k:v for k,v in raw.items() if k != "input_checksum"})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request


def _profile(request):
    """Reuse P1-05's positive domain/phone gate, only on the bound GBP detail."""
    from app.preflight_v22.gbp_identity import assess_candidate, observations_from_site_selection, record_from_saved_mapping
    from app.preflight_v22.gbp_identity_models import GbpIdentityRequest
    from app.report_v22.business_identity_v2 import AlignmentIdentityV2
    from app.report_v22.site_business_facts import build_site_business_facts
    from app.report_v22.site_business_models import SiteBusinessFactsInput
    from app.report_v22.site_business_selection import select_site_business_candidates
    ctx, identity = request.evidence_input.context, request.business_identity
    source = next((s for s in request.evidence_input.sources if s.kind == "public_gbp"), None)
    ref = ctx.customer_public_gbp
    if (source is None or ref is None or source.payload.collection_status != "succeeded"
            or source.binding.identity_match_status != "matched" or identity.identity_match.state != "matched"):
        return None
    fields = source.payload.record.fields
    get = lambda name: getattr(fields, name).value if getattr(fields, name).state == "observed" else None
    row = {"business_name":get("business_name"), "website_url":get("website_url"),
        "phone":get("phone"), "address":get("address"),
        **{k.kind:k.value for k in source.payload.record.observed_entity_keys}}
    record = record_from_saved_mapping(row, source_ref=str(source.binding.snapshot_id),
        record_id=source.payload.request_record_id, payload_checksum=source.binding.payload_checksum,
        observed_at=source.binding.fetched_at, expires_at=source.binding.expires_at, kind="detail")
    site = next((s for s in request.evidence_input.sources if s.kind == "site"), None)
    facts = build_site_business_facts(SiteBusinessFactsInput(context=dict(case_id=ctx.case_id,
        report_type="prospect", site_url=ctx.site_url, evaluated_at=ctx.evaluated_at), source=site,
        limits=request.site_business_limits), resource_limits=True)
    selection = select_site_business_candidates(facts, AlignmentIdentityV2(identity, ctx.target_market), request.site_gbp_alignment_limits)
    observations = observations_from_site_selection(facts, selection)
    lookup = GbpIdentityRequest(case_ref=str(ctx.case_id), site_url=ctx.site_url, country_code=identity.country_code,
        target_market=ctx.target_market, evaluated_at=ctx.evaluated_at,
        phones=observations["phones"], names=observations["names"], saved_records=[record])
    if not assess_candidate(lookup, record).eligible:
        return None
    keys = {k.kind:k.value for k in source.payload.record.observed_entity_keys}
    if not any(keys.get(k.kind) == k.value for k in ref.entity_keys):
        return None
    branch = identity.selected_branch_ref
    if not branch or branch not in {*keys.values(), *(f"{k}:{v}" for k,v in keys.items())}:
        return None
    return keys


def _branch_match(record, keys):
    pairs = [(getattr(record, "provider_"+kind), value) for kind,value in keys.items()]
    comparable = [(a,b) for a,b in pairs if a]
    if comparable:
        return False if any(a != b for a,b in comparable) else True
    return None


def build_keyword_visibility(request):
    """Read only bound normalized first-page records, never observation prose."""
    from app.report_v22.evidence_bindings import host
    from app.report_v22.search_relevance import organic_scope_supported
    from app.report_v22.sample_locality import audit_sample
    ctx = request.evidence_input.context
    source = next((s for s in request.evidence_input.sources if s.kind == "serp"), None)
    ready = source is not None and source.binding.health_status == "healthy" and source.binding.identity_match_status == "matched"
    profile = _profile(request) if ready else None
    rows = []
    for index, query in enumerate(ctx.queries):
        prefix = f"/payload/query_runs/{index}"
        run = source.payload.query_runs[index] if ready else None
        channels = {}
        for channel in ("local_pack", "organic", "maps"):
            call_name = "maps_call" if channel == "maps" else "google_call"
            paths = [prefix+"/"+call_name+"/status"] if source else []
            selected = [(i,r) for i,r in enumerate(run.results) if r.result_type == channel] if run else []
            reason = "source_unavailable"
            status, position, basis, matched = "not_checked", None, None, []
            if run:
                if getattr(run, call_name).status != "succeeded": reason = "request_failed"
                elif not selected: reason = "section_unavailable"
                elif any(f"SERP_{channel.upper()}_{suffix}" in run.limitations for suffix in ("SECTION_INVALID",)):
                    reason = "parse_incomplete"
                else:
                    decisions = [(i,r,(host(r.url)==host(ctx.site_url) if r.url is not None else None)
                        if channel == "organic" else _branch_match(r,profile) if profile else None) for i,r in selected]
                    hits = [(i,r) for i,r,match in decisions if match is True]
                    if hits:
                        if len({r.rank_source for _,r in hits}) > 1: reason = "rank_basis_mismatch"
                        else:
                            status, reason = "observed", "observed"
                            position = min(r.position for _,r in hits)
                            matched = [(i,r) for i,r in hits if r.position == position]
                            basis = matched[0][1].rank_source
                    elif any(match is None for _,_,match in decisions): reason = "identity_unresolved"
                    elif any(f"SERP_{channel.upper()}_{suffix}" in run.limitations for suffix in ("RESULT_SKIPPED", "DUPLICATE_SKIPPED")):
                        reason = "parse_incomplete"
                    else: status, reason = "not_detected", "first_page_absent"
                    if channel == "organic" and (not organic_scope_supported(query, [r for _,r in selected], market=ctx.target_market.city)
                            or audit_sample([r for _,r in selected], ctx.target_market)["non_local_majority"]):
                        status, reason, position, basis, matched = "not_checked", "sample_not_local", None, None, []
            for i,_ in selected:
                paths.extend(f"{prefix}/results/{i}/{field}" for field in ("result_type", "url" if channel == "organic" else "provider_cid"))
            for i,_ in matched:
                paths.extend(f"{prefix}/results/{i}/{field}" for field in ("position", "rank_source"))
            channels[channel] = KeywordPosition(status=status, position=position, rank_source=basis, reason=reason,
                checked_count=len(selected), record_ids=[r.record_id for _,r in matched], evidence_paths=list(dict.fromkeys(paths)))
        rows.append(KeywordRow(query=query, **channels))
    return KeywordVisibility(rows=rows, local_pack=assessment(rows,"local_pack"), organic=assessment(rows,"organic"),
        sample=KeywordSample(snapshot_id=source.binding.snapshot_id if source else None,
            payload_checksum=source.binding.payload_checksum if source else None,
            observed_at=source.binding.fetched_at.isoformat() if source else None,
            latitude=source.payload.target_point.latitude if source else None, longitude=source.payload.target_point.longitude if source else None,
            language=ctx.search_language, device=ctx.search_device, selected_branch_ref=request.business_identity.selected_branch_ref))
