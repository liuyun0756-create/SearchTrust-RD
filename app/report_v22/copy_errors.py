"""Stable, payload-free failures for the v2.2 controlled copy boundary."""

from __future__ import annotations

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError, TransientJobError


CopyInputErrorKind = Literal[
    "INPUT_INVALID",
    "CHECKSUM_MISMATCH",
    "CATALOG_INVALID",
    "LIMIT_EXCEEDED",
    "ATTEMPTS_INVALID",
]

CopyOutputErrorKind = Literal[
    "OUTPUT_MISSING",
    "OUTPUT_INVALID",
    "CHECKSUM_MISMATCH",
    "REFERENCE_INVALID",
    "LANGUAGE_INVALID",
    "LIMIT_EXCEEDED",
]


class PublicCopyInputError(DeterministicJobError):
    """A local contract failure that another Dify call cannot repair."""

    def __init__(self, kind: CopyInputErrorKind) -> None:
        super().__init__(
            f"V22_COPY_{kind}",
            {
                "INPUT_INVALID": "The public copy input is invalid.",
                "CHECKSUM_MISMATCH": "The public copy input does not match its bound checksum.",
                "CATALOG_INVALID": "The public copy catalog does not support this action plan.",
                "LIMIT_EXCEEDED": "The public copy input exceeds its resource limit.",
                "ATTEMPTS_INVALID": "The public copy attempt limit is invalid.",
            }[kind],
        )


class PublicCopyOutputError(TransientJobError):
    """A rejected provider result that may be repaired by a bounded retry."""

    def __init__(
        self,
        kind: CopyOutputErrorKind,
        details: list[str] | tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            f"V22_COPY_{kind}",
            {
                "OUTPUT_MISSING": "The copy provider did not return a result.",
                "OUTPUT_INVALID": "The copy provider returned an invalid result.",
                "CHECKSUM_MISMATCH": "The copy provider returned a result for another action plan.",
                "REFERENCE_INVALID": "The copy provider referenced an unsupported fact or pattern.",
                "LANGUAGE_INVALID": "The copy provider returned the wrong language contract.",
                "LIMIT_EXCEEDED": "The copy provider result exceeds its resource limit.",
            }[kind],
        )
        self.details = tuple(details[:20])


class PublicCopyProviderTransientError(TransientJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_COPY_PROVIDER_TRANSIENT",
            "The copy provider is temporarily unavailable.",
        )


class PublicCopyProviderPermanentError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_COPY_PROVIDER_REJECTED",
            "The copy provider rejected the configured request.",
        )


class PublicCopyRetryExhaustedError(TransientJobError):
    """All in-call attempts failed without producing safe client copy."""

    def __init__(self, error_codes: list[str] | tuple[str, ...]) -> None:
        super().__init__(
            "V22_COPY_RETRY_EXHAUSTED",
            "Safe client copy could not be generated after the allowed attempts.",
        )
        self.details = tuple(error_codes[:3])
