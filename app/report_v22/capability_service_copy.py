"""Bounded interpretation and materials for actual private-source findings.

Never treat a session, click or configured event as a qualified lead. This is
not business-value scoring or automatic content publishing.
"""
from app.report_v22 import first_party_ga4_findings as ga4, first_party_gsc_findings as gsc
from app.report_v22 import cross_source_findings as cross

CAUSAL_LIMIT = "Search clicks, Analytics sessions and configured key events measure different things; these observations do not establish qualified leads, revenue or the cause of a change."


def interpretation(finding, evaluation):
    measurement = evaluation.finding_kind == "measurement" or evaluation.target.key == "(not set)"
    conflict = "CONFLICT" in finding.rule_id
    if measurement:
        return ("Check the source definitions and measurement scope before changing page content.",
            "An unresolved source or event definition can make a client decision unreliable; this does not establish lost enquiries.",
            "conflicting" if conflict else "needs_verification")
    if finding.rule_id in {gsc.PAGE_OPPORTUNITY, gsc.QUERY_OPPORTUNITY, cross.PAGE_OPPORTUNITY}:
        return ("Review the matching search intent, page and current offer before agreeing an improvement.",
            "Relevant search exposure can justify reviewing the current page, not a promise that an edit will increase rankings or enquiries.", "supported")
    if finding.rule_id in {ga4.ENGAGEMENT_GAP, ga4.CONVERSION_GAP}:
        return ("Verify the landing-page path and event meaning before deciding whether to change this page.",
            "A difference in measured behaviour is a reason to investigate the page and measurement together, not proof that the copy caused it.", "needs_verification")
    return ("Keep this observation in view and investigate comparable scope before revising the plan.",
        "Observed movement is not by itself a recommendation or evidence that earlier SEO work caused the change.", "supported")


def action_content(finding, evaluation):
    """Return only work justified by a supported rule; growth is not filler work."""
    if evaluation.finding_kind == "measurement":
        return dict(kind="measurement_check", title="Verify the measurement basis", readiness="requires_confirmation",
            instruction="Check the named source, resource, date window and page or event definition. Reproduce the observation in the connected account. Confirm configured key-event meaning and collection before interpreting it as an enquiry; document a correction only if the check supports one.",
            confirmations=["Confirm the intended page and configured event meaning with the client."],
            completion=["The intended source scope and event meaning are documented.", "The specific measurement issue was reproduced, corrected if confirmed, and checked again."],
            review="Recheck the same resource, page and comparable windows after the measurement check; do not compare unlike metrics as if they were the same conversion.")
    if evaluation.target.key == "(not set)":
        # No live landing page is identified. The dedicated configuration
        # finding above supports the check; do not propose opening a fake page.
        return None
    if finding.rule_id in {gsc.PAGE_OPPORTUNITY, cross.PAGE_OPPORTUNITY}:
        return dict(kind="service_brief", title="Review the existing service page", readiness="requires_confirmation",
            instruction="Review the existing target page against the named search observation. Confirm that this service is a business priority and that the page is the intended target. Prepare approved service scope, process, supporting proof and a clear contact step. Improve that page only where its inspected content and confirmed business details support the change; do not infer missing copy from performance data alone.",
            confirmations=["Confirm the service priority and intended existing page.", "Inspect the page and obtain approved business details before drafting publishable copy."],
            completion=["The target page and business priority are confirmed.", "A page-specific editing brief or a documented no-change decision is ready for the SEO to execute."],
            review="Record the actual edit and date if implemented, then compare eligible evidence for the same page and scope without claiming causality.")
    if finding.rule_id == gsc.QUERY_OPPORTUNITY:
        return dict(kind="technical_check", title="Match the query to the intended page", readiness="instructions",
            instruction="Review the named query and its date window in Search Console. Check its intent and the page currently serving that intent before proposing a page change. A query-level observation alone does not identify a landing page or justify building a new one.",
            confirmations=[], completion=["The query intent and matching page are checked, or the unresolved page association is documented."],
            review="Compare the same query and eligible scope on the next review; record unresolved page association rather than inventing one.")
    if finding.rule_id in {ga4.ENGAGEMENT_GAP, ga4.CONVERSION_GAP}:
        return dict(kind="measurement_check", title="Check the landing-page contact journey", readiness="requires_confirmation",
            instruction="Open the named landing page, test the intended contact journey and verify what the configured key event records. Check matching search evidence and available page content before deciding on a content change. Low recorded event activity does not establish low lead quality or prove a page problem.",
            confirmations=["Confirm the intended contact journey and the meaning of the recorded key event."],
            completion=["The live contact journey and event collection have been checked.", "A supported page or measurement correction, or a no-change decision, is documented."],
            review="Revisit the same landing page and event scope using comparable periods; keep traffic, tracking and concurrent changes visible.")
    return None
