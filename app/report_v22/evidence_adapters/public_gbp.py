"""Public observations only: unknowns are coverage, not empty business facts."""
from app.report_v22.evidence_adapters.common import coverage, full_limitations, observe, scalar, LIMITATIONS_OVERFLOW
from app.report_v22.models import SourceLocator
from app.report_v22.public_gbp_models import FIELD_NAMES, KEY_ORDER

PUBLIC_NOTE = "These observations are from a public customer GBP profile, not authorized GBP Performance data."
NAMESPACE = "customer_public_gbp_v1"


def source_notes(source):
    notes = [PUBLIC_NOTE, *full_limitations(source)]
    notes.extend(f"Customer public GBP identity: {reason}." for reason in source.payload.identity_reasons)
    if source.payload.failure_code is not None:
        notes.append(f"Customer public GBP lookup failed: {source.payload.failure_code}.")
    return sorted(set(notes))


def limited_notes(notes):
    notes = sorted(set([PUBLIC_NOTE, *notes]))
    if len(notes) <= 20:
        return notes
    retained = [note for note in notes if note not in {PUBLIC_NOTE, LIMITATIONS_OVERFLOW}][:18]
    return sorted([*retained, PUBLIC_NOTE, LIMITATIONS_OVERFLOW])


def public_observe(source, key, field, value, path, **options):
    observation = observe(source, "public_gbp_field", key, field, value, path, **options)
    # These scalars were already strictly validated in the snapshot. Preserve
    # their exact text across the models' whitespace-stripping defaults.
    return observation.model_copy(update={"original_value": scalar(value), "normalized_value": scalar(value)})


def record_context(source, field):
    payload = source.payload
    keys = [f"{key.kind}:{key.value}" for key in payload.record.observed_entity_keys]
    return [NAMESPACE, payload.subject_reference_checksum, *keys, field]


def locator(source, reference):
    expected = {key.kind: key.value for key in reference.entity_keys}
    observed = {key.kind: key.value for key in source.payload.record.observed_entity_keys}
    matched = next((f"{kind}:{observed[kind]}" for kind in KEY_ORDER
                    if kind in observed and observed[kind] == expected.get(kind)), None)
    return SourceLocator(url=source.payload.record.observed_public_gbp_url, external_resource_id=matched)


def observations(source, reference):
    record = source.payload.record
    options = dict(locator=locator(source, reference), limitations=source_notes(source))
    for field in FIELD_NAMES:
        wrapper = getattr(record.fields, field)
        key, prefix = record_context(source, field), f"/payload/record/fields/{field}"
        if wrapper.state != "observed":
            reason = "empty" if wrapper.state == "returned_empty" else "partial"
            yield coverage(source, key, field, reason, wrapper.state, f"{prefix}/state", **options)
        elif isinstance(wrapper.value, list):
            for index, value in enumerate(wrapper.value):
                yield public_observe(source, key, field, value, f"{prefix}/value/{index}", **options)
        else:
            yield public_observe(source, key, field, wrapper.value, f"{prefix}/value", **options)
    for index, key in enumerate(record.observed_entity_keys):
        field = f"observed_entity_keys.{key.kind}"
        yield public_observe(source, record_context(source, field), field, key.value,
                      f"/payload/record/observed_entity_keys/{index}/value", **options)
    if record.observed_public_gbp_url is not None:
        field = "observed_public_gbp_url"
        yield public_observe(source, record_context(source, field), field, record.observed_public_gbp_url,
                      "/payload/record/observed_public_gbp_url", **options)
