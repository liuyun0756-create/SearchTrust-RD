"""Version-gated wording and grouping; no judgment or action selection."""
import logging

from app.report_v22.public_rule_catalog import OBSERVED

logger = logging.getLogger(__name__)
POSITION_RULESETS = frozenset({"v22_capability_public_v12", "v22_capability_public_v13"})
NEUTRAL_ZERO_ACTION_SUMMARY = ("This sample did not identify a supported change to propose. "
    "Unverified checks and sample limits are listed below.")
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
DOMAIN_GROUP = "Domain coverage remains unverified for these saved samples:\n"
COMPARISON_GROUP = "Full competitor-position comparisons remain incomplete for these saved samples:\n"
_GROUPS = (("Domain coverage for ", DOMAIN_GROUP),
           ("The full competitor-position comparison for ", COMPARISON_GROUP))


def zero_action_summary(findings, evidence):
    by_id = {item["id"]: item for item in evidence}
    groups = {"maps": [], "organic": []}
    skipped = 0
    for finding in sorted(findings, key=lambda f: f["target"]):
        if finding["rule_id"] != OBSERVED:
            continue
        # Decode the existing target format, never infer positions from prose.
        query, kind, _, _, _ = finding["target"].rsplit(" · ", 4)
        positions = sorted({item["value"] for key in finding["evidence_ids"]
            if (item := by_id.get(key)) is not None and type(item["value"]) is int
            and any(path.endswith("/position") for path in item["field_path"].splitlines())})
        if not positions:
            skipped += 1
            continue
        groups[kind].append(f"{query}: {', '.join(map(str, positions))}")
    observations = [f"Observed {label} positions: {'; '.join(groups[kind])}."
        for kind, label in (("maps", "Maps"), ("organic", "Organic search")) if groups[kind]]
    if skipped:
        # Presentation degradation is observable, never a new evidence judgment.
        logger.warning("ZERO_ACTION_SUMMARY_DEGRADED", extra={
            "event": "zero_action_summary_degraded", "skipped_position_findings": skipped,
            "summary_level": "partial_positions" if observations else "neutral_without_positions"})
    return " ".join(["This sample did not identify a supported change to propose.", *observations,
        "Unverified checks and sample limits are listed below."])


def finding_display_key(finding):
    """Severity first; semantic card order never changes action priority."""
    tier = (0 if finding["rule_id"] == OBSERVED else
        1 if finding.get("kind") == "fact" and finding.get("status") == "confirmed" else 2)
    return (-_SEVERITY_ORDER[finding["severity"]], tier, finding["id"])


def group_limits(values):
    """Retain each original sentence verbatim, with its query/type context."""
    values = list(dict.fromkeys(values))
    groups = {prefix: [value for value in values if value.startswith(prefix)] for prefix, _ in _GROUPS}
    result, emitted = [], set()
    for value in values:
        for prefix, heading in _GROUPS:
            if value.startswith(prefix) and len(groups[prefix]) > 1:
                if prefix not in emitted:
                    result.append(heading + "\n".join(groups[prefix]))
                    emitted.add(prefix)
                break
        else:
            result.append(value)
    return result


def finish_presentation(raw, manifest):
    if manifest.ruleset_version not in POSITION_RULESETS:
        return
    # Run after annotation and action selection. Only the card list is reordered.
    raw["findings"] = sorted(raw["findings"], key=finding_display_key)
    if not raw["actions"]:
        raw["summary"] = zero_action_summary(raw["findings"], raw["evidence"])
    raw["limitations"] = group_limits(raw["limitations"])
