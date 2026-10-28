"""Pure report content, not a deliverable, authorization endpoint or paid analysis."""
import json

from pydantic import BaseModel

from app.report_v22.review_plan_basis import PlanBasisReport
from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.ga4_session_reading import read_sessions
from app.report_v22.metric_reading import metric_reading
from app.report_v22.page_implementation_checks import compare_page_observations
from app.report_v22.review_evidence_models import MAX_BYTES, ReviewEvidenceBundle
from app.report_v22.review_evidence_validation import source_references
from app.report_v22.review_projection_models import (
    ActionFacts, ClientAction, DecisionSummary, Ga4Facts, GscFacts, PageFacts,
    RecordedFacts, ReviewCheck, ReviewProjection, SeoAction, PriorFinding,
)

CHECK_COPY = {
    "CONFIRM_RECORDED_WORK": "Confirm what was implemented and when. No implementation record was selected for this action.",
    "ESTABLISH_PAGE_CHECK": "No page check was selected. Establish a check for the relevant page conditions.",
    "FIRST_PAGE_BASELINE": "First recorded check. There is no comparable earlier observation.",
    "REVIEW_PAGE_CONDITIONS": "Review the selected page conditions and static-check limitations; this is not a verdict on the whole action.",
    "WAIT_FOR_WINDOW": "The selected observation window was not complete at evaluation. Wait before arranging a new collection.",
    "REVIEW_GSC_LIMITS": "Review the listed search-data limitations. Missing or restricted figures are not zero.",
    "REVIEW_GA4_LIMITS": "Review the listed session-reading limitations. Restricted figures are not an outcome measurement.",
    "NO_SELECTED_METRICS": "No metric observations were selected. No performance change can be described here.",
    "DESCRIPTIVE_COUNTS_ONLY": "Read these figures within their own scopes and dates. They do not establish the effect of this work.",
}
DECISION_COPY = {
    "continue": "SEO-recorded decision: continue the original action.",
    "adjust": "SEO-recorded decision: propose an adjustment. The proposed scope still needs client-delivery review.",
    "observe": "SEO reports the work complete and chooses observation. System verification of completion is not established.",
    "defer": "SEO-recorded decision: defer this action. Review the stated reconsideration condition before delivery.",
    "new": "SEO-recorded proposal for new work. Its scope and basis still need client-delivery review.",
}


def parse_review_bundle(value) -> ReviewEvidenceBundle:
    """Always detach and revalidate. No public route may use this as authentication."""
    try:
        raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes):
            raw = canonical_json_bytes(raw)
        if len(raw) > MAX_BYTES:
            raise DeterministicJobError("V22_REVIEW_TOO_LARGE", "The selected review evidence exceeds the supported size.")
        # Reject duplicate keys instead of silently accepting a replacement value.
        def unique(pairs):
            obj = {}
            for key, item in pairs:
                if key in obj:
                    raise ValueError("duplicate key")
                obj[key] = item
            return obj
        decoded = json.loads(raw, object_pairs_hook=unique)
        return ReviewEvidenceBundle.model_validate_json(canonical_json_bytes(decoded))
    except DeterministicJobError as error:
        if error.error_code == "V22_REVIEW_TOO_LARGE":
            raise
        raise DeterministicJobError("V22_REVIEW_INVALID", "The selected review evidence is inconsistent or unavailable.") from None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise DeterministicJobError("V22_REVIEW_INVALID", "The selected review evidence is inconsistent or unavailable.") from None


def decision_summary(action_id, decision):
    return DecisionSummary(action_id=action_id, decision_state="seo_recorded" if decision else "needs_review",
        disposition=decision.disposition if decision else None,
        text=DECISION_COPY[decision.disposition] if decision else "Next work has not been decided. Review the available evidence and open checks.")


def _facts(action, selection, entry):
    page = gsc = ga4 = None
    codes = []
    if not entry:
        codes.append("CONFIRM_RECORDED_WORK")
    if not selection.page:
        codes.append("ESTABLISH_PAGE_CHECK")
    else:
        p = selection.page
        o = p.current
        compared = compare_page_observations(o, p.previous.model_dump(mode="json") if p.previous else None)
        page = PageFacts(target_url=o.plan.target_url, observed_at=o.observed_at,
            previous_observed_at=p.previous.observed_at if p.previous else None, conditions=list(o.plan.conditions), results=list(o.results),
            status=o.status, failure=o.failure, comparison=compared.status, comparison_reason=compared.reason,
            changes=list(compared.changes), limitation=o.limitation)
        if compared.status == "first_observation":
            codes.append("FIRST_PAGE_BASELINE")
        if compared.status == "not_comparable" or any(r.state != "observed" for r in o.results):
            codes.append("REVIEW_PAGE_CONDITIONS")
    if selection.gsc:
        r = selection.gsc.result
        reading = metric_reading(r)
        gsc = GscFacts(scope=r.scope, windows=r.windows, evaluated_at=r.evaluated_at, state=r.state, reasons=r.reasons,
            metrics=r.metrics, reading=reading.metrics, limitations=r.limitations,
            concurrent_changes=selection.gsc.request.concurrent_changes, effect_eligibility=r.effect_eligibility)
        if r.state == "waiting":
            codes.append("WAIT_FOR_WINDOW")
        if r.state != "comparable" or any(m.state != "comparable" for m in r.metrics):
            codes.append("REVIEW_GSC_LIMITS")
    if selection.ga4:
        reading = read_sessions(selection.ga4)
        ga4 = Ga4Facts.model_validate_json(verified_canonical_json_bytes({k: v for k, v in reading.model_dump(mode="json").items()
            if k in Ga4Facts.model_fields}))
        if "WINDOW_NOT_ELAPSED" in reading.reasons:
            codes.append("WAIT_FOR_WINDOW")
        if reading.state == "withheld":
            codes.append("REVIEW_GA4_LIMITS")
    if not gsc and not ga4:
        codes.append("NO_SELECTED_METRICS")
    if (gsc and gsc.state == "comparable") or (ga4 and ga4.state == "available"):
        codes.append("DESCRIPTIVE_COUNTS_ONLY")
    return ActionFacts(action_id=action.id, title=action.title, target=action.target,
        recorded_work=RecordedFacts(state="seo_recorded" if entry else "not_recorded",
            implemented_on=entry.implemented_on if entry else None, timezone=entry.timezone if entry else None),
        page=page, gsc=gsc, ga4=ga4, checks=[ReviewCheck(code=c, text=CHECK_COPY[c]) for c in dict.fromkeys(codes)])


def build_review_projection(value) -> ReviewProjection:
    b = parse_review_bundle(value)
    entries = {e.action_id: e for e in b.implementation.entries}
    decisions = {d.action_id: d for d in b.decisions}
    actions, clients = [], []
    for action, selection in zip(b.delivery.actions, b.selections, strict=True):
        entry, decision = entries.get(action.id), decisions.get(action.id)
        facts = _facts(action, selection, entry)
        next_decision = decision_summary(action.id, decision)
        actions.append(SeoAction(facts=facts, approved_action=action, implementation_entry=entry,
            source_references=source_references(b, selection), next_decision=next_decision, decision_record=decision))
        clients.append(ClientAction(facts=facts, next_decision=next_decision))
    new = [d for d in b.decisions if d.disposition == "new"]
    raw = dict(schema_version="v22_review_projection_v1", rules_version=b.rules_version, input_checksum=b.checksum,
        evidence_checksum=b.evidence_checksum, evaluated_at=b.evaluated_at, user_id=str(b.user_id), case_id=str(b.case_id),
        report_id=str(b.report.id), report_checksum=b.report_checksum, delivery_id=str(b.delivery_id),
        delivery_revision=b.delivery_revision, delivery_checksum=b.delivery_checksum,
        prior_diagnosis=b.report.prior_diagnosis.model_dump(mode="json") if isinstance(b.report,PlanBasisReport) else dict(source="prior_report_not_rechecked", title=b.delivery.title, summary=b.delivery.summary,
            action_state=b.report.action_state, limitations=b.report.limitations,
            findings=[{k: v for k, v in f.model_dump(mode="json").items() if k in PriorFinding.model_fields} for f in b.report.findings]),
        seo_actions=[a.model_dump(mode="json") for a in actions], seo_new_decisions=[d.model_dump(mode="json") for d in new],
        client_actions=[c.model_dump(mode="json") for c in clients], client_new_decisions=[decision_summary(d.action_id, d).model_dump(mode="json") for d in new],
        effect=None, causal_attribution=False)
    # Independent decoded trees prevent changes to a private layer mutating a client layer.
    return ReviewProjection.model_validate_json(verified_canonical_json_bytes(raw | {"checksum": verified_request_digest(raw)}))
