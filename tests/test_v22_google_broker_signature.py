import json
from pathlib import Path

from app.google_connections_v22 import sign_google_broker_body


def test_google_broker_signature_matches_typescript_fixture():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "v22_google_broker_signature.json").read_text(encoding="utf-8")
    )
    assert sign_google_broker_body(
        fixture["secret"],
        timestamp=fixture["timestamp"],
        request_id=fixture["request_id"],
        nonce=fixture["nonce"],
        body=fixture["body"],
        version=fixture["version"],
    ) == fixture["signature"]
