"""Stable identities and complete parent-Finding fingerprints for V22-073."""

from app.jobs_v22.digest import request_digest
from app.report_v22.evidence_identity import stable_key


IDENTITY_VERSION = "v22_version_diff_identity_v1"
RULESET_VERSION = "v22_version_diff_v1"


def finding_fingerprint(finding: object) -> str:
    """Fingerprint every canonical field of the immutable parent Finding."""
    return request_digest(finding)


def diff_audit_id(
    *, parent_report_id: object, change_type: str, previous_finding_id: str | None,
    current_finding_refs: list[object],
) -> str:
    dumped = [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        for item in current_finding_refs
    ]
    return "vda_" + stable_key({
        "identity_version": IDENTITY_VERSION,
        "ruleset_version": RULESET_VERSION,
        "parent_report_id": str(parent_report_id),
        "change_type": change_type,
        "previous_finding_id": previous_finding_id,
        "current_finding_refs": dumped,
    })
