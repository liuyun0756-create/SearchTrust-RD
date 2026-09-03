"""Strict models for the v2.2 controlled Dify copy contract."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.report_v22.models import ActionId, FindingId, StrictModel


Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
CopyFactId = Annotated[str, Field(pattern=r"^cf_[a-f0-9]{24}$")]
CopyPatternKey = Annotated[
    str,
    Field(min_length=3, max_length=120, pattern=r"^[a-z][a-z0-9_.-]+$"),
]
CopySlotName = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]+$"),
]
CopyPurpose = Literal["why_now", "client_facing_explanation"]
CopyFactType = Literal[
    "finding_statement",
    "finding_scope",
    "finding_severity",
    "finding_confidence",
    "target_url",
    "target_query",
    "target_page_type",
    "target_gbp_field",
    "target_site",
    "definition_of_done",
    "required_limitation",
]


class PublicCopyLimits(StrictModel):
    max_facts_per_action: int = Field(default=5_000, ge=1, le=5_000)
    max_patterns_per_purpose: int = Field(default=20, ge=1, le=20)
    max_slots_per_pattern: int = Field(default=20, ge=1, le=20)
    max_fact_refs_per_slot: int = Field(default=5_000, ge=1, le=5_000)
    max_fact_value_chars: int = Field(default=200_000, ge=1, le=200_000)
    max_rendered_chars: int = Field(default=200_000, ge=1, le=200_000)
    max_input_bytes: int = Field(default=20_000_000, ge=1, le=20_000_000)
    max_output_bytes: int = Field(default=2_000_000, ge=1, le=2_000_000)
    max_result_bytes: int = Field(default=2_000_000, ge=1, le=2_000_000)
    max_error_details: int = Field(default=20, ge=1, le=20)


class CopyFact(StrictModel):
    fact_id: CopyFactId
    action_id: ActionId
    source_finding_ids: list[FindingId] = Field(default_factory=list, max_length=5_000)
    source_path: str = Field(min_length=1, max_length=500)
    fact_type: CopyFactType
    value: str = Field(min_length=1, max_length=200_000)
    allowed_purposes: list[CopyPurpose] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_fact(self) -> "CopyFact":
        if len(self.source_finding_ids) != len(set(self.source_finding_ids)):
            raise ValueError("copy fact finding IDs must be unique")
        if len(self.allowed_purposes) != len(set(self.allowed_purposes)):
            raise ValueError("copy fact purposes must be unique")
        if self.source_path.startswith("finding.") and len(self.source_finding_ids) != 1:
            raise ValueError("finding facts must reference exactly one finding")
        return self


class CopyPatternSlotSpec(StrictModel):
    slot_name: CopySlotName
    allowed_fact_types: list[CopyFactType] = Field(min_length=1, max_length=20)
    min_items: int = Field(ge=1, le=5_000)
    max_items: int = Field(ge=1, le=5_000)

    @model_validator(mode="after")
    def validate_slot(self) -> "CopyPatternSlotSpec":
        if len(self.allowed_fact_types) != len(set(self.allowed_fact_types)):
            raise ValueError("copy slot fact types must be unique")
        if self.max_items < self.min_items:
            raise ValueError("copy slot maximum must not be below its minimum")
        return self


class CopyPatternSpec(StrictModel):
    pattern_key: CopyPatternKey
    purpose: CopyPurpose
    compatible_template_keys: list[str] = Field(min_length=1, max_length=20)
    selection_hint: str = Field(min_length=1, max_length=300)
    slots: list[CopyPatternSlotSpec] = Field(min_length=1, max_length=20)
    max_rendered_chars: int = Field(ge=1, le=200_000)

    @model_validator(mode="after")
    def validate_pattern(self) -> "CopyPatternSpec":
        if len(self.compatible_template_keys) != len(set(self.compatible_template_keys)):
            raise ValueError("compatible action templates must be unique")
        names = [slot.slot_name for slot in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("copy pattern slot names must be unique")
        return self


class CopyActionInput(StrictModel):
    action_id: ActionId
    sequence: int = Field(ge=1, le=3)
    template_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]+$")
    template_version: str = Field(min_length=1, max_length=32)
    finding_ids: list[FindingId] = Field(min_length=1, max_length=5_000)
    facts: list[CopyFact] = Field(min_length=1, max_length=5_000)
    why_now_patterns: list[CopyPatternSpec] = Field(min_length=1, max_length=20)
    explanation_patterns: list[CopyPatternSpec] = Field(min_length=1, max_length=20)
    required_limitation_ids: list[CopyFactId] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_action(self) -> "CopyActionInput":
        if len(self.finding_ids) != len(set(self.finding_ids)):
            raise ValueError("copy action finding IDs must be unique")
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("copy action fact IDs must be unique")
        finding_set = set(self.finding_ids)
        if any(fact.action_id != self.action_id for fact in self.facts):
            raise ValueError("copy fact belongs to another action")
        if any(not set(fact.source_finding_ids) <= finding_set for fact in self.facts):
            raise ValueError("copy fact references another action finding")
        patterns = self.why_now_patterns + self.explanation_patterns
        pattern_keys = [pattern.pattern_key for pattern in patterns]
        if len(pattern_keys) != len(set(pattern_keys)):
            raise ValueError("copy action pattern keys must be unique")
        if any(pattern.purpose != "why_now" for pattern in self.why_now_patterns):
            raise ValueError("why-now catalog contains another purpose")
        if any(
            pattern.purpose != "client_facing_explanation"
            for pattern in self.explanation_patterns
        ):
            raise ValueError("explanation catalog contains another purpose")
        if any(self.template_key not in pattern.compatible_template_keys for pattern in patterns):
            raise ValueError("copy pattern does not support the action template")
        if len(self.required_limitation_ids) != len(set(self.required_limitation_ids)):
            raise ValueError("required limitation IDs must be unique")
        facts_by_id = {fact.fact_id: fact for fact in self.facts}
        if any(
            fact_id not in facts_by_id
            or facts_by_id[fact_id].fact_type != "required_limitation"
            for fact_id in self.required_limitation_ids
        ):
            raise ValueError("required limitation ID must reference a limitation fact")
        return self


class CopyRequestV1(StrictModel):
    schema_version: Literal["v22_copy_request_v1"] = "v22_copy_request_v1"
    copy_contract_version: Literal["v22_controlled_copy_v1"] = "v22_controlled_copy_v1"
    copy_catalog_version: Literal["v22_english_action_copy_v1"] = "v22_english_action_copy_v1"
    language: Literal["en"] = "en"
    findings_checksum: Digest
    action_plan_checksum: Digest
    actions: list[CopyActionInput] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_request(self) -> "CopyRequestV1":
        if [action.sequence for action in self.actions] != [1, 2, 3]:
            raise ValueError("copy request actions must be ordered 1, 2, 3")
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("copy request action IDs must be unique")
        return self


class PatternSelection(StrictModel):
    pattern_key: CopyPatternKey
    slot_bindings: dict[CopySlotName, list[CopyFactId]] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_bindings(self) -> "PatternSelection":
        for fact_ids in self.slot_bindings.values():
            if not fact_ids:
                raise ValueError("copy slot binding must not be empty")
            if len(fact_ids) != len(set(fact_ids)):
                raise ValueError("copy slot fact IDs must be unique")
        return self


class CopyActionSelection(StrictModel):
    action_id: ActionId
    sequence: int = Field(ge=1, le=3)
    why_now: PatternSelection
    client_facing_explanation: PatternSelection


class CopyResponseV1(StrictModel):
    schema_version: Literal["v22_copy_response_v1"] = "v22_copy_response_v1"
    copy_contract_version: Literal["v22_controlled_copy_v1"] = "v22_controlled_copy_v1"
    copy_catalog_version: Literal["v22_english_action_copy_v1"] = "v22_english_action_copy_v1"
    language: Literal["en"] = "en"
    action_plan_checksum: Digest
    actions: list[CopyActionSelection] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_response(self) -> "CopyResponseV1":
        if [action.sequence for action in self.actions] != [1, 2, 3]:
            raise ValueError("copy response actions must be ordered 1, 2, 3")
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("copy response action IDs must be unique")
        return self


class RenderedActionCopy(StrictModel):
    action_id: ActionId
    sequence: int = Field(ge=1, le=3)
    why_now: str = Field(min_length=1, max_length=200_000)
    client_facing_explanation: str = Field(min_length=1, max_length=200_000)
    why_now_pattern_key: CopyPatternKey
    why_now_fact_ids: list[CopyFactId] = Field(min_length=1, max_length=5_000)
    explanation_pattern_key: CopyPatternKey
    explanation_fact_ids: list[CopyFactId] = Field(min_length=1, max_length=5_000)


class PublicActionCopyResult(StrictModel):
    schema_version: Literal["public_action_copy_result_v1"] = "public_action_copy_result_v1"
    copy_contract_version: Literal["v22_controlled_copy_v1"] = "v22_controlled_copy_v1"
    copy_catalog_version: Literal["v22_english_action_copy_v1"] = "v22_english_action_copy_v1"
    findings_checksum: Digest
    action_plan_checksum: Digest
    actions: list[RenderedActionCopy] = Field(min_length=3, max_length=3)
    attempt_count: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def validate_result(self) -> "PublicActionCopyResult":
        if [action.sequence for action in self.actions] != [1, 2, 3]:
            raise ValueError("rendered copy actions must be ordered 1, 2, 3")
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("rendered copy action IDs must be unique")
        return self
