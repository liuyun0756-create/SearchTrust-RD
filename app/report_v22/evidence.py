"""Pure, fail-closed snapshot-backed EvidenceItem builder for V22-031.

Only evidence_index belongs in the frozen report. Audit records remain internal.
Inputs must be authorized by the caller; this module performs no persistence,
network access, clock reads, findings, or report assembly.
"""

from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.evidence_adapters import site, serp, competitor, first_party
from app.report_v22.evidence_adapters.common import coverage, full_limitations, limited
from app.report_v22.evidence_bindings import eligibility, validate_sources
from app.report_v22.evidence_errors import EvidenceError
from app.report_v22.evidence_identity import evidence_id, selector_path
from app.report_v22.evidence_models import (
    EvidenceBuildInput, EvidenceBuildResult, EvidenceCoverageGap, EvidenceSourceSummary,
    EvidenceSourceTrace, reject_nonfinite,
)
from app.report_v22.models import EvidenceItem, SourceLocator


def resolve_origin(source, pointer):
    """Resolve a JSON pointer against its bound source wrapper, not another source."""
    if not pointer.startswith("/"):
        raise EvidenceError("SOURCE_INVALID")
    value = source
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value,list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        raise EvidenceError("SOURCE_INVALID") from None


def _ineligible(source, reason, notes, competitor_id=None):
    b=source.binding
    field = "expires_at" if reason=="expired" and b.expires_at is not None else "health_status"
    if reason in {"identity_mismatch","identity_unconfirmed"}:
        field="identity_match_status"
    value=getattr(b,field)
    locator=SourceLocator()
    request_context=None
    if source.kind=="first_party":
        request_context=source.payload.provider_request_context
        locator=SourceLocator(external_resource_id=request_context.external_resource_id)
    return coverage(source,source.binding.source_type,field,reason,value,f"/binding/{field}",
        locator=locator,request_context=request_context,limitations=notes,competitor_id=competitor_id,
        health_status="expired" if reason=="expired" else b.health_status)


def build_evidence_index(value: EvidenceBuildInput | dict) -> EvidenceBuildResult:
    try:
        # Revalidate nested models too: model_copy/update intentionally skips validators.
        reject_nonfinite(value)
        data=value.model_dump(mode="python") if isinstance(value,EvidenceBuildInput) else value
        request=EvidenceBuildInput.model_validate(data)
        sources=validate_sources(request)
        items, traces, fingerprints = {}, {}, {}
        summaries=[]
        retained_bytes=0
        gaps=[EvidenceCoverageGap(source_type=m.source_type,reason=m.reason,competitor_id=m.competitor_id) for m in request.missing_sources]
        adapters={"site":site,"serp":serp,"competitor":competitor,"first_party":first_party}
        for source in sources:
            notes=set(full_limitations(source))
            reason=eligibility(source,request.context.evaluated_at)
            if reason:
                identities = [item.competitor.competitor_id for item in source.payload.competitors] if source.kind=="competitor" else [None]
                observations=[_ineligible(source,reason,notes,identity) for identity in identities]
            else:
                observations=adapters[source.kind].observations(source)
            source_json=source.model_dump(mode="json")
            source_ids=set()
            for observation in observations:
                reject_nonfinite(observation)
                if observation.snapshot_id != source.binding.snapshot_id:
                    raise EvidenceError("SNAPSHOT_BINDING_INVALID")
                if observation.source_type not in {source.binding.source_type,"coverage"}:
                    raise EvidenceError("SNAPSHOT_BINDING_INVALID")
                if (observation.source_type=="coverage") != (observation.gap_reason is not None):
                    raise EvidenceError("SOURCE_INVALID")
                if source.kind=="competitor" and observation.selector.competitor_id not in {item.competitor.competitor_id for item in source.payload.competitors}:
                    raise EvidenceError("SNAPSHOT_BINDING_INVALID")
                for path in observation.origin_paths:
                    resolve_origin(source_json,path)
                identifier=evidence_id(observation)
                locator=observation.source_locator.model_copy(update={"field_path":selector_path(observation.selector)})
                item=EvidenceItem(
                    evidence_id=identifier,snapshot_id=observation.snapshot_id,source_type=observation.source_type,
                    source_locator=locator,original_value=observation.original_value,normalized_value=observation.normalized_value,
                    collected_at=observation.collected_at,coverage_start=observation.coverage_start,coverage_end=observation.coverage_end,
                    confidence=observation.confidence,health_status=observation.health_status,limitations=limited(observation.limitations))
                # Canonical bytes distinguish True/1, float/int, and every frozen field.
                fingerprint=canonical_json_bytes({"item":item.model_dump(mode="json"),"selector":observation.selector.model_dump(mode="json")})
                if identifier in items:
                    if fingerprints[identifier] != fingerprint:
                        raise EvidenceError("ID_CONFLICT")
                    previous_size=len(canonical_json_bytes(traces[identifier]))
                    traces[identifier].origin_paths=sorted(set([*traces[identifier].origin_paths,*observation.origin_paths]))
                    retained_bytes+=len(canonical_json_bytes(traces[identifier]))-previous_size
                else:
                    items[identifier]=item
                    fingerprints[identifier]=fingerprint
                    traces[identifier]=EvidenceSourceTrace(evidence_id=identifier,snapshot_id=item.snapshot_id,
                        selector=observation.selector,origin_paths=sorted(set(observation.origin_paths)))
                    retained_bytes+=len(canonical_json_bytes(item))+len(canonical_json_bytes(traces[identifier]))
                    if len(items)>request.limits.max_items:
                        raise EvidenceError("LIMIT_EXCEEDED")
                # A lower bound on final output size stops oversized builds early;
                # the exact final check also includes summaries, gaps and syntax.
                if retained_bytes>request.limits.max_bytes:
                    raise EvidenceError("LIMIT_EXCEEDED")
                source_ids.add(identifier)
                notes.update(observation.limitations)
                if observation.gap_reason:
                    gaps.append(EvidenceCoverageGap(source_type=source.binding.source_type,reason=observation.gap_reason,
                        snapshot_id=source.binding.snapshot_id,competitor_id=observation.selector.competitor_id,evidence_id=identifier))
            summaries.append(EvidenceSourceSummary(snapshot_id=source.binding.snapshot_id,source_type=source.binding.source_type,
                health_status=source.binding.health_status,identity_match_status=source.binding.identity_match_status,
                business_eligible=reason is None,evidence_count=len(source_ids),limitations=sorted(notes)))
        gap_map={canonical_json_bytes(g):g for g in gaps}
        result=EvidenceBuildResult(evidence_index=[items[k] for k in sorted(items)],source_traces=[traces[k] for k in sorted(traces)],
            source_summaries=summaries,coverage_gaps=[gap_map[k] for k in sorted(gap_map)])
        if len(canonical_json_bytes(result))>request.limits.max_bytes:
            raise EvidenceError("LIMIT_EXCEEDED")
        return result
    except (ValidationError, ValueError, TypeError):
        raise EvidenceError("SOURCE_INVALID") from None
