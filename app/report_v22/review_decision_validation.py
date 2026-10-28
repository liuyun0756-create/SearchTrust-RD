"""Binding to historical evidence; no new evidence or causal inference."""
import json

from pydantic import BaseModel

from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.capability import instant
from app.report_v22.review_evidence_validation import source_references
from app.report_v22.review_freeze_storage import decode
from app.report_v22.review_decision_models import MAX_INPUT, SaveDecision


def error(code="INVALID"):
    return DeterministicJobError(f"V22_REVIEW_DECISION_{code}", "The review decision request is invalid, unavailable or has changed.")


def parse(model, value, limit):
    try:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        raw = value.encode("utf-8") if isinstance(value, str) else value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > limit:
            raise error("TOO_LARGE")
        # Decode first to reject duplicate keys, then strict JSON-native types.
        decoded = decode(raw)
        # json.loads accepts NaN/Infinity, while orjson would silently turn them
        # into null. Reject before serializing nullable fields or trusting hashes.
        json.dumps(decoded, allow_nan=False)
        return model.model_validate_json(canonical_json_bytes(decoded))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise error() from None


def normalize(value, saved):
    request = parse(SaveDecision, value, MAX_INPUT)
    if request.review_id != saved.id:
        raise error("UNAVAILABLE")
    b = saved.bundle
    original = [a.id for a in b.delivery.actions]
    implemented = {e.action_id for e in b.implementation.entries}
    refs = {s.action_id: set(source_references(b, s)) for s in b.selections}
    all_refs = set().union(*refs.values()) if refs else set()
    for d in request.decisions:
        new = d.disposition == "new"
        if new == (d.action_id in original):
            raise error()
        if not set(d.references) <= (all_refs if new else refs[d.action_id]):
            raise error()
        if d.disposition == "observe" and d.action_id not in implemented:
            raise error()
    # Stable approved order, then explicit order of new work. No text rewriting.
    ordered = [d for action in original for d in request.decisions if d.action_id == action]
    ordered += [d for d in request.decisions if d.disposition == "new"]
    raw = request.model_dump(mode="json")
    raw["decisions"] = [d.model_dump(mode="json") | {"references": sorted(d.references)} for d in ordered]
    return parse(SaveDecision, raw, MAX_INPUT)


def validate_revision(record, saved, *, user_id, case_id, report_id):
    b = saved.bundle
    if (record.user_id, record.case_id, record.report_id, record.review_id) != (user_id, case_id, report_id, saved.id):
        raise error()
    if record.evidence_checksum != b.evidence_checksum or record.bundle_checksum != b.checksum:
        raise error()
    if instant(record.recorded_at) < max(instant(saved.frozen_at), instant(b.evaluated_at)):
        raise error()
    if normalize(record.request(), saved) != record.request():
        raise error()
    return record
