"""Finding identities never depend on traversal order or generated prose."""
from uuid import UUID

from app.report_v22.evidence_identity import stable_key
from app.report_v22.findings_models import RuleTarget
from app.report_v22.public_rule_catalog import RULESET_VERSION

IDENTITY_VERSION = "v22_public_finding_identity_v1"


def finding_id(*, case_id: UUID, rule_id: str, rule_version: str, target: RuleTarget,
               evidence_ids: list[str], comparator_ids: list[str]) -> str:
    return "fn_" + stable_key({
        "identity_version": IDENTITY_VERSION, "ruleset_version": RULESET_VERSION,
        "case_id": str(case_id), "rule_id": rule_id, "rule_version": rule_version,
        "target": target.model_dump(mode="json"),
        "evidence_ids": sorted(set(evidence_ids)), "comparator_ids": sorted(set(comparator_ids)),
    })
