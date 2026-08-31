"""Scalar observation helpers shared by the offline adapters."""

from datetime import date, datetime
from pydantic import HttpUrl

from app.report_v22.evidence_identity import stable_key
from app.report_v22.evidence_models import EvidenceObservation, EvidenceSelector
from app.report_v22.models import SourceLocator

LIMITATIONS_OVERFLOW = "Additional limitations are retained in the internal source summary."


def full_limitations(source):
    notes = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"limitations", "health_reasons"}:
                    notes.extend(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(source.payload.model_dump(mode="json"))
    return sorted(set(notes))


def limited(notes):
    notes = sorted(set(notes))
    return notes if len(notes) <= 20 else notes[:19] + [LIMITATIONS_OVERFLOW]


def scalar(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, HttpUrl):
        return str(value)
    return value


def observe(source, category, record_key, field, value, path, *, locator=None,
            competitor_id=None, dimensions=(), metric_key=None, unit=None, request_context=None,
            collected_at=None, limitations=(), confidence=None, health_status="healthy",
            coverage_start=None, coverage_end=None):
    value = scalar(value)
    return EvidenceObservation(
        snapshot_id=source.binding.snapshot_id, source_type=source.binding.source_type,
        selector=EvidenceSelector(category=category, record_key=stable_key(record_key), field=field,
            record_context=record_key if isinstance(record_key,list) else [record_key],
            competitor_id=competitor_id, dimensions=list(dimensions), metric_key=metric_key, unit=unit, request_context=request_context),
        source_locator=locator or SourceLocator(), original_value=value, normalized_value=value,
        collected_at=collected_at or source.binding.fetched_at,
        confidence=confidence or ("high" if source.kind == "first_party" else "medium"),
        health_status=health_status, limitations=sorted(set(limitations)), origin_paths=[path],
        coverage_start=coverage_start, coverage_end=coverage_end,
    )


def coverage(source, record_key, field, reason, value, path, *, health_status=None, **kwargs):
    item = observe(source,"coverage",record_key,field,value,path, confidence="low",
        health_status=health_status or source.binding.health_status, **kwargs)
    return item.model_copy(update={"source_type":"coverage", "normalized_value":reason, "gap_reason":reason})


def fields(source, category, record_key, model, prefix, whitelist, **kwargs):
    """Only explicitly whitelisted fields; lists are individual scalar observations."""
    for field in whitelist:
        value = getattr(model, field)
        if isinstance(value, list):
            for index, element in enumerate(value):
                yield observe(source,category,record_key,field,element,f"{prefix}/{field}/{index}",**kwargs)
        elif value is not None:
            yield observe(source,category,record_key,field,value,f"{prefix}/{field}",**kwargs)
