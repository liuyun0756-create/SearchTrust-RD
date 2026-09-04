"""Deterministic assembly of public pipeline outputs into report_v2_2."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from app.api.v2.models import AnalyzeRequest
from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
from app.jobs_v22.digest import request_digest
from app.report_v22.action_models import ActionSkeleton, ActionTarget, PublicActionPlan
from app.report_v22.copy_models import PublicActionCopyResult
from app.report_v22.findings_models import PublicFindingsResult
from app.report_v22.models import (
    ClientSummary,
    CompetitorAnalysis,
    CompetitorSummary,
    DataCoverage,
    ExecutiveDecision,
    FirstPartyPerformance,
    GA4Performance,
    GBPPerformance,
    GSCPerformance,
    IdentitySection,
    LabelCount,
    Limitation,
    MarketSnapshot,
    ReportV22,
    ReportVersion,
    Roadmap,
    RoadmapPhase,
    SearchResult,
    SiteInventorySummary,
    SitePageSummary,
    SourceCoverage,
    TopAction,
    ValidationMetric,
    VersionDiff,
)


_SOURCE_ORDER = ("site", "serp", "competitor", "gbp", "gsc", "ga4")
_ROADMAP_PERIODS = ("days_1_30", "days_31_60", "days_61_90")
_PROSPECT_LIMITATION = (
    "This prospect report uses public data only; authorized GSC, GBP Performance, "
    "and GA4 data are not connected."
)


def _not_connected(source_type: str):
    model = {"gsc": GSCPerformance, "gbp": GBPPerformance, "ga4": GA4Performance}[
        source_type
    ]
    return model(
        connection_state="not_connected",
        snapshot_id=None,
        identity_match_status="not_checked",
        health_status="not_checked",
        coverage_start=None,
        coverage_end=None,
        metrics=[],
        health_reasons=["Client data has not been connected."],
        limitations=[_PROSPECT_LIMITATION],
    )


def _target_text(target: ActionTarget) -> str:
    if target.kind in {"url", "site"}:
        return str(target.url)
    if target.kind == "query":
        return target.query or ""
    if target.kind == "page_type":
        return target.page_type or ""
    return f"Google Business Profile field: {target.gbp_field}"


def _top_action(action: ActionSkeleton, copy) -> TopAction:
    return TopAction(
        action_id=action.action_id,
        sequence=action.sequence,
        finding_ids=action.finding_ids,
        why_now=copy.why_now,
        exact_targets=[_target_text(target) for target in action.exact_targets],
        implementation_steps=action.implementation_steps,
        specification=action.specification,
        required_client_assets=action.required_client_assets,
        dependencies=action.dependencies,
        owner_suggestion=action.owner_suggestion,
        effort_bucket=action.effort_bucket,
        definition_of_done=action.definition_of_done,
        validation_metrics=[
            ValidationMetric(
                metric_key=metric.metric_key,
                baseline=metric.baseline,
                success_condition=metric.success_condition,
                source_type=metric.source_types[0],
            )
            for metric in action.validation_metrics
        ],
        data_sources=action.data_sources,
        review_date=action.review_date,
        client_facing_explanation=copy.client_facing_explanation,
    )


def _coverage(findings: PublicFindingsResult) -> DataCoverage:
    summaries = {item.source_type: item for item in findings.evidence_result.source_summaries}
    sources = []
    for source_type in _SOURCE_ORDER:
        summary = summaries.get(source_type)
        if summary is None:
            sources.append(
                SourceCoverage(
                    source_type=source_type,
                    health_status="not_checked",
                    identity_match_status="not_checked",
                    snapshot_ids=[],
                    checked_items=0,
                    available_items=0,
                    coverage_summary=f"{source_type.upper()} data was not available for this prospect report.",
                    limitations=[_PROSPECT_LIMITATION]
                    if source_type in {"gsc", "gbp", "ga4"}
                    else [],
                )
            )
            continue
        sources.append(
            SourceCoverage(
                source_type=source_type,
                health_status=summary.health_status,
                identity_match_status=summary.identity_match_status,
                snapshot_ids=[summary.snapshot_id],
                checked_items=summary.evidence_count,
                available_items=summary.evidence_count if summary.business_eligible else 0,
                coverage_summary=(
                    f"{summary.evidence_count} traceable {source_type} observations were retained."
                ),
                limitations=summary.limitations,
            )
        )
    return DataCoverage(
        full_evidence_coverage=False,
        sources=sources,
        limitations=[_PROSPECT_LIMITATION],
    )


def _market(
    request: AnalyzeRequest,
    shared: SharedMarketSnapshot,
    findings: PublicFindingsResult,
) -> MarketSnapshot:
    evidence_by_record: dict[tuple[str, str], str] = {}
    for trace in findings.evidence_result.source_traces:
        context = trace.selector.record_context
        if trace.selector.category == "serp_field" and len(context) >= 2:
            evidence_by_record[(str(context[1]), trace.selector.field)] = trace.evidence_id
    results = []
    for run in shared.snapshot.query_runs:
        for item in run.results:
            evidence_id = evidence_by_record.get((item.record_id, "position"))
            if evidence_id is None:
                continue
            results.append(
                SearchResult(
                    query=item.query,
                    result_type=item.result_type,
                    position=item.position,
                    business_name=item.display_name,
                    url=item.url,
                    evidence_id=evidence_id,
                )
            )
    return MarketSnapshot(
        observed_at=shared.snapshot.completed_at,
        target_market=request.target_market,
        queries=request.queries,
        results=results,
        summary=f"{len(results)} traceable search results were retained across the confirmed query set.",
        limitations=list(shared.snapshot.limitations),
    )


def _site(
    inventory: SiteInventorySnapshot,
    findings: PublicFindingsResult,
) -> SiteInventorySummary:
    site_ids_by_url: dict[str, list[str]] = defaultdict(list)
    for item in findings.evidence_result.evidence_index:
        if item.source_type == "site" and item.source_locator.url is not None:
            site_ids_by_url[str(item.source_locator.url)].append(item.evidence_id)
    return SiteInventorySummary(
        discovered_url_count=inventory.discovered_url_count,
        structurally_checked_count=inventory.structurally_checked_count,
        deep_analyzed_count=inventory.deep_analyzed_count,
        discovery_limit=inventory.discovery_limit,
        deep_analysis_limit=inventory.deep_analysis_limit,
        page_type_counts=[
            LabelCount(label=item.label, count=item.count) for item in inventory.page_type_counts
        ],
        selected_pages=[
            SitePageSummary(
                url=page.url,
                page_type=page.page_type,
                crawl_depth=page.crawl_depth,
                deep_analyzed=page.deep_analyzed,
                evidence_ids=sorted(site_ids_by_url[str(page.url)]),
            )
            for page in inventory.selected_pages
        ],
        limitations=inventory.limitations,
    )


def _competitors(
    collection: CompetitorCollectionSnapshot,
    findings: PublicFindingsResult,
) -> CompetitorAnalysis:
    evidence_by_competitor: dict[str, list[str]] = defaultdict(list)
    for trace in findings.evidence_result.source_traces:
        if trace.selector.competitor_id is not None:
            evidence_by_competitor[trace.selector.competitor_id].append(trace.evidence_id)
    competitors = []
    for item in collection.competitors:
        competitor_id = item.competitor.competitor_id
        strengths = []
        if item.site_status != "unavailable":
            strengths.append("A traceable public website sample was available.")
        if item.public_gbp_status != "unavailable":
            strengths.append("A traceable public business profile was available.")
        gaps = list(item.limitations)
        if item.site_status == "unavailable":
            gaps.append("The competitor website sample was unavailable.")
        if item.public_gbp_status == "unavailable":
            gaps.append("The competitor public business profile was unavailable.")
        competitors.append(
            CompetitorSummary(
                competitor_id=competitor_id,
                business_name=item.competitor.business_name,
                website_url=item.competitor.website_url,
                public_gbp_url=item.competitor.public_gbp_url,
                query_appearance_count=item.query_appearance_count,
                best_position=item.best_position,
                analyzed_page_count=item.analyzed_page_count,
                strengths=strengths,
                gaps=list(dict.fromkeys(gaps)),
                evidence_ids=sorted(evidence_by_competitor[competitor_id]),
            )
        )
    return CompetitorAnalysis(
        selection_method="system_ranked_user_confirmed",
        competitors=competitors,
        comparison_summary=(
            f"{len(competitors)} user-confirmed competitors were compared using the saved market context."
        ),
        limitations=list(collection.limitations),
    )


def _limitations(
    site: SiteInventorySnapshot,
    shared: SharedMarketSnapshot,
    collection: CompetitorCollectionSnapshot,
    findings: PublicFindingsResult,
) -> list[Limitation]:
    descriptions = [_PROSPECT_LIMITATION]
    descriptions.extend(site.limitations)
    descriptions.extend(shared.snapshot.limitations)
    descriptions.extend(collection.limitations)
    for summary in findings.evidence_result.source_summaries:
        descriptions.extend(summary.limitations)
    unique = list(dict.fromkeys(str(item) for item in descriptions if str(item).strip()))
    return [
        Limitation(
            limitation_id=f"lim_{request_digest(description)[7:23]}",
            category="coverage",
            severity="medium",
            description=description,
            affected_sections=["data_coverage"],
        )
        for description in unique
    ]


def assemble_prospect_report(
    *,
    report_id: UUID,
    request: AnalyzeRequest,
    site_inventory: SiteInventorySnapshot,
    shared_market: SharedMarketSnapshot,
    competitor_collection: CompetitorCollectionSnapshot,
    findings: PublicFindingsResult,
    action_plan: PublicActionPlan,
    action_copy: PublicActionCopyResult,
    generated_at: datetime,
    copy_model_version: str,
) -> ReportV22:
    """Combine already-validated deterministic stages without inventing new facts."""

    copy_by_id = {item.action_id: item for item in action_copy.actions}
    top_actions = [
        _top_action(action, copy_by_id[action.action_id]) for action in action_plan.actions
    ]
    finding_by_id = {item.finding_id: item for item in findings.findings}
    lead_action = top_actions[0]
    lead_finding = finding_by_id[lead_action.finding_ids[0]]
    all_assets = list(
        dict.fromkeys(asset for action in top_actions for asset in action.required_client_assets)
    )

    return ReportV22(
        identity=IdentitySection(case_id=request.case_id, business=request.business_identity),
        case_context={
            "primary_service": request.primary_service,
            "target_market": request.target_market,
            "queries": request.queries,
            "search_language": shared_market.snapshot.language,
            "search_device": shared_market.snapshot.device,
        },
        report_version=ReportVersion(
            schema_version="2.2.0",
            report_id=report_id,
            report_type="prospect",
            version_number=1,
            parent_report_id=None,
            generated_at=generated_at,
            ruleset_version=findings.ruleset_version,
            copy_model_version=copy_model_version,
        ),
        data_coverage=_coverage(findings),
        market_snapshot=_market(request, shared_market, findings),
        site_inventory_summary=_site(site_inventory, findings),
        competitor_analysis=_competitors(competitor_collection, findings),
        first_party_performance=FirstPartyPerformance(
            gsc=_not_connected("gsc"),
            gbp=_not_connected("gbp"),
            ga4=_not_connected("ga4"),
        ),
        executive_decision=ExecutiveDecision(
            core_problem=lead_finding.statement,
            finding_ids=lead_action.finding_ids,
            why_now=lead_action.why_now,
            decision_summary=lead_action.client_facing_explanation,
        ),
        eight_layers=findings.site_rollup.layers,
        findings=findings.findings,
        top_actions=top_actions,
        roadmap_30_60_90=Roadmap(
            phases=[
                RoadmapPhase(
                    period=period,
                    objective=action.client_facing_explanation,
                    action_ids=[action.action_id],
                    exit_criteria=action.definition_of_done,
                )
                for period, action in zip(_ROADMAP_PERIODS, top_actions, strict=True)
            ]
        ),
        client_summary=ClientSummary(
            headline=f"Priority growth plan for {request.business_identity.business_name}",
            core_problem=lead_finding.statement,
            opportunity=lead_action.client_facing_explanation,
            action_ids=[item.action_id for item in top_actions],
            required_client_assets=all_assets,
            next_review_date=top_actions[0].review_date,
        ),
        evidence_index=findings.evidence_result.evidence_index,
        version_diff=VersionDiff(kind="initial", parent_report_id=None, entries=[]),
        limitations=_limitations(
            site_inventory,
            shared_market,
            competitor_collection,
            findings,
        ),
    )
