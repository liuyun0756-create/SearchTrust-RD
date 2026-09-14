import hashlib
import json
from pathlib import Path

import pytest

from app.jobs_v22.digest import (
    canonical_json_bytes,
    request_digest,
    verified_canonical_json_bytes,
    verified_request_digest,
)
from app.report_v22.models import ReportV22


@pytest.mark.parametrize(("value", "expected"), [
    (1.0, "1"), (-0.0, "0"), (1e-6, "0.000001"), (1e-7, "1e-7"),
    (1e20, "100000000000000000000"), (1e21, "1e+21"),
    (333333333.33333329, "333333333.3333333"), (0.1 + 0.2, "0.30000000000000004"),
    (1000000000000000128, "1000000000000000100"),
    (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"),
    (-1e20, "-100000000000000000000"), (-1e-7, "-1e-7"),
    (295147905179352825856.0, "295147905179352830000"),
    (999999999999999900000.0, "999999999999999900000"),
    (1.0000000000000001e23, "1.0000000000000001e+23"),
    (9.999999999999997e22, "9.999999999999997e+22"),
])
def test_verified_numbers_match_ecmascript_canonical_bytes_and_hash(value, expected):
    encoded = expected.encode("utf-8")
    assert verified_canonical_json_bytes(value) == encoded
    assert verified_request_digest(value) == f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_verified_digest_matches_modified_schema_valid_frontend_prospect():
    report = json.loads((Path(__file__).resolve().parents[1] / "contracts/v2.2/fixtures/prospect.json").read_text())
    report["case_context"]["target_market"].update(latitude=1.0, longitude=-0.0)
    report["market_snapshot"]["target_market"].update(latitude=1.0, longitude=-0.0)
    report["identity"]["business"]["primary_location"].update(latitude=1e-6, longitude=-0.0)
    ReportV22.model_validate_json(json.dumps(report))
    # Fixed hash from frontend canonicalDigest with these exact schema-valid edits.
    expected = "sha256:b976d1b3fba055fdb91c0a3e31477cb515f2add186831d8779741c2ead0e2c3c"
    assert verified_request_digest(report) == expected
    assert request_digest(report) != expected


def test_verified_digest_sorts_utf16_keys_and_preserves_array_order():
    value = {"\ufb33": 1.0, "😀": [-0.0, 1e-7], "10": True, "2": None}
    expected = '{"10":true,"2":null,"😀":[0,1e-7],"דּ":1}'.encode()
    assert verified_canonical_json_bytes(value) == expected
    assert verified_request_digest([1, 2]) != verified_request_digest([2, 1])


def test_legacy_checkpoint_digest_encoding_is_unchanged():
    value = {"a": 1.0, "b": -0.0, "c": 1e-7}
    assert canonical_json_bytes(value) == b'{"a":1.0,"b":-0.0,"c":1e-7}'
    assert request_digest(value) == f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"
    assert verified_canonical_json_bytes(value) == b'{"a":1,"b":0,"c":1e-7}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 10**400, {1: "not-string"}, {"a": object()}, (1, 2), {1, 2}])
def test_verified_digest_rejects_non_json_or_nonfinite_values(value):
    with pytest.raises((TypeError, ValueError)):
        verified_request_digest(value)


def test_verified_digest_rejects_cycles_but_accepts_shared_values():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="acyclic"):
        verified_request_digest(cycle)
    shared = {"a": 1}
    assert verified_request_digest([shared, shared]) == verified_request_digest([{"a": 1}, {"a": 1}])
