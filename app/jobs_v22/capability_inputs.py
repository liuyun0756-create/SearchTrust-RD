"""Reconstructible input recipes without duplicating source payloads."""
from copy import deepcopy

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.capability_public import RULESET as PUBLIC_RULESET, LEGACY_RULESET as LEGACY_PUBLIC_RULESET, PRIORITY_RULESET, BUSINESS_RULESET as BUSINESS_PUBLIC_RULESET, _bound_public_sources
from app.report_v22.capability_service import _validated
from app.report_v22.findings_models import PublicFindingsInput


def validate_generation_input(manifest, request, previous_report=None):
    bound = AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
    if bound.mode == "acquisition":
        value = PublicFindingsInput.model_validate_json(canonical_json_bytes(request))
        if (previous_report is not None or bound.ruleset_version not in {PUBLIC_RULESET, BUSINESS_PUBLIC_RULESET, LEGACY_PUBLIC_RULESET, PRIORITY_RULESET}
                or verified_request_digest(value.model_dump(mode="json")) != bound.generation_input_checksum):
            raise ValueError("Invalid generation identity")
        _bound_public_sources(bound, value)
    else:
        bound, value = _validated(bound, request, previous_report)
        _bound_public_sources(bound, value.public_input)
    return bound, value


def generation_recipe(manifest, request, previous_report=None):
    bound, value = validate_generation_input(manifest, request, previous_report)
    body = value.model_dump(mode="json")
    recipe = deepcopy(body)
    public = recipe if bound.mode == "acquisition" else recipe["public_input"]
    for source in public["evidence_input"]["sources"]:
        source.pop("payload")
        if source.get("shared_snapshot") is not None:
            source["shared_snapshot"].pop("snapshot")
    if bound.mode != "acquisition":
        # Keep normalized header timestamps and order: manifests may encode the
        # same instant with different fractional precision. Never duplicate the
        # metric body or temporary official GBP content.
        for snapshot in recipe["first_party_input"]["snapshots"]:
            snapshot.pop("normalized_payload")
            snapshot.pop("raw_payload")
    wrapped = dict(schema_version="v22_generation_recipe_v1", request=recipe)
    if len(canonical_json_bytes(wrapped)) > 250_000:
        raise ValueError("Generation recipe is too large")
    return bound, value, wrapped
