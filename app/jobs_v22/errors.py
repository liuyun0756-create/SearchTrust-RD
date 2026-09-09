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


class IdempotencyConflict(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "IDEMPOTENCY_CONFLICT",
            "The idempotency key is already associated with a different request.",
        )


class JobIdentityConflict(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "JOB_IDENTITY_CONFLICT",
            "The job identifier is already associated with a different task.",
        )


class JobNotFound(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("JOB_NOT_FOUND", "The requested task was not found.")


class JobNotRetryable(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("JOB_NOT_RETRYABLE", "The task is not eligible for a manual retry.")


class JobNewAttemptRequired(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "JOB_NEW_ATTEMPT_REQUIRED",
            "Generate again by creating a new task with one account credit.",
        )


class InvalidJobTransition(DeterministicJobError):
    def __init__(self, message: str = "The task state transition is not allowed.") -> None:
        super().__init__("INVALID_JOB_TRANSITION", message)


class JobLeaseLost(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("JOB_LEASE_LOST", "This task run was superseded by recovery.")


class JobDeadlineExceeded(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__("JOB_DEADLINE_EXCEEDED", "The analysis exceeded its processing deadline.")


class ProviderCircuitOpen(TransientJobError):
    def __init__(self) -> None:
        super().__init__(
            "PROVIDER_CIRCUIT_OPEN",
            "A data provider is cooling down after repeated temporary failures.",
        )


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
