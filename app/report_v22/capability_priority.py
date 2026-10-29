"""P4 deterministic, ordinal prioritization. No revenue score or text guessing.

The frozen BusinessScope is the only business input. Arbitrary historical
priority prose is retained, not interpreted as a confirmed objective. Candidate
inputs and the selection trace are persisted, including candidates outside the
three-action delivery budget. No inference is made from the report title.
"""
from urllib.parse import urlsplit
from copy import deepcopy
from app.report_v22.public_rule_catalog import HTTP, NOINDEX

VERSION = "v22_business_priority_v1"
OBJECTIVES = {
    "service_visibility": {"title_brief", "service_brief"},
    "contact_path": {"service_brief", "profile_correction"},
    "business_identity": {"profile_correction"},
}
LIMIT = "Business return is unknown. This order is not a revenue forecast or evidence that SEO caused a change."
RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def candidate_input(action):
    return dict(action_id=action["id"], target=action["target"], finding_ids=sorted(action["finding_ids"]),
        dependencies=sorted(action["dependencies"]),
        material_kinds=sorted({m["kind"] for m in action["materials"]}),
        readiness=sorted({m["readiness"] for m in action["materials"]}))


def _measurement_rule(rule):
    return (any(rule.startswith(prefix + ".") for prefix in (
        "V22.FIRST_PARTY.GSC.MEASUREMENT_COVERAGE", "V22.FIRST_PARTY.GA4.MEASUREMENT_CONFIGURATION",
        "V22.FIRST_PARTY.GBP.PROFILE_OR_MEASUREMENT"))
        or rule in {"V22.CROSS_SOURCE.GSC_GA4.PAGE_DIRECTION_CONFLICT",
            "V22.CROSS_SOURCE.GSC_GA4.AGGREGATE_DIRECTION_CONFLICT",
            "V22.CROSS_SOURCE.GSC_GBP.VISIBILITY_CONFLICT", "V22.CROSS_SOURCE.GA4_GBP.BEHAVIOR_CONFLICT",
            "V22.CROSS_SOURCE.GSC_GA4.WEEKLY_CONFLICT"})


def _page(value, site):
    """Match only explicit same-business HTTP(S) pages; never a substring."""
    try:
        parsed, business = urlsplit(value), urlsplit(site)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.hostname.lower().removeprefix("www.") != business.hostname.lower().removeprefix("www.")):
            return None
        port = parsed.port
        if port == (443 if parsed.scheme == "https" else 80): port = None
        return (parsed.hostname.lower().removeprefix("www."), port, parsed.path or "/", parsed.query)
    except (ValueError, AttributeError):
        return None


def prioritize(inputs, findings, business, input_checksum, *, rules_version=VERSION):
    """Return a replayable trace: precedence, focus, support, severity,
    confidence, execution conditions, then exact stable action identity.
    Candidate enumeration order and caller wording never break a tie.
    """
    inputs = deepcopy(inputs)
    if rules_version not in {VERSION, "v22_business_priority_v2"}:
        raise ValueError("unknown priority version")
    conditions = business.get("execution_conditions") or {"capacity": "unknown", "publishing_access": "unknown"}
    by_id = {f["id"]: f for f in findings}
    if len({c["action_id"] for c in inputs}) != len(inputs):
        raise ValueError("duplicate priority candidate")
    ids = {c["action_id"] for c in inputs}
    pages = {_page(p, business["site_url"]) for p in business["target_pages"]} - {None}
    objective = business["priority"] if business["priority"] in OBJECTIVES else None
    entries = []
    for candidate in sorted(inputs, key=lambda c: c["action_id"]):
        if any(candidate[key] != sorted(set(candidate[key])) for key in ("finding_ids", "dependencies", "material_kinds", "readiness")):
            raise ValueError("noncanonical priority input")
        if (not set(candidate["dependencies"]) <= ids or candidate["action_id"] in candidate["dependencies"]
                or not candidate["finding_ids"] or not set(candidate["finding_ids"]) <= by_id.keys()):
            raise ValueError("invalid priority references")
        linked = [by_id[fid] for fid in candidate["finding_ids"]]
        # Exact rule identities, not material titles or free-form prose.
        prerequisite = ("access_indexing" if any(f["rule_id"] in {HTTP, NOINDEX} for f in linked) else
            "measurement" if any(_measurement_rule(f["rule_id"]) for f in linked) else "none")
        match = ("confirmed_target" if any(_page(t, business["site_url"]) in pages for t in candidate["target"].split("\n")) else
            "confirmed_objective" if objective and (set(candidate["material_kinds"]) & OBJECTIVES[objective]
                or rules_version == "v22_business_priority_v2" and objective == "contact_path" and any(f["rule_id"] in {
                    "V22.FIRST_PARTY.GA4.ENGAGEMENT_GAP", "V22.FIRST_PARTY.GA4.CONVERSION_GAP"} for f in linked)) else
            "not_matched" if pages or objective else "unknown")
        support = ("conflicting" if any(f["interpretation"] == "conflicting" for f in linked) else
            "needs_verification" if any(f["interpretation"] == "needs_verification" or f["classification"] == "estimate" for f in linked) else "supported")
        execution = ("confirmation_required" if "requires_confirmation" in candidate["readiness"] else
            "ready_to_use" if candidate["readiness"] and set(candidate["readiness"]) == {"ready_to_use"} else "instructions_only")
        publishing = bool(set(candidate["material_kinds"]) & {"title_brief", "service_brief", "profile_correction"})
        if rules_version == "v22_business_priority_v2" and publishing and conditions["publishing_access"] == "needs_approval":
            execution = "confirmation_required"
        codes = ["prerequisite_" + prerequisite, "business_" + match, "evidence_" + support,
            "execution_" + execution, "severity_" + max((f["severity"] for f in linked), key=RANK.__getitem__),
            "confidence_" + max((f["confidence"] for f in linked), key=RANK.__getitem__), "return_unknown", "stable_identity_tiebreak"]
        if candidate["dependencies"]: codes.append("dependencies_first")
        if rules_version == "v22_business_priority_v2":
            codes.extend(["capacity_" + conditions["capacity"], "publishing_" + conditions["publishing_access"]])
        explanation = {
            "access_indexing": "Check access and indexing before optimizing affected pages.",
            "measurement": "Establish reliable measurement before interpreting performance.",
            "none": {"confirmed_target": "This work addresses an explicitly confirmed target page.",
                "confirmed_objective": "This work matches the explicitly confirmed business objective.",
                "not_matched": "No direct match to the confirmed focus; this is not a low-value judgment.",
                "unknown": "Business focus is not specified; order uses evidence and problem attributes, not assumed business value."}[match],
        }[prerequisite]
        if support == "conflicting": explanation += " Conflicting evidence calls for investigation, not a claimed business cause."
        elif support == "needs_verification": explanation += " Confirm the stated assumptions before changing anything."
        if execution == "confirmation_required": explanation += " Business details or approval are still required."
        if rules_version == "v22_business_priority_v2":
            if publishing and conditions["capacity"] == "limited":
                explanation += " Limited execution capacity is confirmed; reserve a separate slot for changes. No duration is estimated."
            if publishing and conditions["publishing_access"] == "unknown":
                explanation += " Publishing access is unknown, not assumed blocked or approved."
        entries.append(dict(input=candidate, prerequisite=prerequisite, business_match=match, evidence_support=support,
            execution=execution, severity=max((f["severity"] for f in linked), key=RANK.__getitem__),
            confidence=max((f["confidence"] for f in linked), key=RANK.__getitem__),
            impact_paths=list(dict.fromkeys(f["impact_path"] for f in linked)),
            comparison_scopes=list(dict.fromkeys(f["comparison_scope"] for f in linked)),
            reason_codes=codes, explanation=explanation, selected_order=None))
    def key(e):
        return ({"access_indexing": 0, "measurement": 1, "none": 2}[e["prerequisite"]],
            {"confirmed_target": 0, "confirmed_objective": 1, "not_matched": 2, "unknown": 2}[e["business_match"]],
            {"supported": 0, "needs_verification": 1, "conflicting": 2}[e["evidence_support"]],
            -RANK[e["severity"]], -RANK[e["confidence"]],
            int(rules_version == "v22_business_priority_v2" and conditions["capacity"] == "limited"
                and bool(set(e["input"]["material_kinds"]) & {"title_brief", "service_brief", "profile_correction"})),
            {"ready_to_use": 0, "instructions_only": 1, "confirmation_required": 2}[e["execution"]], e["input"]["action_id"])
    chosen = []
    pending = list(entries)
    # Validate the entire graph, including candidates beyond the delivery cap.
    ordered = set()
    while pending:
        available = [e for e in pending if set(e["input"]["dependencies"]) <= ordered]
        if not available: raise ValueError("cyclic priority dependencies")
        entry = min(available, key=key)
        ordered.add(entry["input"]["action_id"])
        if len(chosen) < 3:
            chosen.append(entry["input"]["action_id"])
            entry["selected_order"] = len(chosen)
        else: entry["reason_codes"].append("outside_delivery_budget")
        pending.remove(entry)
    return dict(rules_version=rules_version, input_checksum=input_checksum, business_priority=business["priority"],
        target_pages=list(business["target_pages"]), action_order=chosen, candidates=entries, limitations=[LIMIT])


def apply_priority(raw, manifest):
    candidates = raw["actions"]
    trace = prioritize([candidate_input(a) for a in candidates], raw["findings"],
        manifest.business.model_dump(mode="json"), manifest.input_checksum,
        rules_version="v22_business_priority_v2" if manifest.ruleset_version.endswith(("_v3", "_v4")) else VERSION)
    by_id = {a["id"]: a for a in candidates}
    raw["actions"] = [by_id[key] for key in trace["action_order"]]
    raw["prioritization"] = trace
    raw["limitations"] = list(dict.fromkeys([*raw["limitations"], LIMIT]))
    if raw["actions"]:
        lead = next(f for f in raw["findings"] if f["id"] == raw["actions"][0]["finding_ids"][0])
        raw["summary"] = f"{lead['observed_fact']} {lead['judgment']}"
    return raw


def valid_delivery_order(ids, trace):
    if len(set(ids)) != len(ids) or set(ids) != set(trace["action_order"]): return False
    entries = {e["input"]["action_id"]: e for e in trace["candidates"]}
    seen = set()
    layer = -1
    for identifier in ids:
        entry = entries[identifier]
        current = {"access_indexing": 0, "measurement": 1, "none": 2}[entry["prerequisite"]]
        if current < layer or not set(entry["input"]["dependencies"]) <= seen: return False
        seen.add(identifier)
        layer = current
    return True
