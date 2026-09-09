"""Deterministic final ReportV22 assembly for V22-074."""

from __future__ import annotations

from typing import Any

from app.jobs_v22.digest import request_digest
from app.report_v22.execution_plan_catalog import (
    MEASUREMENT_DONE,
    MEASUREMENT_OWNERS,
    MEASUREMENT_PRESENTATION,
    PUBLIC_PRESENTATION,
)
from app.report_v22.execution_plan_models import ExecutableAction, ExecutionRoadmapPhase
from app.report_v22.models import (
    ActionSpecification,
    ClientSummary,
    DataCoverage,
    ExecutiveDecision,
    FirstPartyPerformance,
    GA4Performance,
    GBPPerformance,
    GSCPerformance,
    Limitation,
    MetricValue,
    ReportV22,
    ReportVersion,
    Roadmap,
    RoadmapPhase,
    SourceCoverage,
    TopAction,
    ValidationMetric,
)


def _target_text(target: Any) -> str:
    if target.kind in {"url", "site"}:
        return str(target.url)
    if target.kind == "query":
        return target.query or ""
    if target.kind == "page_type":
        return target.page_type or ""
    return f"Google Business Profile field: {target.gbp_field}"


def _validation_metrics(action: ExecutableAction) -> list[ValidationMetric]:
    metrics = [action.primary_metric]
    if action.guardrail_metric is not None:
        metrics.append(action.guardrail_metric)
    return [
        ValidationMetric(
            metric_key=item.metric_key,
            baseline=item.baseline,
            success_condition=item.success_condition,
            source_type=item.source_types[0],
        )
        for item in metrics
    ]


def _top_actions(
    *, executable: list[ExecutableAction], ranked: list[Any], core_finding_id: str,
) -> list[TopAction]:
    result: list[TopAction] = []
    for planned, source in zip(executable, ranked, strict=True):
        if source.public_action is not None:
            action = source.public_action
            presentation = PUBLIC_PRESENTATION[action.template_key]
            dependencies = [*planned.blocked_by_action_ids, *action.dependencies]
            result.append(TopAction(
                action_id=action.action_id,
                sequence=action.sequence,
                finding_ids=action.finding_ids,
                why_now=presentation.why_now,
                exact_targets=[_target_text(item) for item in action.exact_targets],
                implementation_steps=action.implementation_steps,
                specification=action.specification,
                required_client_assets=action.required_client_assets,
                dependencies=list(dict.fromkeys(dependencies)),
                owner_suggestion=action.owner_suggestion,
                effort_bucket=action.effort_bucket,
                definition_of_done=action.definition_of_done,
                validation_metrics=_validation_metrics(planned),
                data_sources=action.data_sources,
                review_date=action.review_date,
                client_facing_explanation=presentation.explanation,
            ))
            continue

        action = source.measurement_action
        if action is None:
            raise ValueError("ranked action payload is missing")
        presentation = MEASUREMENT_PRESENTATION[action.template_key]
        finding_ids = [item.finding_id for item in action.finding_refs] or [core_finding_id]
        result.append(TopAction(
            action_id=action.action_id,
            sequence=source.sequence,
            finding_ids=sorted(set(finding_ids)),
            why_now=presentation.why_now,
            exact_targets=[f"{item.upper()} measurement readiness" for item in action.source_types],
            implementation_steps=action.implementation_steps,
            specification=ActionSpecification(
                content_requirements=[],
                gbp_requirements=[],
                technical_requirements=[
                    "Use only fresh snapshots bound to the same Case, identity and comparable time basis."
                ],
            ),
            required_client_assets=["Required analytics access and resource confirmation."],
            dependencies=planned.blocked_by_action_ids,
            owner_suggestion=MEASUREMENT_OWNERS[action.template_key],
            effort_bucket="medium",
            definition_of_done=list(MEASUREMENT_DONE[action.template_key]),
            validation_metrics=_validation_metrics(planned),
            data_sources=action.source_types,
            review_date=action.review_date,
            client_facing_explanation=presentation.explanation,
        ))
    return result


def _performance_metrics(
    source_type: str,
    executable: list[ExecutableAction],
    evidence_by_id: dict[str, Any],
    trace_by_id: dict[str, Any],
) -> list[MetricValue]:
    result: list[MetricValue] = []
    seen: set[tuple[str, str]] = set()
    for action in executable:
        metrics = [action.primary_metric]
        if action.guardrail_metric is not None:
            metrics.append(action.guardrail_metric)
        for metric in metrics:
            if metric.baseline_kind != "exact_value" or source_type not in metric.source_types:
                continue
            evidence = [
                evidence_by_id[key] for key in metric.evidence_ids
                if key in evidence_by_id
                and evidence_by_id[key].source_type == source_type
                and isinstance(evidence_by_id[key].normalized_value, (int, float))
                and not isinstance(evidence_by_id[key].normalized_value, bool)
            ]
            if not evidence:
                continue
            first = evidence[0]
            trace = trace_by_id.get(first.evidence_id)
            field = trace.selector.field if trace is not None else metric.metric_key
            key = (metric.metric_key, field)
            if key in seen:
                continue
            seen.add(key)
            comparisons = [
                evidence_by_id[item].normalized_value
                for item in metric.comparator_ids
                if item in evidence_by_id
                and evidence_by_id[item].source_type == source_type
                and isinstance(evidence_by_id[item].normalized_value, (int, float))
                and not isinstance(evidence_by_id[item].normalized_value, bool)
            ]
            result.append(MetricValue(
                metric_key=f"{metric.metric_key}_{len(result) + 1}",
                label=field.replace("_", " ").title(),
                value=float(first.normalized_value),
                unit=trace.selector.unit if trace is not None and trace.selector.unit else "value",
                comparison_value=float(comparisons[0]) if comparisons else None,
            ))
    return result


def _first_party_performance(
    *, snapshots: list[Any], assessments: list[Any], executable: list[ExecutableAction],
    evidence_by_id: dict[str, Any], trace_by_id: dict[str, Any],
) -> FirstPartyPerformance:
    snapshot_by_source = {item.source_type: item for item in snapshots}
    assessment_by_source = {item.source_type: item for item in assessments}

    def envelope(source_type: str):
        model = {"gsc": GSCPerformance, "ga4": GA4Performance, "gbp": GBPPerformance}[source_type]
        snapshot = snapshot_by_source.get(source_type)
        assessment = assessment_by_source[source_type]
        if snapshot is None:
            return model(
                connection_state="not_connected",
                snapshot_id=None,
                identity_match_status="not_checked",
                health_status="not_checked",
                coverage_start=None,
                coverage_end=None,
                metrics=[],
                health_reasons=assessment.reasons,
                limitations=["Official source data is not connected for this verified report."],
            )
        eligible = assessment.state == "eligible_for_business"
        limitations = list(snapshot.normalized_payload.get("limitations", []))
        if not eligible:
            limitations.append("This source is not eligible to support business outcome metrics.")
        return model(
            connection_state="verified" if eligible else "connected",
            snapshot_id=snapshot.snapshot_id,
            identity_match_status=snapshot.identity_match_status,
            health_status=snapshot.health_status,
            coverage_start=snapshot.coverage_start,
            coverage_end=snapshot.coverage_end,
            metrics=[] if source_type == "gbp" else _performance_metrics(
                source_type, executable, evidence_by_id, trace_by_id,
            ),
            health_reasons=snapshot.health_reasons,
            limitations=sorted(set(limitations)),
        )

    return FirstPartyPerformance(gsc=envelope("gsc"), gbp=envelope("gbp"), ga4=envelope("ga4"))


def _coverage(
    parent: ReportV22, performance: FirstPartyPerformance, evidence: list[Any],
) -> DataCoverage:
    by_source = {item.source_type: item for item in parent.data_coverage.sources}
    for item in (performance.gsc, performance.ga4):
        count = sum(
            1 for value in evidence
            if value.source_type == item.source_type and value.snapshot_id == item.snapshot_id
        )
        by_source[item.source_type] = SourceCoverage(
            source_type=item.source_type,
            health_status=item.health_status,
            identity_match_status=item.identity_match_status,
            snapshot_ids=[item.snapshot_id] if item.snapshot_id is not None else [],
            checked_items=count,
            available_items=count if item.connection_state == "verified" else 0,
            coverage_summary=(
                "The bound source is eligible for verified business metrics."
                if item.connection_state == "verified"
                else "The bound source is present but not eligible for business metrics."
            ),
            limitations=item.limitations,
        )
    if performance.gbp.snapshot_id is not None:
        item = performance.gbp
        count = sum(
            1 for value in evidence
            if value.source_type == "gbp" and value.snapshot_id == item.snapshot_id
        )
        by_source["gbp"] = SourceCoverage(
            source_type="gbp",
            health_status=item.health_status,
            identity_match_status=item.identity_match_status,
            snapshot_ids=[item.snapshot_id],
            checked_items=count,
            available_items=count if item.connection_state == "verified" else 0,
            coverage_summary="Official GBP was checked using durable categorical evidence only.",
            limitations=item.limitations,
        )
    full = all(
        item.connection_state == "verified"
        and item.health_status == "healthy"
        and item.identity_match_status == "matched"
        and item.snapshot_id is not None
        for item in (performance.gsc, performance.gbp, performance.ga4)
    )
    return DataCoverage(
        full_evidence_coverage=full,
        sources=[by_source[key] for key in ("site", "serp", "competitor", "gbp", "gsc", "ga4")],
        limitations=sorted(set([
            *parent.data_coverage.limitations,
            *(value for item in (performance.gsc, performance.gbp, performance.ga4) for value in item.limitations),
        ])),
    )


def _limitations(
    parent: ReportV22, performance: FirstPartyPerformance, extras: list[str],
) -> list[Limitation]:
    values = list(parent.limitations)
    existing = {item.description for item in values}
    descriptions = sorted(set([
        *extras,
        *(
        value
        for item in (performance.gsc, performance.gbp, performance.ga4)
        for value in [*item.health_reasons, *item.limitations]
        if value
        ),
    ]))
    for description in descriptions:
        if description in existing:
            continue
        values.append(Limitation(
            limitation_id=f"lim_{request_digest(description)[7:23]}",
            category="coverage",
            severity="medium",
            description=description,
            affected_sections=["data_coverage", "first_party_performance", "top_actions"],
        ))
    return values


def assemble_verified_report(
    *, parent: ReportV22, report_id: Any, evaluated_at: Any, copy_model_version: str,
    verified_input: Any, verified_result: Any, version_diff_result: Any,
    executable: list[ExecutableAction],
    roadmap: list[ExecutionRoadmapPhase], findings: list[Any], evidence: list[Any],
    trace_by_id: dict[str, Any], limitations: list[str],
) -> ReportV22:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    first_party = _first_party_performance(
        snapshots=verified_input.first_party_input.snapshots,
        assessments=verified_input.first_party_result.source_assessments,
        executable=executable,
        evidence_by_id=evidence_by_id,
        trace_by_id=trace_by_id,
    )
    finding_by_id = {item.finding_id: item for item in findings}
    core_id = verified_result.core_problem_finding.finding_id
    core = finding_by_id[core_id]
    top_actions = _top_actions(
        executable=executable,
        ranked=verified_result.actions,
        core_finding_id=core_id,
    )
    business_action = next(item for item in top_actions if core_id in item.finding_ids)
    report = ReportV22(
        identity=parent.identity,
        case_context=parent.case_context,
        report_version=ReportVersion(
            schema_version="2.2.0",
            report_id=report_id,
            report_type="verified_execution",
            version_number=parent.report_version.version_number + 1,
            parent_report_id=parent.report_version.report_id,
            generated_at=evaluated_at,
            ruleset_version="v22_execution_plan_v1",
            copy_model_version=copy_model_version,
        ),
        data_coverage=_coverage(parent, first_party, evidence),
        market_snapshot=parent.market_snapshot,
        site_inventory_summary=parent.site_inventory_summary,
        competitor_analysis=parent.competitor_analysis,
        first_party_performance=first_party,
        executive_decision=ExecutiveDecision(
            core_problem=core.statement,
            finding_ids=business_action.finding_ids,
            why_now=business_action.why_now,
            decision_summary=top_actions[0].client_facing_explanation,
        ),
        eight_layers=parent.eight_layers,
        findings=findings,
        top_actions=top_actions,
        roadmap_30_60_90=Roadmap(phases=[
            RoadmapPhase(
                period=item.period,
                objective=item.objective,
                action_ids=[item.action_id],
                exit_criteria=item.exit_criteria,
            )
            for item in roadmap
        ]),
        client_summary=ClientSummary(
            headline=f"Verified execution plan for {parent.identity.business.business_name}",
            core_problem=core.statement,
            opportunity=top_actions[0].client_facing_explanation,
            action_ids=[item.action_id for item in top_actions],
            required_client_assets=list(dict.fromkeys(
                asset for item in top_actions for asset in item.required_client_assets
            )),
            next_review_date=top_actions[0].review_date,
        ),
        evidence_index=evidence,
        version_diff=version_diff_result.version_diff,
        limitations=_limitations(parent, first_party, limitations),
    )
    return report
