"""Independent card-order expectations through the complete public generator."""
from unittest.mock import patch

import pytest

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.identity_input import prepare_identity_input
from app.report_v22.public_rule_catalog import DOMAIN, HTTP, OBSERVED, TITLE
from findings_helpers import market, seal, site
from test_v22_capability_public import inputs


@pytest.mark.parametrize("version", [12, 13])
@pytest.mark.parametrize("higher_severity", [False, True], ids=["same-severity", "severity-before-tier"])
def test_generated_cards_have_explicit_semantic_order_and_are_repeatable(version, higher_severity):
    # Two positive Maps queries and one completed sample without the client.
    # Two duplicate-title groups provide other confirmed facts of the same severity.
    source = market([("example.test", 2, "provider_position")])
    run = source.payload.query_runs[-1]
    run.results[0] = run.results[0].model_copy(update={
        "url": "https://competitor-1.test/", "normalized_domain": "competitor-1.test",
        "record_id": request_digest([run.query, "competitor-1.test"]),
    })
    source = seal(source)
    records = [{"title": title} for title in ["Shared A", "Shared A", "Shared B", "Shared B"]]
    if higher_severity:
        records.append({"status_code": 503, "title": "Unavailable"})
    with patch("test_v22_capability_public.market", return_value=source):
        manifest, value = inputs(business_source=site(records), ruleset="v22_capability_public_v12")
    if version == 13:
        manifest, value, _, _ = prepare_identity_input(manifest, value)
    assert manifest.ruleset_version == f"v22_capability_public_v{version}"
    before = (canonical_json_bytes(manifest), canonical_json_bytes(value))

    first = build_public_capability_report(manifest, value)
    second = build_public_capability_report(manifest, value)
    cards = first.findings
    # Golden rule order, deliberately independent of finding_display_key.
    assert [f.rule_id for f in cards] == (
        ([HTTP] if higher_severity else []) + [OBSERVED, OBSERVED, TITLE, TITLE, DOMAIN])
    assert all(f.status == "confirmed" for f in cards if f.rule_id in {HTTP, OBSERVED, TITLE})
    assert next(f for f in cards if f.rule_id == DOMAIN).status == "not_detected"
    if higher_severity:
        assert cards[0].severity == "high"  # HTTP 5xx: severity beats the observed-fact tier.
        assert all(f.severity == "low" for f in cards[1:])
    else:
        assert all(f.severity == "low" for f in cards)
    for rule in (OBSERVED, TITLE):
        ids = [f.id for f in cards if f.rule_id == rule]
        assert len(ids) == 2 and ids == sorted(ids)  # Final tie-break within each tier.
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert (canonical_json_bytes(manifest), canonical_json_bytes(value)) == before
