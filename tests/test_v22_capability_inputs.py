import json

import pytest

from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.digest import canonical_json_bytes
from test_v22_capability_public import inputs as public_inputs
from test_v22_capability_service import inputs as service_inputs


@pytest.mark.parametrize("mode", ["acquisition", "first_service", "after_acquisition"])
def test_recipe_keeps_context_but_never_duplicates_source_or_private_payloads(mode):
    if mode == "acquisition": manifest, value = public_inputs(); parent = None
    else: manifest, value, parent = service_inputs(mode=mode)
    before = canonical_json_bytes(value)
    bound, parsed, recipe = generation_recipe(manifest, value, parent)
    assert bound == manifest and canonical_json_bytes(parsed) == before and canonical_json_bytes(value) == before
    restored = json.loads(canonical_json_bytes(recipe))["request"]
    original = value.model_dump(mode="json")
    public = restored if mode == "acquisition" else restored["public_input"]
    public_original = original if mode == "acquisition" else original["public_input"]
    assert public["evidence_input"]["context"] == public_original["evidence_input"]["context"]
    for source, expected in zip(public["evidence_input"]["sources"], public_original["evidence_input"]["sources"]):
        assert "payload" not in source
        if source.get("shared_snapshot") is not None:
            assert "snapshot" not in source["shared_snapshot"]
            source["shared_snapshot"]["snapshot"] = expected["shared_snapshot"]["snapshot"]
        source["payload"] = expected["payload"]
    if mode != "acquisition":
        assert all("normalized_payload" not in s and "raw_payload" not in s for s in restored["first_party_input"]["snapshots"])
        restored["first_party_input"]["snapshots"] = original["first_party_input"]["snapshots"]
    assert restored == original


def test_changed_non_snapshot_context_cannot_reuse_the_frozen_generation_digest():
    manifest, value = public_inputs()
    body = value.model_dump(mode="json")
    body["limits"]["max_findings"] -= 1
    with pytest.raises(ValueError): generation_recipe(manifest, body)
