"""Build, validate and render the v2.2 controlled copy contract."""

from __future__ import annotations

import json
from typing import Any, Iterable

from pydantic import ValidationError

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.action_errors import PublicActionError
from app.report_v22.action_models import ActionSkeleton, PublicActionPlan, PublicActionPlanInput
from app.report_v22.actions import build_public_action_plan, canonical_public_findings
from app.report_v22.copy_catalog import (
    CATALOG_VERSION as COPY_CATALOG_VERSION,
    CopyPatternDefinition,
    pattern_definition,
    pattern_definitions_for,
)
from app.report_v22.copy_errors import PublicCopyInputError, PublicCopyOutputError
from app.report_v22.copy_models import (
    CopyActionInput,
    CopyActionSelection,
    CopyFact,
    CopyPatternSpec,
    CopyPurpose,
    CopyRequestV1,
    CopyResponseV1,
    PatternSelection,
    PublicActionCopyResult,
    PublicCopyLimits,
    RenderedActionCopy,
)
from app.report_v22.evidence_models import reject_nonfinite
from app.report_v22.findings_models import PublicFindingsResult
from app.report_v22.models import Finding
from app.report_v22.public_action_catalog import (
    ACTION_TEMPLATES,
    CATALOG_VERSION as ACTION_CATALOG_VERSION,
)


COPY_OUTPUT_KEY = "action_copy_v2_2"
COPY_CONTRACT_VERSION = "v22_controlled_copy_v1"

_FINDING_FIELDS = {
    "finding.statement": ("statement", "finding_statement"),
    "finding.scope": ("scope", "finding_scope"),
    "finding.severity": ("severity", "finding_severity"),
    "finding.confidence": ("confidence", "finding_confidence"),
}
_TARGET_FIELDS = {
    "target.url": ("url", "url", "target_url"),
    "target.query": ("query", "query", "target_query"),
    "target.page_type": ("page_type", "page_type", "target_page_type"),
    "target.gbp_field": ("gbp_field", "gbp_field", "target_gbp_field"),
    "target.site": ("site", "url", "target_site"),
}
_WHY_FACT_TYPES = {
    "finding_statement",
    "finding_scope",
    "finding_severity",
    "finding_confidence",
}
_SAFE_ERROR_PATH_PARTS = {
    COPY_OUTPUT_KEY,
    "schema_version",
    "copy_contract_version",
    "copy_catalog_version",
    "language",
    "action_plan_checksum",
    "actions",
    "action_id",
    "sequence",
    "why_now",
    "client_facing_explanation",
    "pattern_key",
    "slot_bindings",
    "finding",
    "targets",
    "completion",
    "limitations",
}


def _validated_limits(value: PublicCopyLimits | None) -> PublicCopyLimits:
    try:
        reject_nonfinite(value or PublicCopyLimits())
        raw = (value or PublicCopyLimits()).model_dump(mode="json", warnings=False)
        return PublicCopyLimits.model_validate_json(canonical_json_bytes(raw))
    except (TypeError, ValueError, ValidationError):
        raise PublicCopyInputError("INPUT_INVALID") from None


def _validated_findings(value: PublicFindingsResult | dict[str, Any]) -> PublicFindingsResult:
    try:
        reject_nonfinite(value)
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, PublicFindingsResult) else value
        parsed = PublicFindingsResult.model_validate_json(canonical_json_bytes(raw))
        return canonical_public_findings(parsed)
    except (TypeError, ValueError, ValidationError):
        raise PublicCopyInputError("INPUT_INVALID") from None


def canonical_public_action_plan(value: PublicActionPlan) -> PublicActionPlan:
    """Normalize set-like audit fields before binding the plan checksum."""
    return value.model_copy(
        update={
            "selection_audit": sorted(
                value.selection_audit,
                key=lambda item: item.candidate_key,
            ),
            "unselected_finding_ids": sorted(value.unselected_finding_ids),
        }
    )


def _validated_plan(value: PublicActionPlan | dict[str, Any]) -> PublicActionPlan:
    try:
        reject_nonfinite(value)
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, PublicActionPlan) else value
        parsed = PublicActionPlan.model_validate_json(canonical_json_bytes(raw))
        return canonical_public_action_plan(parsed)
    except (TypeError, ValueError, ValidationError):
        raise PublicCopyInputError("INPUT_INVALID") from None


def _safe_validation_paths(exc: ValidationError, maximum: int) -> list[str]:
    def safe_part(value: object) -> str:
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value in _SAFE_ERROR_PATH_PARTS:
            return value
        return "<unexpected>"

    return [
        ".".join(safe_part(part) for part in error["loc"])
        for error in exc.errors(include_url=False, include_context=False, include_input=False)[:maximum]
    ]


def _fact_id(
    *,
    action_id: str,
    source_finding_ids: list[str],
    source_path: str,
    fact_type: str,
    value: str,
) -> str:
    digest = request_digest(
        {
            "copy_contract_version": COPY_CONTRACT_VERSION,
            "action_id": action_id,
            "source_finding_ids": source_finding_ids,
            "source_path": source_path,
            "fact_type": fact_type,
            "value": value,
        }
    )
    return f"cf_{digest[7:31]}"


def _copy_fact(
    *,
    action: ActionSkeleton,
    source_finding_ids: Iterable[str],
    source_path: str,
    fact_type: str,
    value: Any,
    limits: PublicCopyLimits,
) -> CopyFact:
    text = str(value).strip()
    if not text or len(text) > limits.max_fact_value_chars:
        raise PublicCopyInputError("LIMIT_EXCEEDED")
    finding_ids = sorted(set(source_finding_ids))
    purpose = "why_now" if fact_type in _WHY_FACT_TYPES else "client_facing_explanation"
    try:
        return CopyFact(
            fact_id=_fact_id(
                action_id=action.action_id,
                source_finding_ids=finding_ids,
                source_path=source_path,
                fact_type=fact_type,
                value=text,
            ),
            action_id=action.action_id,
            source_finding_ids=finding_ids,
            source_path=source_path,
            fact_type=fact_type,
            value=text,
            allowed_purposes=[purpose],
        )
    except ValidationError:
        raise PublicCopyInputError("INPUT_INVALID") from None


def _target_value(action_target: Any, attribute: str) -> Any:
    return getattr(action_target, attribute)


def _facts_for_action(
    action: ActionSkeleton,
    findings: dict[str, Finding],
    limits: PublicCopyLimits,
) -> list[CopyFact]:
    allowed = set(action.copy_requirements.allowed_fact_fields)
    facts: list[CopyFact] = []
    for finding_id in sorted(action.finding_ids):
        finding = findings[finding_id]
        for field_name, (attribute, fact_type) in _FINDING_FIELDS.items():
            if field_name not in allowed:
                continue
            facts.append(
                _copy_fact(
                    action=action,
                    source_finding_ids=[finding_id],
                    source_path=f"finding.{finding_id}.{attribute}",
                    fact_type=fact_type,
                    value=getattr(finding, attribute),
                    limits=limits,
                )
            )

    for target in sorted(action.exact_targets, key=canonical_json_bytes):
        target_identity = request_digest(
            {
                "kind": target.kind,
                "value": str(
                    target.url
                    if target.kind in {"url", "site"}
                    else getattr(target, target.kind)
                ),
                "finding_ids": target.finding_ids,
            }
        )[7:23]
        for field_name, (kind, attribute, fact_type) in _TARGET_FIELDS.items():
            if field_name not in allowed or target.kind != kind:
                continue
            facts.append(
                _copy_fact(
                    action=action,
                    source_finding_ids=target.finding_ids,
                    source_path=f"target.{target.kind}.{target_identity}.{attribute}",
                    fact_type=fact_type,
                    value=_target_value(target, attribute),
                    limits=limits,
                )
            )

    if "action.definition_of_done" in allowed:
        for index, value in enumerate(action.definition_of_done):
            facts.append(
                _copy_fact(
                    action=action,
                    source_finding_ids=action.finding_ids,
                    source_path=f"action.definition_of_done.{index}",
                    fact_type="definition_of_done",
                    value=value,
                    limits=limits,
                )
            )

    for index, value in enumerate(action.copy_requirements.required_limitations):
        facts.append(
            _copy_fact(
                action=action,
                source_finding_ids=action.finding_ids,
                source_path=f"action.required_limitation.{index}",
                fact_type="required_limitation",
                value=value,
                limits=limits,
            )
        )

    facts.sort(key=lambda fact: (fact.source_path, fact.fact_id))
    if not facts or len(facts) > limits.max_facts_per_action:
        raise PublicCopyInputError("LIMIT_EXCEEDED")
    return facts


def _pattern_is_satisfiable(
    definition: CopyPatternDefinition,
    facts: list[CopyFact],
) -> bool:
    for slot in definition.spec.slots:
        count = sum(fact.fact_type in slot.allowed_fact_types for fact in facts)
        if count < slot.min_items:
            return False
    return True


def _pattern_specs(
    action: ActionSkeleton,
    facts: list[CopyFact],
    purpose: CopyPurpose,
    limits: PublicCopyLimits,
) -> list[CopyPatternSpec]:
    definitions = [
        definition
        for definition in pattern_definitions_for(action.template_key, purpose)
        if _pattern_is_satisfiable(definition, facts)
    ]
    if not definitions or len(definitions) > limits.max_patterns_per_purpose:
        raise PublicCopyInputError("CATALOG_INVALID")
    if any(len(definition.spec.slots) > limits.max_slots_per_pattern for definition in definitions):
        raise PublicCopyInputError("LIMIT_EXCEEDED")
    return [definition.spec.model_copy(deep=True) for definition in definitions]


def _validate_action_contract(
    action: ActionSkeleton,
    findings: dict[str, Finding],
) -> None:
    template = ACTION_TEMPLATES.get(action.template_key)
    if template is None or action.template_version != template.version:
        raise PublicCopyInputError("CATALOG_INVALID")
    if not set(action.finding_ids) <= set(findings):
        raise PublicCopyInputError("INPUT_INVALID")
    if action.copy_requirements.finding_ids != action.finding_ids:
        raise PublicCopyInputError("INPUT_INVALID")
    if action.copy_requirements.allowed_fact_fields != list(template.copy_allowed_fact_fields):
        raise PublicCopyInputError("CATALOG_INVALID")
    if action.copy_requirements.required_limitations != list(template.copy_required_limitations):
        raise PublicCopyInputError("CATALOG_INVALID")


def build_copy_request(
    findings: PublicFindingsResult | dict[str, Any],
    action_plan: PublicActionPlan | dict[str, Any],
    *,
    limits: PublicCopyLimits | None = None,
) -> CopyRequestV1:
    """Build the minimal stable Dify input from authoritative v2.2 facts."""
    checked_limits = _validated_limits(limits)
    checked_findings = _validated_findings(findings)
    checked_plan = _validated_plan(action_plan)
    if checked_plan.action_catalog_version != ACTION_CATALOG_VERSION:
        raise PublicCopyInputError("CATALOG_INVALID")
    if checked_plan.findings_checksum != request_digest(checked_findings):
        raise PublicCopyInputError("CHECKSUM_MISMATCH")
    try:
        rebuilt_plan = canonical_public_action_plan(
            build_public_action_plan(
                PublicActionPlanInput(
                    findings_result=checked_findings,
                    planning_date=checked_plan.planning_date,
                )
            )
        )
    except (PublicActionError, TypeError, ValueError, ValidationError):
        raise PublicCopyInputError("INPUT_INVALID") from None
    if canonical_json_bytes(rebuilt_plan) != canonical_json_bytes(checked_plan):
        raise PublicCopyInputError("CHECKSUM_MISMATCH")
    if len(
        canonical_json_bytes(
            {
                "findings": checked_findings.model_dump(mode="json"),
                "action_plan": checked_plan.model_dump(mode="json"),
            }
        )
    ) > checked_limits.max_input_bytes:
        raise PublicCopyInputError("LIMIT_EXCEEDED")

    finding_map = {finding.finding_id: finding for finding in checked_findings.findings}
    actions: list[CopyActionInput] = []
    for action in checked_plan.actions:
        _validate_action_contract(action, finding_map)
        facts = _facts_for_action(action, finding_map, checked_limits)
        limitations = [
            fact.fact_id for fact in facts if fact.fact_type == "required_limitation"
        ]
        try:
            actions.append(
                CopyActionInput(
                    action_id=action.action_id,
                    sequence=action.sequence,
                    template_key=action.template_key,
                    template_version=action.template_version,
                    finding_ids=action.finding_ids,
                    facts=facts,
                    why_now_patterns=_pattern_specs(
                        action,
                        facts,
                        "why_now",
                        checked_limits,
                    ),
                    explanation_patterns=_pattern_specs(
                        action,
                        facts,
                        "client_facing_explanation",
                        checked_limits,
                    ),
                    required_limitation_ids=limitations,
                )
            )
        except ValidationError:
            raise PublicCopyInputError("INPUT_INVALID") from None

    request = CopyRequestV1(
        copy_catalog_version=COPY_CATALOG_VERSION,
        findings_checksum=checked_plan.findings_checksum,
        action_plan_checksum=request_digest(checked_plan),
        actions=actions,
    )
    if len(canonical_json_bytes(request)) > checked_limits.max_input_bytes:
        raise PublicCopyInputError("LIMIT_EXCEEDED")
    return request


def _validated_request(
    value: CopyRequestV1 | dict[str, Any],
    limits: PublicCopyLimits,
) -> CopyRequestV1:
    try:
        reject_nonfinite(value)
        raw = value.model_dump(mode="json", warnings=False) if isinstance(value, CopyRequestV1) else value
        request = CopyRequestV1.model_validate_json(canonical_json_bytes(raw))
    except (TypeError, ValueError, ValidationError):
        raise PublicCopyInputError("INPUT_INVALID") from None
    if len(canonical_json_bytes(request)) > limits.max_input_bytes:
        raise PublicCopyInputError("LIMIT_EXCEEDED")
    for action in request.actions:
        if (
            len(action.facts) > limits.max_facts_per_action
            or len(action.why_now_patterns) > limits.max_patterns_per_purpose
            or len(action.explanation_patterns) > limits.max_patterns_per_purpose
            or any(
                len(pattern.slots) > limits.max_slots_per_pattern
                for pattern in action.why_now_patterns + action.explanation_patterns
            )
        ):
            raise PublicCopyInputError("LIMIT_EXCEEDED")
        template = ACTION_TEMPLATES.get(action.template_key)
        if template is None or template.version != action.template_version:
            raise PublicCopyInputError("CATALOG_INVALID")
        for fact in action.facts:
            expected_id = _fact_id(
                action_id=fact.action_id,
                source_finding_ids=fact.source_finding_ids,
                source_path=fact.source_path,
                fact_type=fact.fact_type,
                value=fact.value,
            )
            if fact.fact_id != expected_id:
                raise PublicCopyInputError("CHECKSUM_MISMATCH")
            if len(fact.value) > limits.max_fact_value_chars:
                raise PublicCopyInputError("LIMIT_EXCEEDED")
        expected_why = [
            item.spec
            for item in pattern_definitions_for(action.template_key, "why_now")
            if _pattern_is_satisfiable(item, action.facts)
        ]
        expected_explanation = [
            item.spec
            for item in pattern_definitions_for(
                action.template_key,
                "client_facing_explanation",
            )
            if _pattern_is_satisfiable(item, action.facts)
        ]
        if action.why_now_patterns != expected_why or action.explanation_patterns != expected_explanation:
            raise PublicCopyInputError("CATALOG_INVALID")
    return request


def validate_copy_request(
    value: CopyRequestV1 | dict[str, Any],
    *,
    limits: PublicCopyLimits | None = None,
) -> CopyRequestV1:
    """Revalidate a bound request before it crosses the provider boundary."""
    checked_limits = _validated_limits(limits)
    return _validated_request(value, checked_limits)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_response(
    outputs: object,
    limits: PublicCopyLimits,
) -> CopyResponseV1:
    if outputs is None:
        raise PublicCopyOutputError("OUTPUT_MISSING")
    try:
        reject_nonfinite(outputs)
        encoded = canonical_json_bytes(outputs)
    except (TypeError, ValueError):
        raise PublicCopyOutputError("OUTPUT_INVALID") from None
    if len(encoded) > limits.max_output_bytes:
        raise PublicCopyOutputError("LIMIT_EXCEEDED")
    if not isinstance(outputs, dict) or set(outputs) != {COPY_OUTPUT_KEY}:
        raise PublicCopyOutputError("OUTPUT_INVALID", ["outputs"])
    value = outputs.get(COPY_OUTPUT_KEY)
    if value is None:
        raise PublicCopyOutputError("OUTPUT_MISSING")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > limits.max_output_bytes:
            raise PublicCopyOutputError("LIMIT_EXCEEDED")
        if value.lstrip().startswith("```"):
            raise PublicCopyOutputError("OUTPUT_INVALID", [COPY_OUTPUT_KEY])
        try:
            value = json.loads(value, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, ValueError):
            raise PublicCopyOutputError("OUTPUT_INVALID", [COPY_OUTPUT_KEY]) from None
    if not isinstance(value, dict):
        raise PublicCopyOutputError("OUTPUT_INVALID", [COPY_OUTPUT_KEY])
    try:
        return CopyResponseV1.model_validate(value)
    except ValidationError as exc:
        paths = _safe_validation_paths(exc, limits.max_error_details)
        if any(path == "language" for path in paths):
            raise PublicCopyOutputError("LANGUAGE_INVALID", paths) from None
        raise PublicCopyOutputError("OUTPUT_INVALID", paths) from None


def _selected_pattern(
    action: CopyActionInput,
    selection: PatternSelection,
    purpose: CopyPurpose,
) -> tuple[CopyPatternDefinition, CopyPatternSpec]:
    available = (
        action.why_now_patterns
        if purpose == "why_now"
        else action.explanation_patterns
    )
    spec = next(
        (item for item in available if item.pattern_key == selection.pattern_key),
        None,
    )
    definition = pattern_definition(selection.pattern_key)
    if spec is None or definition is None or definition.spec != spec:
        raise PublicCopyOutputError("REFERENCE_INVALID", [purpose, "pattern_key"])
    if definition.spec.purpose != purpose:
        raise PublicCopyOutputError("REFERENCE_INVALID", [purpose, "purpose"])
    return definition, spec


def _render_selection(
    action: CopyActionInput,
    selection: PatternSelection,
    purpose: CopyPurpose,
    limits: PublicCopyLimits,
) -> tuple[str, list[str]]:
    definition, spec = _selected_pattern(action, selection, purpose)
    slots = {slot.slot_name: slot for slot in spec.slots}
    if set(selection.slot_bindings) != set(slots):
        raise PublicCopyOutputError("REFERENCE_INVALID", [purpose, "slot_bindings"])
    facts = {fact.fact_id: fact for fact in action.facts}
    used_ids: list[str] = []
    rendered_slots: dict[str, str] = {}
    for slot in spec.slots:
        fact_ids = selection.slot_bindings[slot.slot_name]
        if (
            len(fact_ids) < slot.min_items
            or len(fact_ids) > slot.max_items
            or len(fact_ids) > limits.max_fact_refs_per_slot
        ):
            raise PublicCopyOutputError("REFERENCE_INVALID", [purpose, slot.slot_name])
        values: list[str] = []
        for fact_id in fact_ids:
            fact = facts.get(fact_id)
            if (
                fact is None
                or purpose not in fact.allowed_purposes
                or fact.fact_type not in slot.allowed_fact_types
            ):
                raise PublicCopyOutputError("REFERENCE_INVALID", [purpose, slot.slot_name])
            values.append(fact.value)
        used_ids.extend(fact_ids)
        rendered_slots[slot.slot_name] = "; ".join(values)
    if len(used_ids) != len(set(used_ids)):
        raise PublicCopyOutputError("REFERENCE_INVALID", [purpose, "duplicate_fact"])
    try:
        rendered = definition.template.format(**rendered_slots).strip()
    except (KeyError, ValueError):
        raise PublicCopyInputError("CATALOG_INVALID") from None
    maximum = min(spec.max_rendered_chars, limits.max_rendered_chars)
    if not rendered or len(rendered) > maximum:
        raise PublicCopyOutputError("LIMIT_EXCEEDED", [purpose])
    return rendered, used_ids


def _validate_mandatory_explanation_facts(
    action: CopyActionInput,
    explanation_ids: list[str],
) -> None:
    facts = {fact.fact_id: fact for fact in action.facts}
    selected = set(explanation_ids)
    required_types = {
        "target_url",
        "target_query",
        "target_page_type",
        "target_gbp_field",
        "target_site",
        "definition_of_done",
    }
    required = {
        fact.fact_id for fact in action.facts if fact.fact_type in required_types
    } | set(action.required_limitation_ids)
    if not required or selected != required:
        raise PublicCopyOutputError(
            "REFERENCE_INVALID",
            ["client_facing_explanation", "required_facts"],
        )
    if any(facts[fact_id].action_id != action.action_id for fact_id in selected):
        raise PublicCopyOutputError("REFERENCE_INVALID", ["action_id"])


def _render_action(
    request_action: CopyActionInput,
    response_action: CopyActionSelection,
    limits: PublicCopyLimits,
) -> RenderedActionCopy:
    why_now, why_ids = _render_selection(
        request_action,
        response_action.why_now,
        "why_now",
        limits,
    )
    explanation, explanation_ids = _render_selection(
        request_action,
        response_action.client_facing_explanation,
        "client_facing_explanation",
        limits,
    )
    _validate_mandatory_explanation_facts(request_action, explanation_ids)
    return RenderedActionCopy(
        action_id=request_action.action_id,
        sequence=request_action.sequence,
        why_now=why_now,
        client_facing_explanation=explanation,
        why_now_pattern_key=response_action.why_now.pattern_key,
        why_now_fact_ids=why_ids,
        explanation_pattern_key=response_action.client_facing_explanation.pattern_key,
        explanation_fact_ids=explanation_ids,
    )


def validate_and_render_copy(
    request: CopyRequestV1 | dict[str, Any],
    outputs: object,
    *,
    limits: PublicCopyLimits | None = None,
) -> PublicActionCopyResult:
    """Reject unsupported output and render copy only from backend facts."""
    checked_limits = _validated_limits(limits)
    checked_request = _validated_request(request, checked_limits)
    response = _parse_response(outputs, checked_limits)
    if response.action_plan_checksum != checked_request.action_plan_checksum:
        raise PublicCopyOutputError("CHECKSUM_MISMATCH", ["action_plan_checksum"])
    request_identity = [
        (action.action_id, action.sequence) for action in checked_request.actions
    ]
    response_identity = [
        (action.action_id, action.sequence) for action in response.actions
    ]
    if response_identity != request_identity:
        raise PublicCopyOutputError("REFERENCE_INVALID", ["actions"])
    rendered = [
        _render_action(request_action, response_action, checked_limits)
        for request_action, response_action in zip(
            checked_request.actions,
            response.actions,
            strict=True,
        )
    ]
    result = PublicActionCopyResult(
        findings_checksum=checked_request.findings_checksum,
        action_plan_checksum=checked_request.action_plan_checksum,
        actions=rendered,
    )
    if len(canonical_json_bytes(result)) > checked_limits.max_result_bytes:
        raise PublicCopyOutputError("LIMIT_EXCEEDED", ["result"])
    return result
