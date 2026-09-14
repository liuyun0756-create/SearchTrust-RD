from __future__ import annotations

import logging
from urllib.parse import quote, quote_plus

import httpx
import pytest

from app.integrations.serpapi import (
    SerpApiKeyState,
    SerpApiKeysUnavailable,
    configured_serpapi_keys,
    execute_serpapi_get,
    serpapi_key_fingerprint,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_configured_serpapi_keys_removes_blanks_and_duplicates() -> None:
    assert configured_serpapi_keys(" primary ", "", "primary") == ["primary"]
    assert configured_serpapi_keys("primary", "secondary", "tertiary") == [
        "primary",
        "secondary",
        "tertiary",
    ]


@pytest.mark.anyio
async def test_execute_serpapi_get_reports_safe_attempt_metadata() -> None:
    attempts: list[tuple[int, str]] = []

    async def before_attempt(slot: int, fingerprint: str) -> None:
        attempts.append((slot, fingerprint))

    state = SerpApiKeyState()
    client = _FakeClient([
        _FakeResponse({"error": "Invalid API key."}, status_code=403),
        _FakeResponse({"search_metadata": {"status": "Success"}, "local_results": []}),
    ])

    result = await execute_serpapi_get(
        client,
        {"engine": "google_maps", "q": "plumber"},
        keys=["primary-secret", "secondary-secret"],
        base_url="https://serpapi.example/search",
        state=state,
        before_attempt=before_attempt,
        logger=logging.getLogger("tests.serpapi"),
    )

    assert result.payload["search_metadata"]["status"] == "Success"
    assert result.metadata.attempt_count == 2
    assert result.metadata.key_slot == 2
    assert attempts == [
        (1, serpapi_key_fingerprint("primary-secret")),
        (2, serpapi_key_fingerprint("secondary-secret")),
    ]
    assert state.active_fingerprint == serpapi_key_fingerprint("secondary-secret")
    assert all("secret" not in fingerprint for _, fingerprint in attempts)


@pytest.mark.anyio
async def test_execute_serpapi_get_does_not_fail_over_transport_errors() -> None:
    client = _FakeClient([RuntimeError("network includes primary-secret")])

    with pytest.raises(RuntimeError, match="transport request failed") as raised:
        await execute_serpapi_get(
            client,
            {"engine": "google"},
            keys=["primary-secret", "secondary-secret"],
            base_url="https://serpapi.example/search",
        )

    assert len(client.requests) == 1
    assert "primary-secret" not in str(raised.value)


@pytest.mark.anyio
async def test_execute_serpapi_get_all_keys_unavailable_is_secret_safe() -> None:
    secrets = ["primary-secret", "secondary-secret", "tertiary-secret"]
    client = _FakeClient([
        _FakeResponse({"error": "No searches remaining."}, status_code=429),
        _FakeResponse({"error": "No searches remaining."}, status_code=429),
        _FakeResponse({"error": "No searches remaining."}, status_code=429),
    ])

    with pytest.raises(SerpApiKeysUnavailable) as raised:
        await execute_serpapi_get(
            client,
            {"engine": "google"},
            keys=secrets,
            base_url="https://serpapi.example/search",
        )

    assert len(client.requests) == 3
    assert all(secret not in str(raised.value) for secret in secrets)


@pytest.mark.anyio
@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
async def test_http_client_logs_redact_all_key_variants_on_success_and_failure(
    caplog: pytest.LogCaptureFixture, level: int,
) -> None:
    secrets = ["first-secret", "second secret/+", "third%secret"]
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": f"api_key={secrets[calls - 1]} exhausted"})
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}})

    caplog.set_level(level, logger="httpx")
    caplog.set_level(level, logger="httpcore")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await execute_serpapi_get(
            client, {"engine": "google_maps", "q": "plumber"}, keys=secrets,
            base_url="https://serpapi.example/search",
        )

    assert result.metadata.attempt_count == 3
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    for secret in secrets:
        assert secret not in rendered
        assert quote(secret, safe="") not in rendered
        assert quote_plus(secret) not in rendered
    assert "api_key=[REDACTED]" in rendered


def test_serpapi_log_safety_does_not_change_unrelated_application_logs(caplog) -> None:
    from app.integrations.serpapi import install_serpapi_log_safety

    install_serpapi_log_safety()
    caplog.set_level(logging.INFO, logger="tests.unrelated")
    logging.getLogger("tests.unrelated").info("api_key=ordinary-application-value")
    assert "api_key=ordinary-application-value" in caplog.text


def test_serpapi_log_safety_redacts_payload_and_httpcore_exception(caplog) -> None:
    from app.integrations.serpapi import install_serpapi_log_safety

    install_serpapi_log_safety()
    caplog.set_level(logging.DEBUG, logger="httpcore.connection")
    try:
        raise RuntimeError("api_key=exception-secret")
    except RuntimeError:
        logging.getLogger("httpcore.connection").exception(
            'payload={"api_key": "payload-secret"}')
    assert "exception-secret" not in caplog.text
    assert "payload-secret" not in caplog.text
    assert '"api_key": "[REDACTED]"' in caplog.text
