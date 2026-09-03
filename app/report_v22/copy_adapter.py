"""Bounded provider orchestration for v2.2 controlled public copy."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

import httpx

from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.copy_contract import validate_and_render_copy, validate_copy_request
from app.report_v22.copy_errors import (
    PublicCopyInputError,
    PublicCopyOutputError,
    PublicCopyProviderPermanentError,
    PublicCopyProviderTransientError,
    PublicCopyRetryExhaustedError,
)
from app.report_v22.copy_models import CopyRequestV1, PublicActionCopyResult, PublicCopyLimits


class CopyProvider(Protocol):
    async def __call__(self, request: CopyRequestV1) -> object: ...


Backoff = Callable[[int], Awaitable[None]]


def _provider_error(exc: Exception) -> PublicCopyProviderTransientError | PublicCopyProviderPermanentError:
    if isinstance(exc, PublicCopyProviderTransientError):
        return exc
    if isinstance(exc, PublicCopyProviderPermanentError):
        return exc
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return PublicCopyProviderTransientError()
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429 or exc.response.status_code >= 500:
            return PublicCopyProviderTransientError()
        return PublicCopyProviderPermanentError()
    return PublicCopyProviderPermanentError()


async def generate_public_action_copy(
    request: CopyRequestV1,
    provider: CopyProvider,
    *,
    max_attempts: int = 3,
    limits: PublicCopyLimits | None = None,
    backoff: Backoff | None = None,
) -> PublicActionCopyResult:
    """Call a provider at most three times and never return unsafe fallback copy."""
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise PublicCopyInputError("ATTEMPTS_INVALID")
    checked = validate_copy_request(request, limits=limits)
    request_bytes = canonical_json_bytes(checked)
    errors: list[str] = []

    for attempt in range(1, max_attempts + 1):
        attempt_request = CopyRequestV1.model_validate_json(request_bytes)
        try:
            outputs = await provider(attempt_request)
        except Exception as exc:
            mapped = _provider_error(exc)
            if isinstance(mapped, PublicCopyProviderPermanentError):
                raise mapped from None
            errors.append(mapped.error_code)
        else:
            try:
                result = validate_and_render_copy(checked, outputs, limits=limits)
            except PublicCopyOutputError as exc:
                errors.append(exc.error_code)
            else:
                return result.model_copy(update={"attempt_count": attempt})

        if attempt < max_attempts and backoff is not None:
            await backoff(attempt)

    raise PublicCopyRetryExhaustedError(errors)
