"""Compare pinned identity evidence with the final saved acquisition sources."""
from uuid import UUID, uuid5, NAMESPACE_URL

from pydantic import Field, model_validator

from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.report_v22.business_identity_v2 import BusinessIdentityV2
from app.report_v22.evidence_bindings import host
from app.report_v22.identity_inheritance_contract import IdentityInheritance, MANIFEST_SCHEMA, RULESET
from app.report_v22.identity_input import prepare_identity_input, source_set_checksum
from app.report_v22.keyword_visibility import assess_bound_profile
from app.report_v22.models import StrictModel
from app.report_v22.site_gbp_comparators import compare_addresses, normalize_phone


class PinnedIdentity(StrictModel):
    id: UUID
    revision: int = Field(ge=1)
    identity: BusinessIdentityV2
    identity_checksum: str
    entity_keys: dict[str, str]
    gbp_website_url: str | None
    gbp_phone: str | None
    phone_matched: bool

    @model_validator(mode="after")
    def bound_identity(self):
        if request_digest(self.identity) != self.identity_checksum or not self.identity.operating_model.confirmation_ref:
            raise ValueError("invalid pinned confirmation")
        return self


def _material(evidence):
    # Ignore source selectors, checksums and crawl order, but retain the supporting words.
    return {(item.kind, " ".join(item.quote.casefold().split())) for item in evidence}


def inherit_identity(manifest, request, pinned: PinnedIdentity):
    original = pinned.identity
    confirmation = original.operating_model.confirmation_ref
    if not isinstance(request.business_identity, BusinessIdentityV2):
        manifest, request, _, _ = prepare_identity_input(manifest, request)
    current = request.business_identity
    profile = assess_bound_profile(request, include_observed_url=True)
    keys, match = profile if profile is not None else ({}, None)

    old_keys = {kind: value for kind, value in pinned.entity_keys.items() if kind in {"cid", "place_id", "data_id"}}
    shared_branch_keys = {value for kind, value in keys.items()
        if kind in {"cid", "place_id"} and old_keys.get(kind) == value}
    stable_branch = (original.selected_branch_ref in shared_branch_keys
        and current.selected_branch_ref in shared_branch_keys
        and all(keys[kind] == old_keys[kind] for kind in old_keys.keys() & keys.keys()))
    old_website = host(pinned.gbp_website_url) if pinned.gbp_website_url else None
    current_website = host(match.record.website_url) if match and match.record.website_url else None
    country = original.country_code
    same_phone = (pinned.gbp_phone is None and bool(match) and match.record.phone is None)
    if country and pinned.gbp_phone and match and match.record.phone:
        old_phone = normalize_phone(pinned.gbp_phone, country).normalized_key
        same_phone = bool(old_phone and old_phone == normalize_phone(match.record.phone, country).normalized_key)
    branch_ok = bool(stable_branch and match and match.current
        and (match.domain_match is True or match.phone_match is True)
        and match.domain_match is not False and match.phone_match is not False
        and old_website == current_website and same_phone
        and (not pinned.phone_matched or match.phone_match is True)
        and original.normalized_domain == current.normalized_domain
        and country == current.country_code)

    old_mode = original.operating_model
    auto = current.operating_model
    visits_kinds = {"visiting", "auxiliary"}
    old_material = _material(old_mode.evidence_refs)
    new_material = _material(auto.evidence_refs)
    # Visit evidence has its own gate so its change does not revoke the operating model.
    old_mode_material = {item for item in old_material if item[0] not in visits_kinds}
    new_mode_material = {item for item in new_material if item[0] not in visits_kinds}
    mode_ok = branch_ok and old_mode_material == new_mode_material and old_mode.conflicts == auto.conflicts
    if mode_ok:
        auto = auto.model_copy(update=dict(value=confirmation.value, state="resolved", source="explicit_confirmation",
            confirmation_ref=confirmation.model_copy(update={"evidence_checksum": source_set_checksum(request)})))

    old_visit = original.operating_location.confirmation_ref
    location = current.operating_location
    visit_ok = bool(branch_ok and old_visit and country and location.location
        and compare_addresses(old_visit.address, location.location.address, country).state in {"exact_match", "semantic_match"}
        and {item for item in old_material if item[0] in visits_kinds} == {item for item in new_material if item[0] in visits_kinds})
    if visit_ok:
        location = location.model_copy(update=dict(role="customer_facing", state="seo_confirmed",
            confirmation_ref=old_visit.model_copy(update={"address": location.location.address,
                "source_identity_version_id": pinned.id})))
    identity = current.model_copy(update=dict(operating_model=auto, operating_location=location,
        selected_branch_ref=original.selected_branch_ref if branch_ok else current.selected_branch_ref))
    request = request.model_copy(update={"business_identity": identity})
    checksum = request_digest(identity)
    provenance = IdentityInheritance(base_ruleset=manifest.ruleset_version,
        source_identity_version_id=pinned.id, source_identity_checksum=pinned.identity_checksum,
        source_revision=pinned.revision, confirmed_at=confirmation.confirmed_at,
        branch="retained" if branch_ok else "needs_confirmation",
        operating_model="retained" if mode_ok else "needs_confirmation",
        visits="retained" if visit_ok else "needs_confirmation" if old_visit else "not_previously_confirmed")
    raw = manifest.model_dump(mode="json", exclude={"input_checksum"})
    raw.update(schema_version=MANIFEST_SCHEMA, ruleset_version=RULESET, identity_inheritance=provenance.model_dump(mode="json"),
        identity_version_id=str(uuid5(NAMESPACE_URL, f"{manifest.case_id}:{checksum}")), identity_checksum=checksum,
        generation_input_checksum=verified_request_digest(request.model_dump(mode="json")))
    raw["input_checksum"] = verified_request_digest(raw)
    return type(manifest).model_validate_json(canonical_json_bytes(raw)), request
