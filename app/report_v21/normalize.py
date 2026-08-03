"""Normalize Dify outputs into the SearchTrust report_v2_1 envelope."""

from __future__ import annotations

import json
import copy
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.report_v21.business_presence import (
    bind_business_presence_evidence,
    build_business_presence_audit,
)
from app.report_v21.coverage import build_data_coverage, build_gbp_alignment, build_gbp_profile, build_gbp_status, build_schema_summary
from app.report_v21.dedupe import dedupe_report_v21
from app.report_v21.gbp_guard import BLOCKED_GBP_CLAIM_PHRASES
from app.report_v21.models import LAYER_DISPLAY_LABELS, LAYER_LABELS, REQUIRED_LAYER_KEYS, ReportV21
from app.report_v21.quality import prune_unsupported_evidence, validate_evidence_quality
from app.report_v21.scoring import apply_deterministic_scoring
from app.report_v21.validate import GOOGLE_CERTAINTY_PHRASES, OLD_LAYER_LABELS, validate_report_v21


STATUS_MAP: dict[str, str] = {
    "良好": "good",
    "一般": "medium",
    "偏弱": "weak",
    "good": "good",
    "medium": "medium",
    "weak": "weak",
    "not_checked": "not_checked",
}

OVERALL_LEVEL_MAP: dict[str, str] = {
    "Weak": "weak",
    "Medium-Low": "medium_weak",
    "Medium": "medium",
    "Strong": "strong",
    "High": "high",
    "偏弱": "weak",
    "中等偏弱": "medium_weak",
    "中等": "medium",
    "良好": "strong",
}

RANKING_LEVEL_MAP: dict[str, str] = {
    "Low": "low",
    "Moderate": "competitive",
    "Moderate-High": "improvable",
    "High": "strong",
    "潜力较低": "low",
    "可参与竞争": "competitive",
    "有提升空间": "improvable",
    "具备较强竞争潜力": "strong",
}

RISK_LEVEL_MAP: dict[str, str] = {
    "Low": "low",
    "Medium": "medium",
    "Medium-High": "medium_high",
    "High": "high",
    "低风险": "low",
    "中风险": "medium",
    "中高风险": "medium_high",
    "高风险": "high",
}

V21_OUTPUT_INVALID_CODE = "V21_OUTPUT_INVALID"
V21_OUTPUT_INVALID_MESSAGE = (
    "The report could not be completed because the analysis output was incomplete. "
    "Please try again."
)


class ReportV21OutputInvalid(RuntimeError):
    """Raised when a new v2.1 run does not contain a valid native Dify report."""

    retryable = True

    def __init__(self, validation_errors: list[str], warnings: list[str] | None = None):
        super().__init__(V21_OUTPUT_INVALID_MESSAGE)
        self.error_code = V21_OUTPUT_INVALID_CODE
        self.user_message = V21_OUTPUT_INVALID_MESSAGE
        self.validation_errors = validation_errors
        self.warnings = warnings or []

    def to_result(self, task_id: str | None = None) -> dict[str, Any]:
        return {
            "status": "failed",
            "error_code": self.error_code,
            "retryable": True,
            "user_message": self.user_message,
            "validation_errors": self.validation_errors,
            "warnings": self.warnings,
            "task_id": task_id,
        }


def parse_json_maybe(value: Any) -> tuple[Any, str | None]:
    """Parse dict/list or JSON strings, returning `(value, warning)`."""
    if isinstance(value, (dict, list)):
        return value, None
    if value is None:
        return None, "value is empty"
    if not isinstance(value, str):
        return None, f"value is not JSON-compatible: {type(value).__name__}"

    raw = value.strip()
    if not raw:
        return None, "value is empty"

    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON string: {exc}"


def extract_report_v21_from_outputs(outputs: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Extract an inner report_v2_1 object from Dify outputs."""
    warnings: list[str] = []
    parsed_outputs, warning = parse_json_maybe(outputs)
    if warning:
        warnings.append(warning)
    if not isinstance(parsed_outputs, dict):
        return None, warnings

    candidate = parsed_outputs.get("report_v2_1")
    if candidate is None:
        if _looks_like_inner_report(parsed_outputs):
            candidate = parsed_outputs
        else:
            return None, warnings

    parsed_candidate, warning = parse_json_maybe(candidate)
    if warning:
        warnings.append(f"report_v2_1 parse warning: {warning}")
        return None, warnings

    if isinstance(parsed_candidate, dict) and isinstance(parsed_candidate.get("report_v2_1"), dict):
        parsed_candidate = parsed_candidate["report_v2_1"]

    if isinstance(parsed_candidate, dict) and _looks_like_inner_report(parsed_candidate):
        return parsed_candidate, warnings

    warnings.append("report_v2_1 candidate is not a v2.1 report object")
    return None, warnings


def normalize_report_to_v21(outputs: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize Dify outputs into `{report_v2_1: ...}` without mutating legacy fields."""
    context = context or {}
    warnings: list[str] = []

    native_report, extract_warnings = extract_report_v21_from_outputs(outputs)
    warnings.extend(extract_warnings)
    if native_report is not None:
        try:
            report = _finalize_report(native_report, context, warnings)
            return {"report_v2_1": report}
        except ValidationError as exc:
            warnings.append(f"native report_v2_1 failed Pydantic validation: {exc.errors()}")

    parsed_outputs, warning = parse_json_maybe(outputs)
    if warning:
        warnings.append(warning)

    if isinstance(parsed_outputs, dict) and "score" in parsed_outputs:
        report = legacy_score_to_report_v21(parsed_outputs, context, warnings)
        return {"report_v2_1": report}

    fallback = _safe_fallback_report(context, warnings + ["No native report_v2_1 or legacy score output was available."])
    return {"report_v2_1": fallback}


def normalize_native_report_to_v21(outputs: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize only native Dify `report_v2_1` output for new v2.1 runs.

    Unlike `normalize_report_to_v21`, this does not adapt legacy `score` output
    and does not synthesize a fallback report. New v2.1 reports must be backed
    by Dify's native structured output.
    """
    context = context or {}
    warnings: list[str] = []

    native_report, extract_warnings = extract_report_v21_from_outputs(outputs)
    warnings.extend(extract_warnings)
    if native_report is None:
        errors = ["Dify output did not include native report_v2_1."]
        if extract_warnings:
            errors.extend(extract_warnings)
        raise ReportV21OutputInvalid(errors, warnings)

    try:
        report = _finalize_report(native_report, context, warnings, run_scoring=True)
    except ValidationError as exc:
        raise ReportV21OutputInvalid(
            [f"Native report_v2_1 failed Pydantic validation: {exc.errors()}"],
            warnings,
        ) from exc

    validation = validate_report_v21(report)
    if not validation.get("valid"):
        errors = [
            str(error)
            for error in validation.get("errors", [])
            if str(error).strip()
        ]
        raise ReportV21OutputInvalid(errors or ["Native report_v2_1 failed validation."], warnings)

    return {"report_v2_1": report}


def normalize_report_copy_to_v21(
    outputs: Any,
    context: dict[str, Any],
    rule_results: dict[int, bool],
    rule_applicability: dict[int, bool],
    evidence_ledger: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a native report from backend facts and Dify narrative copy."""
    from app.report_v21.copy_contract import (  # noqa: PLC0415
        assemble_report_skeleton,
        parse_report_copy,
        validate_english_copy,
    )

    report_copy = parse_report_copy(outputs)
    validate_english_copy(report_copy)
    skeleton = assemble_report_skeleton(
        report_copy,
        rule_results,
        rule_applicability,
        evidence_ledger,
        context,
    )
    try:
        report = _finalize_report(skeleton, context, [], run_scoring=True)
    except ValidationError as exc:
        raise ReportV21OutputInvalid(
            [f"Backend report assembly failed Pydantic validation: {exc.errors()}"],
        ) from exc

    evidence_notes = prune_unsupported_evidence(report, context)
    evidence_notes.extend(validate_evidence_quality(report, context))
    if evidence_notes:
        _append_limitations(
            report,
            [
                "Some supporting evidence was unavailable or could not be traced; "
                "the affected evidence sections were omitted."
            ],
        )
    validation = validate_report_v21(report)
    if not validation.get("valid"):
        raise ReportV21OutputInvalid(
            [str(error) for error in validation.get("errors", []) if str(error).strip()]
            or ["Backend-assembled report_v2_1 failed validation."],
        )
    return {"report_v2_1": report}


def legacy_score_to_report_v21(
    outputs: dict[str, Any],
    context: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Convert the current production legacy score output into a partial v2.1 report."""
    context = context or {}
    warnings = list(warnings or [])
    score, score_warning = parse_json_maybe(outputs.get("score"))
    if score_warning:
        warnings.append(f"legacy score parse warning: {score_warning}")
    if not isinstance(score, dict):
        score = {}
        warnings.append("Legacy score was not an object; generated fallback v2.1 structure.")

    module_1 = _dict(score.get("module_1_overview"))
    module_2 = _dict(score.get("module_2_page_level"))
    module_3 = _dict(score.get("module_3_key_problems"))
    module_4 = _dict(score.get("module_4_eight_layers"))
    module_5 = _dict(score.get("module_5_optimization"))

    warnings.append("Legacy-adapted report_v2_1 may lack structured evidence_items and action_items.")

    report = {
        "schema_version": "2.1",
        "report_id": _context_str(context, "report_id", _report_id_from_context(context)),
        "analyzed_url": _context_str(context, "url", "unknown-url"),
        "page_type": _context_str(context, "page_type", "unknown"),
        "generated_at": _context_str(context, "generated_at", _now_iso()),
        "gbp_status": _gbp_status_from_context(context),
        "data_coverage": _data_coverage_from_context(context, warnings),
        "overall_status": _status_card(
            outputs.get("trust_status"),
            fallback_label="Trust Status",
            fallback_level="medium",
            level_map=OVERALL_LEVEL_MAP,
        ),
        "ranking_potential": _status_card(
            outputs.get("ranking_potential"),
            fallback_label="Ranking Potential",
            fallback_level="competitive",
            level_map=RANKING_LEVEL_MAP,
        ),
        "risk_level": _status_card(
            outputs.get("risk_level"),
            fallback_label="Risk Level",
            fallback_level="medium",
            level_map=RISK_LEVEL_MAP,
        ),
        "primary_blocking_layer": _primary_blocking_layer(module_1, module_3),
        "page_level": _page_level(module_2),
        "layers": _layers_from_legacy(module_4),
        "key_issues": _key_issues_from_legacy(module_3),
        "optimization_path": _optimization_path_from_legacy(module_5),
        "client_summary": _client_summary_from_legacy(module_1),
    }

    return _finalize_report(report, context, warnings)


def _finalize_report(
    report: dict[str, Any],
    context: dict[str, Any],
    warnings: list[str],
    *,
    run_scoring: bool = True,
) -> dict[str, Any]:
    report = _normalize_action_example_copy_fields(report)
    report["schema_version"] = "2.1"
    report["report_id"] = _context_str(context, "report_id", _report_id_from_context(context))
    report["analyzed_url"] = _context_str(context, "url", "unknown-url")
    report["page_type"] = _context_str(context, "page_type", "unknown")
    report["generated_at"] = _context_str(context, "generated_at", _now_iso())
    report["gbp_status"] = build_gbp_status(context)
    report["gbp_profile"] = build_gbp_profile(context)
    report["gbp_alignment"] = build_gbp_alignment(context)
    report["schema_summary"] = build_schema_summary(context)
    report["data_coverage"] = build_data_coverage(context, warnings)
    business_presence_audit = build_business_presence_audit(context)
    report["business_presence_audit"] = business_presence_audit
    report = bind_business_presence_evidence(report, business_presence_audit, context)
    report = _backfill_presentation_fields(report)

    model = ReportV21.model_validate(report)
    model_report = model.model_dump(mode="json", exclude_none=True)
    validation = validate_report_v21(model_report)
    final_report = validation.get("sanitized_report") or model_report

    validation_notes = [*validation.get("warnings", []), *validation.get("errors", [])]
    if validation_notes:
        _append_limitations(final_report, validation_notes)

    pre_dedupe_report = copy.deepcopy(final_report)
    deduped_report, dedupe_warnings = dedupe_report_v21(final_report)
    if dedupe_warnings:
        _append_limitations(deduped_report, dedupe_warnings)

    post_dedupe_validation = validate_report_v21(deduped_report)
    if post_dedupe_validation.get("sanitized_report"):
        deduped_report = post_dedupe_validation["sanitized_report"]

    post_dedupe_notes = [
        *post_dedupe_validation.get("warnings", []),
        *post_dedupe_validation.get("errors", []),
    ]
    if post_dedupe_notes:
        _append_limitations(deduped_report, post_dedupe_notes)

    if not run_scoring:
        final_model = ReportV21.model_validate(deduped_report)
        return final_model.model_dump(mode="json", exclude_none=True)

    pre_scoring_report = copy.deepcopy(deduped_report)
    scored_report, scoring_warnings = apply_deterministic_scoring(deduped_report)
    if scoring_warnings:
        _append_limitations(scored_report, scoring_warnings)

    post_scoring_validation = validate_report_v21(scored_report)
    if post_scoring_validation.get("sanitized_report"):
        scored_report = post_scoring_validation["sanitized_report"]

    post_scoring_notes = [
        *post_scoring_validation.get("warnings", []),
        *post_scoring_validation.get("errors", []),
    ]
    if post_scoring_notes:
        _append_limitations(scored_report, post_scoring_notes)

    try:
        final_model = ReportV21.model_validate(scored_report)
        return final_model.model_dump(mode="json", exclude_none=True)
    except ValidationError:
        fallback_report = pre_scoring_report
        _append_limitations(
            fallback_report,
            ["Deterministic scoring was skipped because structural validation failed after scoring."],
        )
        try:
            fallback_model = ReportV21.model_validate(fallback_report)
            return fallback_model.model_dump(mode="json", exclude_none=True)
        except ValidationError:
            fallback_report = pre_dedupe_report
            _append_limitations(
                fallback_report,
                ["Dedupe pass was skipped because structural validation failed after dedupe."],
            )
            try:
                fallback_model = ReportV21.model_validate(fallback_report)
                return fallback_model.model_dump(mode="json", exclude_none=True)
            except ValidationError:
                return model_report


def _normalize_action_example_copy_fields(value: Any) -> Any:
    """Convert the legacy scalar example_copy form without weakening validation."""
    if isinstance(value, list):
        return [_normalize_action_example_copy_fields(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_action_example_copy_fields(item)
        for key, item in value.items()
    }
    if "example_copy" in normalized and _looks_like_action_item(normalized):
        example_copy = normalized["example_copy"]
        if isinstance(example_copy, str):
            normalized["example_copy"] = [example_copy]
    return normalized


def _looks_like_action_item(value: dict[str, Any]) -> bool:
    return "task_title" in value and "affected_layer" in value


def _safe_fallback_report(context: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    layer_reports = []
    for idx, key in enumerate(REQUIRED_LAYER_KEYS, start=1):
        label = LAYER_LABELS[key]
        layer_reports.append({
            "layer_id": idx,
            "layer_key": key,
            "layer_name": label,
            "layer_label": LAYER_DISPLAY_LABELS[key],
            "status": "not_checked",
            "checked_rule_ids": [],
            "triggered_rule_ids": [],
            "summary": "This layer was not normalized from a supported Dify output.",
            "explanation": "The backend generated a safe fallback because no supported report output was available.",
            "evidence_items": [_not_available_evidence(f"ev-{key}-fallback", "No supported output was available.")],
            "suggested_fixes": [],
            "action_items": [],
        })

    report = {
        "schema_version": "2.1",
        "report_id": _report_id_from_context(context),
        "analyzed_url": _context_str(context, "url", "unknown-url"),
        "page_type": _context_str(context, "page_type", "unknown"),
        "generated_at": _context_str(context, "generated_at", _now_iso()),
        "gbp_status": _gbp_status_from_context(context),
        "data_coverage": _data_coverage_from_context(context, warnings),
        "overall_status": {"label": "Trust Status", "level": "medium", "explanation": "Status could not be normalized from the Dify output."},
        "ranking_potential": {"label": "Ranking Potential", "level": "competitive", "explanation": "Ranking potential could not be normalized from the Dify output."},
        "risk_level": {"label": "Risk Level", "level": "medium", "explanation": "Risk level could not be normalized from the Dify output."},
        "primary_blocking_layer": {
            "layer_key": "foundation",
            "layer_name": LAYER_LABELS["foundation"],
            "reason": "No supported output was available to identify a primary blocking layer.",
            "evidence_items": [_not_available_evidence("ev-primary-fallback", "No supported output was available.")],
        },
        "page_level": {"label": "Unknown", "what_it_looks_like": "The page level could not be normalized.", "strengths": [], "missing_elements": []},
        "layers": layer_reports,
        "key_issues": [],
        "optimization_path": {"must_execute_now": [], "defer_until_later": [], "do_not_prioritize_yet": [], "roadmap": [], "fix_order_warning": "No optimization path could be normalized.", "completion_signals": []},
        "client_summary": {
            "title": "SearchTrust report could not be fully normalized",
            "plain_language_summary": "The backend returned a safe v2.1 fallback because the Dify output did not match a supported shape.",
            "why_it_matters": "The legacy report fields may still be available, but the v2.1 structure is incomplete.",
            "first_priority": "Review the raw Dify output and workflow response shape.",
            "not_first_priority": "Do not treat this fallback as a complete evidence-backed report.",
            "expected_change": "A supported Dify output should produce a richer report_v2_1 object.",
        },
    }
    return _finalize_report(report, context, warnings)


def _layers_from_legacy(module_4: dict[str, Any]) -> list[dict[str, Any]]:
    legacy_layers = module_4.get("layers") if isinstance(module_4.get("layers"), list) else []
    by_key = {
        layer.get("layer_key"): layer
        for layer in legacy_layers
        if isinstance(layer, dict) and layer.get("layer_key") in REQUIRED_LAYER_KEYS
    }
    results: list[dict[str, Any]] = []
    for idx, key in enumerate(REQUIRED_LAYER_KEYS, start=1):
        legacy = by_key.get(key, {})
        label = LAYER_LABELS[key]
        status = STATUS_MAP.get(str(legacy.get("status", "")).strip(), "not_checked")
        description = str(legacy.get("description") or "Legacy output did not provide a layer explanation.")
        evidence = []
        if status in {"medium", "weak", "not_checked"}:
            evidence.append(_not_available_evidence(f"ev-{key}-legacy", "Legacy output did not include structured evidence for this layer."))
        results.append({
            "layer_id": idx,
            "layer_key": key,
            "layer_name": str(legacy.get("layer_name") or label),
            "layer_label": LAYER_DISPLAY_LABELS[key],
            "status": status,
            "checked_rule_ids": [],
            "triggered_rule_ids": [],
            "triggered_findings": [],
            "summary": description,
            "explanation": description,
            "evidence_items": evidence,
            "suggested_fixes": [],
            "action_items": [],
        })
    return results


def _key_issues_from_legacy(module_3: dict[str, Any]) -> list[dict[str, Any]]:
    raw_issues = module_3.get("concrete_issues")
    if not isinstance(raw_issues, list):
        return []

    issues: list[dict[str, Any]] = []
    for index, issue in enumerate(raw_issues, start=1):
        if not isinstance(issue, dict):
            continue
        layer_key = _best_layer_key(issue.get("affected_layer") or issue.get("layer_key") or issue.get("title"))
        title = str(issue.get("title") or issue.get("issue_title") or f"Legacy issue {index}")
        explanation = str(issue.get("explanation") or issue.get("judgement") or "Legacy issue lacks structured explanation.")
        action = _legacy_action(
            action_id=f"act-legacy-{index}",
            layer_key=layer_key,
            title=f"Review: {title}",
            suggestions=issue.get("suggestions"),
        )
        issues.append({
            "id": f"issue-legacy-{index}",
            "issue_title": title,
            "affected_layer": layer_key,
            "related_rule_ids": [],
            "severity": "medium",
            "evidence_items": [_not_available_evidence(f"ev-issue-legacy-{index}", "Legacy issue did not include structured evidence.")],
            "judgement": str(issue.get("judgement") or explanation),
            "explanation": explanation,
            "why_it_matters": _string_summary(issue.get("impacts")) or "This issue may limit the page's trust signals.",
            "impacts": _string_list(issue.get("impacts")),
            "suggestions": _string_list(issue.get("suggestions")),
            "recommended_actions": [action],
        })
    return issues


def _optimization_path_from_legacy(module_5: dict[str, Any]) -> dict[str, Any]:
    must_execute = []
    raw_must = _dict(module_5.get("must_execute_now"))
    raw_items = raw_must.get("items") if isinstance(raw_must.get("items"), list) else []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        must_execute.append(_legacy_action(
            action_id=f"act-must-{index}",
            layer_key=_best_layer_key(item.get("affected_layer") or item.get("title")),
            title=str(item.get("title") or f"Must execute {index}"),
            suggestions=item.get("execution_focus"),
            completion=item.get("completion_signals"),
            expected=str(item.get("expected_impact") or ""),
        ))

    roadmap = []
    raw_roadmap = module_5.get("roadmap") if isinstance(module_5.get("roadmap"), list) else []
    for index, phase in enumerate(raw_roadmap, start=1):
        if not isinstance(phase, dict):
            continue
        roadmap.append({
            "id": f"phase-legacy-{index}",
            "phase_title": str(phase.get("phase_title") or f"Phase {index}"),
            "sequence": index,
            "goal": str(phase.get("goal") or "Legacy roadmap goal was not structured."),
            "entry_condition": str(phase.get("entry_condition") or ""),
            "action_items": [],
            "expected_outcomes": _string_list(phase.get("expected_outcomes")),
        })

    blocker = _dict(module_5.get("primary_trust_blocker"))
    return {
        "must_execute_now": must_execute,
        "defer_until_later": [],
        "do_not_prioritize_yet": [],
        "roadmap": roadmap,
        "fix_order_warning": str(blocker.get("why_cannot_skip") or "Legacy optimization path lacks a structured fix-order warning."),
        "completion_signals": [],
    }


def _primary_blocking_layer(module_1: dict[str, Any], module_3: dict[str, Any]) -> dict[str, Any]:
    raw = (
        module_1.get("primary_blocking_layer")
        or _dict(module_3.get("primary_trust_failure")).get("blocking_layer")
        or "foundation"
    )
    layer_key = _best_layer_key(raw)
    return {
        "layer_key": layer_key,
        "layer_name": LAYER_LABELS[layer_key],
        "reason": str(_dict(module_3.get("primary_trust_failure")).get("description") or "Legacy report identified this as the closest primary blocking layer."),
        "evidence_items": [_not_available_evidence("ev-primary-legacy", "Legacy report did not include structured evidence for the primary blocking layer.")],
    }


def _page_level(module_2: dict[str, Any]) -> dict[str, Any]:
    label = str(module_2.get("level") or module_2.get("label") or "Legacy page level")
    current_assessment = str(
        module_2.get("current_assessment")
        or module_2.get("what_it_means")
        or module_2.get("summary")
        or "Legacy output did not include a v2.1 page-level explanation."
    )
    return {
        "label": label,
        "what_it_looks_like": current_assessment,
        "strengths": _string_list(module_2.get("strengths") or module_2.get("existing_foundation")),
        "missing_elements": _string_list(module_2.get("missing_elements") or module_2.get("main_limitation")),
        "current_assessment": current_assessment,
        "existing_foundation": _string_summary(module_2.get("existing_foundation")),
        "main_limitation": _string_summary(module_2.get("main_limitation")),
        "likely_search_outcome": _string_summary(module_2.get("likely_search_outcome")),
        "competitive_interpretation": _string_summary(module_2.get("competitive_interpretation")),
    }


def _backfill_presentation_fields(report: dict[str, Any]) -> dict[str, Any]:
    page_level = report.get("page_level")
    if isinstance(page_level, dict):
        current = str(page_level.get("current_assessment") or page_level.get("what_it_looks_like") or "").strip()
        page_level.setdefault("current_assessment", current)
        page_level.setdefault("existing_foundation", _string_summary(page_level.get("strengths")))
        page_level.setdefault("main_limitation", _string_summary(page_level.get("missing_elements")))
        page_level.setdefault("likely_search_outcome", "")
        page_level.setdefault("competitive_interpretation", "")

    issues = report.get("key_issues")
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            issue.setdefault("judgement", str(issue.get("explanation") or "").strip())
            issue.setdefault("impacts", _string_list(issue.get("why_it_matters")))
            suggestions = issue.get("suggestions")
            if not isinstance(suggestions, list) or not suggestions:
                actions = issue.get("recommended_actions")
                issue["suggestions"] = [
                    str(action.get("task_title")).strip()
                    for action in actions if isinstance(action, dict) and str(action.get("task_title") or "").strip()
                ] if isinstance(actions, list) else []
    return report


def _client_summary_from_legacy(module_1: dict[str, Any]) -> dict[str, Any]:
    title = str(module_1.get("title") or "SearchTrust legacy-adapted summary")
    summary = str(module_1.get("summary") or module_1.get("diagnosis_summary") or "Legacy output was adapted into report_v2_1.")
    primary = str(module_1.get("primary_blocking_layer") or "Review the primary blocking layer.")
    return {
        "title": title,
        "plain_language_summary": summary,
        "why_it_matters": str(module_1.get("impact") or "Legacy output may not include structured evidence, so the v2.1 report is partial."),
        "first_priority": primary,
        "not_first_priority": "Do not treat legacy-adapted placeholders as complete evidence.",
        "expected_change": "A native report_v2_1 output should provide stronger evidence and action detail.",
    }


def _status_card(value: Any, fallback_label: str, fallback_level: str, level_map: dict[str, str]) -> dict[str, str]:
    parsed, _ = parse_json_maybe(value)
    if not isinstance(parsed, dict):
        parsed = {}
    label = str(parsed.get("label") or fallback_label)
    raw_level = str(parsed.get("level") or parsed.get("value") or "").strip()
    level = level_map.get(raw_level, fallback_level)
    explanation = str(parsed.get("explanation") or parsed.get("description") or "Adapted from legacy status card output.")
    return {"label": label, "level": level, "explanation": explanation}


def _gbp_status_from_context(context: dict[str, Any]) -> dict[str, str]:
    return build_gbp_status(context)


def _data_coverage_from_context(context: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    return build_data_coverage(context, warnings)


def _legacy_action(
    action_id: str,
    layer_key: str,
    title: str,
    suggestions: Any = None,
    completion: Any = None,
    expected: str = "",
) -> dict[str, Any]:
    suggestion_list = _string_list(suggestions)
    completion_list = _string_list(completion) or ["Complete the legacy recommendation and verify it on the page."]
    return {
        "id": action_id,
        "priority": "medium",
        "task_title": title,
        "affected_layer": _best_layer_key(layer_key),
        "related_rule_ids": [],
        "where_to_add": ["Relevant page sections identified during implementation."],
        "what_to_add": suggestion_list or ["Review the legacy recommendation and add missing trust-supporting details."],
        "example_copy": [],
        "implementation_notes": ["Adapted from legacy Dify output; structured implementation detail was not available."],
        "completion_signals": completion_list,
        "expected_effect": expected or "Improves the affected trust layer when implemented with page-specific evidence.",
        "effort_level": "medium",
    }


def _not_available_evidence(evidence_id: str, explanation: str) -> dict[str, str]:
    return {
        "id": evidence_id,
        "source_type": "not_available",
        "source_label": "Legacy output",
        "comparison_result": "not_checked",
        "confidence": "low",
        "explanation": explanation,
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _append_limitations(report: dict[str, Any], notes: list[str]) -> None:
    coverage = report.get("data_coverage")
    if not isinstance(coverage, dict):
        return
    limitations = coverage.setdefault("limitations", [])
    if not isinstance(limitations, list):
        return
    for note in notes:
        safe_note = _redact_validation_note(note)
        if safe_note not in limitations:
            limitations.append(safe_note)


def _redact_validation_note(note: str) -> str:
    safe_note = str(note)
    for phrase in OLD_LAYER_LABELS:
        safe_note = re.sub(re.escape(phrase), "[blocked old layer label]", safe_note, flags=re.IGNORECASE)
    for phrase in GOOGLE_CERTAINTY_PHRASES:
        safe_note = re.sub(re.escape(phrase), "[blocked Google certainty claim]", safe_note, flags=re.IGNORECASE)
    for phrase in BLOCKED_GBP_CLAIM_PHRASES:
        safe_note = re.sub(re.escape(phrase), "[blocked GBP claim]", safe_note, flags=re.IGNORECASE)
    return f"Validation note: {safe_note}"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _string_summary(value: Any) -> str:
    return " ".join(_string_list(value)).strip()


def _best_layer_key(value: Any) -> str:
    raw = str(value or "").lower()
    for key in REQUIRED_LAYER_KEYS:
        if key in raw:
            return key
    for key, label in LAYER_LABELS.items():
        if label.lower() in raw:
            return key
    return "foundation"


def _looks_like_inner_report(value: dict[str, Any]) -> bool:
    return value.get("schema_version") == "2.1" or "gbp_status" in value or "data_coverage" in value


def _context_str(context: dict[str, Any], key: str, default: str) -> str:
    value = context.get(key)
    if value is None or value == "":
        return default
    return str(value)


def _report_id_from_context(context: dict[str, Any]) -> str:
    task_id = _context_str(context, "task_id", "")
    if task_id:
        return f"RPT-{task_id}"
    return f"RPT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
