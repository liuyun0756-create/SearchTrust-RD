"""Stable identities for V22-072 relations and controlled measurement actions."""

from app.report_v22.evidence_identity import stable_key


IDENTITY_VERSION = "v22_verified_reprioritization_identity_v1"
RULESET_VERSION = "v22_verified_reprioritization_v1"


def relation_id(*, finding_ref: object, relation_kind: str, candidate_key: str | None) -> str:
    dumped = finding_ref.model_dump(mode="json") if hasattr(finding_ref, "model_dump") else finding_ref
    return "rel_" + stable_key({
        "identity_version": IDENTITY_VERSION,
        "ruleset_version": RULESET_VERSION,
        "finding_ref": dumped,
        "relation_kind": relation_kind,
        "candidate_key": candidate_key,
    })


def measurement_action_id(*, template_key: str, source_types: list[str], issue_codes: list[str]) -> str:
    return "ac_verified_" + stable_key({
        "identity_version": IDENTITY_VERSION,
        "ruleset_version": RULESET_VERSION,
        "template_key": template_key,
        "source_types": sorted(set(source_types)),
        "issue_codes": sorted(set(issue_codes)),
    })[:48]
