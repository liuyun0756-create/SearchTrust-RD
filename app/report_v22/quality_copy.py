"""Reader-facing limits. Internal rule IDs remain in the evaluation record."""
from app.report_v22.public_rule_catalog import AHEAD, ASSET, DOMAIN, GBP_RULES


def unchecked_limits(evaluations):
    limits = set()
    labels = dict(zip(GBP_RULES, ("Business name", "Address", "Phone number", "Service areas")))
    for evaluation in evaluations:
        if evaluation.state != "not_checked":
            continue
        if evaluation.rule_id in labels:
            limits.add(f"{labels[evaluation.rule_id]}: the saved sources do not support a reliable website/profile comparison. Confirm the current details before proposing a correction.")
        elif evaluation.rule_id in {AHEAD, DOMAIN}:
            kind = evaluation.target.result_type.replace("_", " ")
            if evaluation.rule_id == DOMAIN:
                limits.add(f"Domain coverage for '{evaluation.target.query}' ({kind}) remains unverified: the returned sample did not support a reliable absence/presence judgment. This does not establish absence or a ranking problem.")
            else:
                limits.add(f"The full competitor-position comparison for '{evaluation.target.query}' ({kind}) could not be completed. Use only the supported same-context positions shown in the findings; do not infer an absolute ranking.")
        elif evaluation.rule_id == ASSET:
            limits.add("The saved page samples do not establish a service-page gap. Check the existing pages and confirmed offering before proposing a new page.")
        else:
            limits.add("Some checks could not be completed within the saved evidence. Confirm their scope before deciding on changes.")
    return sorted(limits)
