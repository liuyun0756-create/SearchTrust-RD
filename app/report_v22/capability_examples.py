"""Deterministic synthetic contract samples, with no external IO."""
from copy import deepcopy
from uuid import UUID

from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import CapabilityReport, SOURCE_ORDER


def example_report(mode="acquisition", action_count=1):
    uid = lambda n: str(UUID(int=n))
    business = dict(id=uid(3), user_id=uid(1), case_id=uid(2), revision=1,
        site_url="https://clinic.example.invalid/emergency", primary_service="Emergency dental care",
        target_market="Northside", target_pages=["https://clinic.example.invalid/emergency"],
        competitor_sites=["https://other-clinic.example.invalid"], priority=None,
        confirmation_source="seo_confirmation", confirmed_at="2026-08-27T08:00:00Z")
    manifest = dict(schema_version="v22_analysis_input_v1", id=uid(4), job_id=uid(5), user_id=uid(1),
        case_id=uid(2), mode=mode, business=business, evaluated_at="2026-08-29T00:00:00Z",
        ruleset_version="capability-p1-v1", generation_input_checksum="sha256:"+"c"*64, previous_report=None, implementation=None,
        snapshots=[dict(snapshot_id=uid(10+i), case_id=uid(2), source_kind=kind,
            schema_version={"site":"site_inventory_snapshot_v1", "serp":"serp_market_snapshot_v1",
                "competitor":"competitor_collection_snapshot_v1", "public_gbp":"customer_public_gbp_snapshot_v1",
                "gsc":"gsc_sync_v1", "ga4":"ga4_sync_v1"}[kind],
            payload_checksum="sha256:"+str(i+1)*64, fetched_at="2026-08-28T08:00:00Z",
            expires_at="2026-08-30T08:00:00Z", health="healthy", identity_match="matched",
            binding_id=uid(20+i) if kind in {"gsc", "ga4"} else None,
            scope="Confirmed emergency-care page and Northside market")
            for i,kind in enumerate(SOURCE_ORDER[:4 if mode=="acquisition" else 6])])
    if mode in {"after_acquisition", "review"}:
        manifest["previous_report"] = dict(report_id=uid(6), case_id=uid(2),
            report_type="prospect" if mode=="after_acquisition" else "verified_execution",
            version=1, payload_checksum="sha256:"+"a"*64)
    if mode=="review":
        manifest["implementation"] = dict(id=uid(7), revision=1, checksum="sha256:"+"b"*64)
    manifest["input_checksum"] = verified_request_digest(manifest)
    report = dict(schema_version="2.2.2", id=uid(5), report_type="prospect" if mode=="acquisition" else "verified_execution",
        version=1 if manifest["previous_report"] is None else 2, generated_at="2026-08-29T00:10:00Z",
        input_manifest=manifest, title="Review the emergency enquiry route",
        summary="The saved page uses a generic contact label. Confirm the intended enquiry route before changing it.",
        evidence=[dict(id="ev-contact", snapshot_id=uid(10), source_kind="site",
            object="https://clinic.example.invalid/emergency", field_path="/contact/label",
            observed_at="2026-08-28T08:00:00Z", value="Contact us", unit=None,
            comparison_scope="Saved emergency-care page", limitations=["Only the saved sample was checked."])],
        findings=[dict(id="fn-contact", rule_id="capability_target.contact_route",rule_version="1.0.0",
            classification="inference",severity="low",confidence="low",target="https://clinic.example.invalid/emergency#contact",
            observed_fact="The saved link label is Contact us.", observed_at="2026-08-28T08:00:00Z",
            comparison_scope="Saved emergency-care page", judgment="The intended route needs confirmation.",
            interpretation="needs_verification", impact_path="A precise instruction could clarify the next contact step.",
            evidence_ids=["ev-contact"], limitations=["No lost enquiries or causal impact is established."])],
        action_state="actions_supported" if action_count else "insufficient_basis",
        actions=[dict(id=f"ac-{i+1}", title=["Confirm the contact route","Clarify the approved contact label","Check the live contact link"][i],
            target="https://clinic.example.invalid/emergency#contact", finding_ids=["fn-contact"],
            steps=["Check the confirmed service and contact destination before making the approved change."],
            completion=["The intended route and the live destination have been checked."],
            review="Compare only eligible evidence for the same page and scope.", dependencies=[] if i==0 else [f"ac-{i}"],
            materials=[dict(id=f"material-{i+1}", kind="service_brief", title="Contact-route editing brief",
                target="https://clinic.example.invalid/emergency#contact",
                body="Confirm the emergency-care contact destination, then write a precise label for that destination. Do not add unsupported opening hours or response-time claims.",
                readiness="requires_confirmation", confirmation_items=["Confirm the intended contact destination."], evidence_ids=["ev-contact"])])
            for i in range(action_count)], limitations=["Synthetic contract sample; no customer outcome is claimed."],
        changes=[], settlement_policy="one_credit_per_completed_analysis")
    return CapabilityReport.model_validate_json(__import__("json").dumps(report)).model_dump(mode="json")


def invalid_examples():
    """Shared full inputs keep FE/BE rejection parity observable."""
    examples = []
    def reject(name, mutate, *, rehash=False):
        report = deepcopy(example_report("first_service"))
        mutate(report)
        if rehash:
            manifest = report["input_manifest"]
            manifest["input_checksum"] = verified_request_digest({k:v for k,v in manifest.items() if k!="input_checksum"})
        examples.append(dict(id=name, report=report))
    reject("unknown_evidence", lambda r:r["findings"][0].update(evidence_ids=["missing"]))
    reject("wrong_snapshot", lambda r:r["evidence"][0].update(snapshot_id=str(UUID(int=99))))
    reject("forged_observation_date", lambda r:r["findings"][0].update(observed_at="2026-08-29T00:00:00Z"))
    reject("wrong_case", lambda r:r["input_manifest"]["snapshots"][0].update(case_id=str(UUID(int=99))), rehash=True)
    reject("expired", lambda r:r["input_manifest"]["snapshots"][0].update(expires_at="2026-08-28T08:00:00Z"), rehash=True)
    reject("missing_ga4", lambda r:r["input_manifest"]["snapshots"].pop(), rehash=True)
    reject("missing_binding", lambda r:r["input_manifest"]["snapshots"][-1].update(binding_id=None), rehash=True)
    reject("mismatched_identity", lambda r:r["input_manifest"]["snapshots"][0].update(identity_match="mismatch"), rehash=True)
    reject("forged_checksum", lambda r:r["input_manifest"].update(input_checksum="sha256:"+"0"*64))
    reject("hidden_parent", lambda r:r["input_manifest"].update(previous_report=dict(report_id=str(UUID(int=6)),
        case_id=str(UUID(int=2)), report_type="prospect", version=1,payload_checksum="sha256:"+"a"*64)), rehash=True)
    reject("zero_state_with_action", lambda r:r.update(action_state="insufficient_basis"))
    reject("duplicate_action", lambda r:r["actions"].append(deepcopy(r["actions"][0])))
    reject("dependency_cycle", lambda r:r["actions"][0].update(dependencies=[r["actions"][0]["id"]]))
    reject("invented_action_finding", lambda r:r["actions"][0].update(finding_ids=["missing"]))
    reject("unchanged_badge", lambda r:r["changes"].append(dict(finding_id="fn-contact",previous_fingerprint="sha256:"+"a"*64,
        current_fingerprint="sha256:"+"a"*64,reason="Refreshed")))
    reject("technical_failure_as_report", lambda r:r.update(settlement_policy="technical_failure_charge"))
    reject("invented_metric", lambda r:r["evidence"][0].update(secret_metric=100))
    reject("four_actions", lambda r:r.update(actions=[{**r["actions"][0],"id":f"ac-{n}"} for n in range(4)]))
    reject("blank_copy", lambda r:r.update(summary=" "))
    reject("wrong_source_schema", lambda r:r["input_manifest"]["snapshots"][0].update(schema_version="invented"), rehash=True)
    reject("noncanonical_uuid", lambda r:r["input_manifest"]["business"].update(id="ABCDEFAB-0000-0000-0000-000000000003"), rehash=True)
    reject("invented_material_evidence", lambda r:r["actions"][0]["materials"][0].update(evidence_ids=["missing"]))
    reject("unconfirmed_publish_ready", lambda r:r["actions"][0]["materials"][0].update(readiness="ready_to_use"))
    reject("unnamed_confirmation", lambda r:r["actions"][0]["materials"][0].update(confirmation_items=[]))
    reject("duplicate_material", lambda r:r["actions"][0]["materials"].append(deepcopy(r["actions"][0]["materials"][0])))
    return examples


def microsecond_example():
    report=deepcopy(example_report("first_service",0))
    m=report["input_manifest"]
    m["evaluated_at"]="2026-08-29T00:00:00.000000Z"
    for source in m["snapshots"]:
        source["fetched_at"]="2026-08-28T08:00:00.123456Z"
        source["expires_at"]="2026-08-29T00:00:00.000001Z"
    report["evidence"][0].update(observed_at="2026-08-28T08:00:00.123456Z", value=" Contact us ")
    report["findings"][0]["observed_at"]="2026-08-28T08:00:00.123456Z"
    m["input_checksum"]=verified_request_digest({k:v for k,v in m.items() if k!="input_checksum"})
    return CapabilityReport.model_validate_json(__import__("json").dumps(report)).model_dump(mode="json")
