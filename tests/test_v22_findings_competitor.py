from datetime import timedelta
import pytest

from app.report_v22.findings import build_public_findings
from app.report_v22.public_rule_catalog import ASSET
from findings_helpers import collection, market, request, site, seal


@pytest.mark.parametrize("types,expected",[(('home','home','home'),0),(('service_detail','home','home'),0),(('service_detail','service_detail','home'),1)])
def test_two_distinct_competitor_threshold(types,expected):
    serp = market()
    result = build_public_findings(request(site(),serp,collection(serp,types)))
    findings = [f for f in result.findings if f.rule_id == ASSET]
    assert len(findings) == expected
    if findings:
        assert findings[0].classification == "inference" and findings[0].confidence == "low"


@pytest.mark.parametrize("hours,expected",[(24,1),(24.01,0)])
def test_actual_nested_inventory_time_controls_comparison(hours,expected):
    serp = market()
    source = collection(serp)
    inventory = source.payload.competitors[0].site_inventory
    inventory.started_at -= timedelta(hours=hours)
    inventory.completed_at -= timedelta(hours=hours)
    result = build_public_findings(request(site(),serp,seal(source)))
    assert sum(f.rule_id == ASSET for f in result.findings) == expected
