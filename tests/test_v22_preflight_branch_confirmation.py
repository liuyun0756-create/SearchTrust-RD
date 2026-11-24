from datetime import datetime, timedelta, timezone
import base64
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.preflight_v22.branch_confirmation import branch_options
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.identity_inheritance import PinnedPreflightBranch, inherit_preflight_branch, mode_material
from test_a3_identity_inheritance import saved_confirmation
from test_p0_08_gold import deny_network


@pytest.mark.parametrize("change", ("none", "expired", "phone", "mode", "visit", "branch_only"))
def test_preflight_pin_rechecks_confirmation_groups(change, monkeypatch):
    deny_network(monkeypatch)
    manifest, request, prior = saved_confirmation()
    identity = request.business_identity
    now = datetime.fromisoformat(manifest.evaluated_at)
    fields = next(s.payload.record.fields for s in request.evidence_input.sources if s.kind == "public_gbp")
    pin = PinnedPreflightBranch(id=UUID(int=191), revision=1, confirmation_checksum="sha256:" + "1" * 64,
        confirmed_at=manifest.evaluated_at, confirmed_by=manifest.user_id, reason="Confirmed this saved branch",
        operating_mode=None if change == "branch_only" else "hybrid", customers_can_visit=change != "branch_only",
        evidence=dict(preflight_id=UUID(int=190), observed_at=(now - timedelta(minutes=5)).isoformat(),
            expires_at=(now + timedelta(minutes=-1 if change == "expired" else 5)).isoformat(),
            branch_ref=identity.selected_branch_ref, public_gbp_url=str(identity.public_gbp_url),
            domain=identity.normalized_domain, country_code=identity.country_code, entity_keys=prior["entity_keys"],
            gbp_website_url=prior["gbp_website_url"], gbp_phone="+18005550199" if change == "phone" else prior["gbp_phone"],
            gbp_address="900 Other Road, Austin, TX" if change == "visit" else fields.address.value,
            phone_matched=True, mode_evidence_complete=True,
            mode_material=[["onsite", "Changed operating evidence"]] if change == "mode" else
                [list(item) for item in sorted(mode_material(identity.operating_model.evidence_refs))]))
    initial = canonical_json_bytes(request)
    bound, updated = inherit_preflight_branch(manifest, request, pin)
    report = build_public_capability_report(bound, updated)
    assert report.schema_version == "2.2.16" and bound.identity_inheritance.source_identity_version_id == pin.id
    assert canonical_json_bytes(request) == initial
    assert canonical_json_bytes(updated.evidence_input) == canonical_json_bytes(request.evidence_input)
    proof = bound.identity_inheritance
    assert (proof.branch, proof.operating_model, proof.visits) == {
        "none": ("retained", "retained", "retained"),
        "expired": ("needs_confirmation",) * 3,
        "phone": ("needs_confirmation",) * 3,
        "mode": ("retained", "needs_confirmation", "retained"),
        "visit": ("retained", "retained", "needs_confirmation"),
        "branch_only": ("retained", "needs_confirmation", "not_previously_confirmed"),
    }[change]


def test_offer_requires_same_entity_evidence_and_keeps_original_expiry(monkeypatch):
    deny_network(monkeypatch)
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    good = SimpleNamespace(public_gbp_url="https://www.google.com/maps?cid=701", market=None, phone=None,
        address="1 Example Street", business_name="Example Plumbing", website_url="https://example.test/")
    conflict = SimpleNamespace(**{**vars(good), "public_gbp_url":"https://www.google.com/maps?cid=702",
        "website_url":"https://other.test/"})
    items = branch_options(preflight_id=UUID(int=4), snapshot=SimpleNamespace(normalized_site_url="https://example.test/",
        html="<html><body><p>Visit our showroom.</p></body></html>"), signals=SimpleNamespace(phones=[]),
        candidates=[good, conflict], key="local-synthetic-token", ttl_seconds=900, now=now)
    assert [item.ref for item in items] == ["701"]
    encoded, proof = items[0].evidence_token.split(".")
    assert len(proof) == 64
    decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert decoded["observed_at"] == now.isoformat()
    assert decoded["expires_at"] == (now + timedelta(seconds=900)).isoformat()
    assert decoded["entity_keys"] == {"cid": "701"}
