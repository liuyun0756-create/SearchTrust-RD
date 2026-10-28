from uuid import uuid4
import json

import pytest

from app.report_v22.page_implementation_checks import prepare_page_check, compare_page_observations, checksum_model
from app.report_v22.page_implementation_models import PageCondition, PageCheckPlan, PageObservation, page_url
from test_v22_implementation_records import context, resign


def prepared(**overrides):
    record, source = context()
    args = dict(implementation=record, business=source["report"].input_manifest.business,
        action_id="ac-1", target_url="https://clinic.example.invalid/emergency#contact",
        conditions=(PageCondition(id="service", kind="text_present", value="Emergency dental care"),
                    PageCondition(id="contact", kind="link_present", value="https://clinic.example.invalid/contact"),
                    PageCondition(id="anchor", kind="anchor_present", value="contact")), **source)
    args.update(overrides)
    return prepare_page_check(**args)


def observation(*, plan=None, states=("observed", "not_observed", "observed"), failed=False,
                at="2026-10-23T01:00:00Z"):
    plan = plan or prepared()
    return checksum_model(PageObservation, dict(
        schema_version="v22_page_implementation_observation_v1", id=str(uuid4()),
        plan=plan.model_dump(mode="json"), observed_at=at,
        status="unable_to_check" if failed else "checked", failure="http_status" if failed else None,
        failure_stage="page" if failed else None, http_status=403 if failed else 200,
        media_type=None if failed else "text/html", response_bytes=10,
        content_checksum=None if failed else "sha256:"+"1"*64,
        results=[dict(condition_id=c.id, state="unable_to_check" if failed else s)
                 for c, s in zip(plan.conditions, states, strict=True)],
        limitation="static_response_only_rendering_functionality_and_identity_not_verified",
        effect_conclusion=None, causal_eligible=False,
    ))


def test_exact_source_binding_and_no_inference_from_scope():
    record, source = context()
    record["entries"][0]["scope"] = "Not a URL; user-reported work only"
    plan = prepared(implementation=resign(record))
    assert plan.binding.implementation_checksum == record["checksum"]
    assert plan.target_url.endswith("/emergency#contact")
    assert plan.binding.site_url == "https://clinic.example.invalid/"
    assert plan.binding.report_id == source["report"].id
    with pytest.raises(ValueError): prepared(target_url=record["entries"][0]["scope"])


@pytest.mark.parametrize("field", ["user_id", "case_id", "report_id", "delivery_revision_id", "report_checksum"])
def test_wrong_implementation_source_rejected(field):
    record, _ = context()
    record[field] = "sha256:"+"f"*64 if field.endswith("checksum") else str(uuid4())
    with pytest.raises(ValueError): prepared(implementation=resign(record))


def test_action_scope_and_approval_cannot_be_substituted():
    _, source = context()
    with pytest.raises(ValueError): prepared(action_id="not-implemented")
    with pytest.raises(ValueError): prepared(business=source["report"].input_manifest.business.model_copy(update={"revision": 2}))
    with pytest.raises(ValueError): prepared(delivery_content=source["delivery_content"].model_copy(update={"status": "draft"}))


@pytest.mark.parametrize("url", [
    "http://clinic.example.invalid/emergency", "https://clinic.example.invalid:80/emergency",
    "https://other.example.invalid/", "https://www.clinic.example.invalid/", "https://sub.clinic.example.invalid/",
    "https://localhost/", "https://127.0.0.1/", "https://169.254.169.254/", "https://[::1]/",
    "https://u:p@clinic.example.invalid/", "https://clinic.example.invalid/?token=secret",
    "https://clinic.example.invalid/?", "https://clinic.example.invalid/a/../emergency",
    "https://clinic.example.invalid/%2e%2e/emergency", "https://clinic.example.invalid/a%00",
    "https://clinic.example.invalid/a\\b", "https://clinic.example.invalid/a%5cb",
    "https://clinic.example.invalid/%oops", "https://clinic.example.invalid/a b",
    "https://clinic.example.invalid/\n", "file:///etc/passwd", "just a scope",
])
def test_unsupported_targets_never_create_a_plan(url):
    with pytest.raises(ValueError): prepared(target_url=url)


def test_path_identity_is_not_collapsed():
    assert page_url("https://clinic.example.invalid/a/") != page_url("https://clinic.example.invalid/a")
    assert page_url("https://clinic.example.invalid/index.html").endswith("/index.html")
    assert page_url("https://CLINIC.example.invalid:443/a#contact") == "https://clinic.example.invalid/a#contact"


@pytest.mark.parametrize("kind,value", [
    ("text_present", "x"*201), ("anchor_present", "x"*129), ("text_present", "x\x00y"),
    ("text_present", "  "), ("text_present", "x\u200by"), ("link_present", "/contact"),
    ("link_present", "javascript:alert(1)"), ("link_present", "https://clinic.example.invalid/?a=b"),
])
def test_condition_limits(kind, value):
    with pytest.raises(ValueError): PageCondition(id="x", kind=kind, value=value)


def test_duplicate_too_many_empty_and_external_conditions():
    one = PageCondition(id="x", kind="text_present", value="hello")
    for conditions in ((), (one, one), tuple(one.model_copy(update={"id": str(i)}) for i in range(6)),
                       (PageCondition(id="x", kind="link_present", value="https://external.invalid/contact"),)):
        with pytest.raises(ValueError): prepared(conditions=conditions)


def test_plan_and_observation_are_frozen_and_hash_checked():
    plan = prepared()
    with pytest.raises(ValueError): plan.target_url = "https://clinic.example.invalid/"
    with pytest.raises(ValueError): plan.conditions[0].value = "different"
    data = plan.model_dump(mode="json")
    data["binding"]["action_id"] = "other"
    with pytest.raises(ValueError): PageCheckPlan.model_validate_json(json.dumps(data))
    value = observation().model_dump(mode="json")
    value["results"][0]["state"] = "not_observed"
    with pytest.raises(ValueError): PageObservation.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("edit", [
    {"http_status": 403}, {"content_checksum": None}, {"response_bytes": 0},
    {"failure": "http_error"}, {"causal_eligible": True}, {"effect_conclusion": "improved"},
    {"observed_at": "2000-01-01T00:00:00Z"},
])
def test_resigning_cannot_turn_invalid_semantics_into_evidence(edit):
    value = observation().model_dump(mode="json", exclude={"checksum"})
    value.update(edit)
    with pytest.raises(ValueError): checksum_model(PageObservation, value)


def test_first_observation_and_three_literal_changes_no_effect():
    before = observation(at="2026-10-23T01:00:00Z")
    after = observation(states=("not_observed", "observed", "observed"), at="2026-10-23T02:00:00Z")
    first = compare_page_observations(after, None)
    assert first.status == "first_observation" and not first.changes
    diff = compare_page_observations(after, before.model_dump(mode="json"))
    assert diff.status == "compared"
    assert [c.change for c in diff.changes] == ["no_longer_observed", "newly_observed", "unchanged"]
    assert diff.effect_conclusion is None and diff.causal_eligible is False
    assert before.results[0].state == "observed"


@pytest.mark.parametrize("mode,reason", [
    ("invalid_hash", "invalid_baseline"), ("legacy", "invalid_baseline"),
    ("same_time", "invalid_time_order"), ("later", "invalid_time_order"),
    ("different_target", "different_plan"), ("different_conditions", "different_plan"),
    ("revised_record", "different_plan"), ("failed", "check_unavailable"),
])
def test_incompatible_baselines_never_manufacture_changes(mode, reason):
    current = observation(at="2026-10-23T02:00:00Z")
    old = observation().model_dump(mode="json")
    if mode == "invalid_hash": old["content_checksum"] = "sha256:"+"f"*64
    elif mode == "legacy": old = {"schema_version": "site_inventory_snapshot_v1"}
    elif mode == "same_time": old = observation(at=current.observed_at).model_dump(mode="json")
    elif mode == "later": old = observation(at="2026-10-23T03:00:00Z").model_dump(mode="json")
    elif mode == "different_target": old = observation(plan=prepared(target_url="https://clinic.example.invalid/other")).model_dump(mode="json")
    elif mode == "different_conditions":
        cs = list(prepared().conditions)
        cs[0] = PageCondition(id="service", kind="text_present", value="Different scope")
        old = observation(plan=prepared(conditions=tuple(cs))).model_dump(mode="json")
    elif mode == "revised_record":
        record, _ = context()
        record["revision"] = 2
        old = observation(plan=prepared(implementation=resign(record))).model_dump(mode="json")
    elif mode == "failed": old = observation(failed=True).model_dump(mode="json")
    result = compare_page_observations(current, old)
    assert result.status == "not_comparable" and result.reason == reason and not result.changes


def test_changed_full_html_digest_does_not_claim_changed_selected_content():
    old = observation()
    value = observation(at="2026-10-23T02:00:00Z").model_dump(mode="json", exclude={"checksum"})
    value["content_checksum"] = "sha256:"+"2"*64
    current = checksum_model(PageObservation, value)
    assert all(c.change == "unchanged" for c in compare_page_observations(current, old.model_dump(mode="json")).changes)


def test_comparison_contract_rejects_impossible_states_and_exports_schema():
    from app.report_v22.page_implementation_models import PageObservationComparison
    assert PageObservation.model_json_schema()["properties"]["schema_version"]["const"] == "v22_page_implementation_observation_v1"
    for changes in ([], [{"condition_id": "x", "change": "newly_observed"}]):
        with pytest.raises(ValueError):
            PageObservationComparison.model_validate_json(json.dumps(dict(status="compared", reason=None,
                previous_id=None, current_id=str(uuid4()), changes=changes)))


@pytest.mark.parametrize("field", [
    "user_id", "case_id", "report_id", "report_checksum", "delivery_revision_id", "delivery_revision",
    "implementation_id", "implementation_revision", "implementation_checksum", "business_id",
    "business_revision", "business_checksum",
])
def test_comparison_cannot_mix_other_ownership_or_revision(field):
    raw = prepared().model_dump(mode="json", exclude={"checksum"})
    raw["binding"][field] = (2 if field.endswith("revision") else "sha256:"+"f"*64
                              if field.endswith("checksum") else str(uuid4()))
    older = observation(plan=checksum_model(PageCheckPlan, raw))
    current = observation(at="2026-10-23T02:00:00Z")
    diff = compare_page_observations(current, older.model_dump(mode="json"))
    assert diff.reason == "different_plan" and not diff.changes


def test_failed_current_and_reused_identity_do_not_make_a_comparison():
    old = observation()
    current = observation(failed=True, at="2026-10-23T02:00:00Z")
    assert compare_page_observations(current, old.model_dump(mode="json")).reason == "check_unavailable"
    raw = observation(at="2026-10-23T02:00:00Z").model_dump(mode="json", exclude={"checksum"})
    raw["id"] = str(old.id)
    current = checksum_model(PageObservation, raw)
    assert compare_page_observations(current, old.model_dump(mode="json")).reason == "invalid_time_order"
