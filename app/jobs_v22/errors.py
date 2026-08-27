"""Stable v2.2 job failures and retry classification."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import ValidationError


@dataclass(frozen=True)
class ClassifiedJobError:
    error_code: str
    user_message: str
    retryable: bool


class DurableJobError(RuntimeError):
    """Base exception whose public code and message are safe to persist."""

    retryable = False

    def __init__(self, error_code: str, user_message: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.user_message = user_message


class DeterministicJobError(DurableJobError):
    retryable = False


class TransientJobError(DurableJobError):
    retryable = True


def classify_job_exception(exc: BaseException) -> ClassifiedJobError:
    """Map arbitrary failures to stable, non-sensitive task errors."""

    if isinstance(exc, DurableJobError):
        return ClassifiedJobError(exc.error_code, exc.user_message, exc.retryable)
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return ClassifiedJobError(
            "PROVIDER_TEMPORARILY_UNAVAILABLE",
            "A data provider is temporarily unavailable. The task will be retried.",
            True,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or status >= 500:
            return ClassifiedJobError(
                "PROVIDER_TEMPORARILY_UNAVAILABLE",
                "A data provider is temporarily unavailable. The task will be retried.",
                True,
            )
        return ClassifiedJobError(
            "PROVIDER_REQUEST_REJECTED",
            "A data provider rejected the request.",
            False,
        )
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return ClassifiedJobError(
            "V22_INPUT_INVALID",
            "The analysis request is invalid.",
            False,
        )
    return ClassifiedJobError(
        "V22_INTERNAL_ERROR",
        "The analysis could not be completed because of a temporary internal error.",
        True,
    )
