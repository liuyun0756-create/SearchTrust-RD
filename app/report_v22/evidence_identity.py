"""Versioned semantic identity, independent of traversal order and physical paths."""

from hashlib import sha256

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.evidence_models import EvidenceObservation, EvidenceSelector, reject_nonfinite

IDENTITY_VERSION = "v22_evidence_identity_v1"


def stable_key(value: object) -> str:
    reject_nonfinite(value)
    return sha256(canonical_json_bytes(value)).hexdigest()


def selector_path(selector: EvidenceSelector) -> str:
    return "selector:" + stable_key(selector.model_dump(mode="json"))


def evidence_id(observation: EvidenceObservation) -> str:
    value = observation.normalized_value
    reject_nonfinite(value)
    locator = observation.source_locator.model_dump(mode="json")
    locator["field_path"] = selector_path(observation.selector)
    return "ev_" + stable_key({
        "version": IDENTITY_VERSION,
        "snapshot_id": str(observation.snapshot_id),
        "source_type": observation.source_type,
        "selector": observation.selector.model_dump(mode="json"),
        "locator": locator,
        "value": {"type": type(value).__name__, "value": value},
    })
