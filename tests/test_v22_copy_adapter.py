from __future__ import annotations

from copy import deepcopy

import httpx
import pytest

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.copy_adapter import generate_public_action_copy
from app.report_v22.copy_errors import (
    PublicCopyInputError,
    PublicCopyProviderPermanentError,
    PublicCopyProviderTransientError,
    PublicCopyRetryExhaustedError,
)
from v22_copy_helpers import copy_request, valid_response


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_first_valid_attempt_returns_three_actions_without_retry() -> None:
    request_value = copy_request()
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        return valid_response(request)

    result = await generate_public_action_copy(request_value, provider)
    assert result.attempt_count == 1
    assert len(result.actions) == 3
    assert calls == 1


@pytest.mark.anyio
async def test_two_invalid_outputs_then_success_use_identical_requests() -> None:
    request_value = copy_request()
    seen: list[bytes] = []
    waits: list[int] = []

    async def provider(request):
        seen.append(canonical_json_bytes(request))
        if len(seen) < 3:
            outputs = valid_response(request)
            outputs["action_copy_v2_2"]["actions"][0]["why_now"]["free_text"] = "unsafe"
            return outputs
        return valid_response(request)

    async def backoff(attempt: int) -> None:
        waits.append(attempt)

    result = await generate_public_action_copy(
        request_value,
        provider,
        backoff=backoff,
    )
    assert result.attempt_count == 3
    assert len(set(seen)) == 1
    assert seen[0] == canonical_json_bytes(request_value)
    assert waits == [1, 2]


@pytest.mark.anyio
async def test_three_invalid_outputs_exhaust_without_partial_or_fallback_copy() -> None:
    request_value = copy_request()
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        return {"action_copy_v2_2": {"free_text": "invented"}}

    with pytest.raises(PublicCopyRetryExhaustedError) as exc:
        await generate_public_action_copy(request_value, provider)

    assert calls == 3
    assert exc.value.error_code == "V22_COPY_RETRY_EXHAUSTED"
    assert exc.value.details == ("V22_COPY_OUTPUT_INVALID",) * 3
    assert not hasattr(exc.value, "actions")


@pytest.mark.anyio
async def test_transient_provider_failures_retry_but_permanent_failures_do_not() -> None:
    request_value = copy_request()
    calls = 0

    async def transient_then_valid(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PublicCopyProviderTransientError()
        return valid_response(request)

    result = await generate_public_action_copy(request_value, transient_then_valid)
    assert result.attempt_count == 3
    assert calls == 3

    calls = 0

    async def permanent(_request):
        nonlocal calls
        calls += 1
        raise PublicCopyProviderPermanentError()

    with pytest.raises(PublicCopyProviderPermanentError):
        await generate_public_action_copy(request_value, permanent)
    assert calls == 1


@pytest.mark.anyio
async def test_http_errors_are_classified_without_exposing_response_content() -> None:
    request_value = copy_request()
    calls = 0

    async def unauthorized(_request):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://dify.invalid/workflows/run")
        response = httpx.Response(401, request=request, text="secret provider body")
        raise httpx.HTTPStatusError("secret provider body", request=request, response=response)

    with pytest.raises(PublicCopyProviderPermanentError) as exc:
        await generate_public_action_copy(request_value, unauthorized)
    assert calls == 1
    assert "secret" not in str(exc.value).lower()

    calls = 0

    async def throttled(_request):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://dify.invalid/workflows/run")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("rate limit", request=request, response=response)

    with pytest.raises(PublicCopyRetryExhaustedError):
        await generate_public_action_copy(request_value, throttled, max_attempts=2)
    assert calls == 2


@pytest.mark.anyio
async def test_invalid_attempt_limit_and_local_request_failure_never_call_provider() -> None:
    request_value = copy_request()
    calls = 0

    async def provider(_request):
        nonlocal calls
        calls += 1
        return None

    for value in (0, 4, True):
        with pytest.raises(PublicCopyInputError) as exc:
            await generate_public_action_copy(request_value, provider, max_attempts=value)
        assert exc.value.error_code == "V22_COPY_ATTEMPTS_INVALID"

    request_value.actions[0].facts[0].value = "tampered"
    with pytest.raises(PublicCopyInputError):
        await generate_public_action_copy(request_value, provider)
    assert calls == 0


@pytest.mark.anyio
async def test_provider_cannot_mutate_the_original_or_later_attempt_requests() -> None:
    request_value = copy_request()
    original = canonical_json_bytes(request_value)
    seen: list[bytes] = []

    async def provider(request):
        seen.append(canonical_json_bytes(request))
        if len(seen) == 1:
            request.actions[0].facts[0].value = "provider mutation"
            return None
        return valid_response(request)

    result = await generate_public_action_copy(request_value, provider, max_attempts=2)
    assert result.attempt_count == 2
    assert canonical_json_bytes(request_value) == original
    assert seen == [original, original]


@pytest.mark.anyio
async def test_unknown_provider_exception_is_permanent_and_sanitized() -> None:
    async def provider(_request):
        raise RuntimeError("customer URL and secret key")

    with pytest.raises(PublicCopyProviderPermanentError) as exc:
        await generate_public_action_copy(copy_request(), provider)
    assert "customer" not in str(exc.value).lower()
    assert "secret" not in str(exc.value).lower()
