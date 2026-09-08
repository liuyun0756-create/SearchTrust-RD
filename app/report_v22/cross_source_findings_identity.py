"""Stable V22-071 Finding identity independent of prose and traversal order."""

from uuid import UUID

from app.report_v22.evidence_identity import stable_key


IDENTITY_VERSION = "v22_cross_source_finding_identity_v1"
RULESET_VERSION = "v22_cross_source_findings_v1"


def cross_source_finding_id(
    *, case_id: UUID, rule_id: str, rule_version: str, target: object,
    evidence_ids: list[str], comparator_ids: list[str],
) -> str:
    dumped = target.model_dump(mode="json") if hasattr(target, "model_dump") else target
    return "fn_" + stable_key({
        "identity_version": IDENTITY_VERSION,
        "ruleset_version": RULESET_VERSION,
        "case_id": str(case_id),
        "rule_id": rule_id,
        "rule_version": rule_version,
        "target": dumped,
        "evidence_ids": sorted(set(evidence_ids)),
        "comparator_ids": sorted(set(comparator_ids)),
    })
