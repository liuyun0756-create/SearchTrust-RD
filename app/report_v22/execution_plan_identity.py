"""Stable V22-074 audit identities."""

from app.report_v22.evidence_identity import stable_key


IDENTITY_VERSION = "v22_execution_plan_identity_v1"
RULESET_VERSION = "v22_execution_plan_v1"


def metric_audit_id(*, action_id: str, metric_key: str, role: str) -> str:
    return "ema_" + stable_key({
        "identity_version": IDENTITY_VERSION,
        "ruleset_version": RULESET_VERSION,
        "action_id": action_id,
        "metric_key": metric_key,
        "role": role,
    })
