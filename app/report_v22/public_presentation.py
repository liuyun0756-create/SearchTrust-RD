"""Version-gated wording and grouping; no judgment or action selection."""
from app.report_v22.public_rule_catalog import OBSERVED

POSITION_RULESETS = frozenset({"v22_capability_public_v12", "v22_capability_public_v13"})
DOMAIN_GROUP = "Domain coverage remains unverified for these saved samples:\n"
COMPARISON_GROUP = "Full competitor-position comparisons remain incomplete for these saved samples:\n"
_GROUPS = (("Domain coverage for ", DOMAIN_GROUP),
           ("The full competitor-position comparison for ", COMPARISON_GROUP))


def zero_action_summary(findings, evidence):
    by_id = {item["id"]: item for item in evidence}
    groups = {"maps": [], "organic": []}
    for finding in sorted(findings, key=lambda f: f["target"]):
        if finding["rule_id"] != OBSERVED:
            continue
        # Decode the existing target format, never infer positions from prose.
        query, kind, _, _, _ = finding["target"].rsplit(" · ", 4)
        positions = sorted({by_id[key]["value"] for key in finding["evidence_ids"]
            if any(path.endswith("/position") for path in by_id[key]["field_path"].splitlines())})
        if not positions or any(type(position) is not int for position in positions):
            raise ValueError("POSITION_EVIDENCE_REQUIRED")
        groups[kind].append(f"{query}: {', '.join(map(str, positions))}")
    observations = [f"Observed {label} positions: {'; '.join(groups[kind])}."
        for kind, label in (("maps", "Maps"), ("organic", "Organic search")) if groups[kind]]
    return " ".join(["This sample did not identify a supported change to propose.", *observations,
        "Unverified checks and sample limits are listed below."])


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
    if not raw["actions"]:
        raw["summary"] = zero_action_summary(raw["findings"], raw["evidence"])
    raw["limitations"] = group_limits(raw["limitations"])
