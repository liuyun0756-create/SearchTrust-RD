"""Pure preparation and comparison; trusted source loading belongs to the caller."""
from __future__ import annotations

from app.report_v22.review_plan_basis import plan_business
from app.jobs_v22.digest import verified_canonical_json_bytes, verified_request_digest
from app.report_v22.capability import BusinessScope, instant
from app.report_v22.implementation_records import parse_implementation_record
from app.report_v22.page_implementation_models import (
    PageCheckPlan, PageCondition, PageObservation, PageObservationComparison, page_url,
)
from app.security_v22.urls import normalize_site_url


def checksum_model(model, payload):
    """Integrity checksum, not an authorization token or cryptographic signature."""
    payload = {**payload, "checksum": verified_request_digest(payload)}
    return model.model_validate_json(verified_canonical_json_bytes(payload))


def prepare_page_check(*, implementation: dict, business: BusinessScope, action_id: str,
                       target_url: str, conditions: tuple[PageCondition, ...], **source) -> PageCheckPlan:
    record = parse_implementation_record(implementation, **source)
    business = BusinessScope.model_validate(business)
    report_business = plan_business(source["report"])
    if (business.user_id != record.user_id or business.case_id != record.case_id
            or verified_request_digest(business.model_dump(mode="json"))
            != verified_request_digest(report_business.model_dump(mode="json"))):
        raise ValueError("business scope must match the approved report")
    if action_id not in {entry.action_id for entry in record.entries}:
        raise ValueError("action missing from implementation record")
    target = page_url(target_url, site=business.site_url)
    return checksum_model(PageCheckPlan, dict(
        schema_version="v22_page_implementation_plan_v1", rules_version="v22_static_page_check_v1",
        binding=dict(user_id=str(record.user_id), case_id=str(record.case_id), report_id=str(record.report_id),
            report_checksum=record.report_checksum, delivery_revision_id=str(record.delivery_revision_id),
            delivery_revision=record.delivery_revision, implementation_id=str(record.id),
            implementation_revision=record.revision, implementation_checksum=record.checksum,
            implementation_recorded_at=record.recorded_at, action_id=action_id, business_id=str(business.id),
            business_revision=business.revision, business_checksum=verified_request_digest(business.model_dump(mode="json")),
            site_url=normalize_site_url(page_url(business.site_url))),
        target_url=target, conditions=[c.model_dump(mode="json") for c in conditions],
    ))


def checked_plan(plan: PageCheckPlan) -> PageCheckPlan:
    # Revalidate even model_copy/model_construct inputs at the execution boundary.
    return PageCheckPlan.model_validate_json(plan.model_dump_json())


def compare_page_observations(current: PageObservation, previous: dict | None) -> PageObservationComparison:
    current = PageObservation.model_validate_json(current.model_dump_json())
    payload = dict(current_id=current.id, previous_id=None, changes=())
    if previous is None:
        return PageObservationComparison(status="first_observation", reason="no_baseline", **payload)
    try:
        old = PageObservation.model_validate_json(verified_canonical_json_bytes(previous))
    except (ValueError, TypeError):
        return PageObservationComparison(status="not_comparable", reason="invalid_baseline", **payload)
    payload["previous_id"] = old.id
    reason = None
    if current.plan != old.plan:
        reason = "different_plan"
    elif current.id == old.id or instant(current.observed_at) <= instant(old.observed_at):
        reason = "invalid_time_order"
    elif current.status != "checked" or old.status != "checked":
        reason = "check_unavailable"
    if reason:
        return PageObservationComparison(status="not_comparable", reason=reason, **payload)
    payload["changes"] = [dict(condition_id=new.condition_id, change=(
        "unchanged" if new.state == before.state else
        "newly_observed" if new.state == "observed" else "no_longer_observed"
    )) for new, before in zip(current.results, old.results, strict=True)]
    return PageObservationComparison.model_validate_json(verified_canonical_json_bytes(dict(status="compared", reason=None,
        **{**payload, "current_id": str(current.id), "previous_id": str(old.id)})))
