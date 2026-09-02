"""Payload-free deterministic failures for v2.2 public action planning."""

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


ActionErrorKind = Literal[
    "INPUT_INVALID",
    "REFERENCE_INVALID",
    "UNSUPPORTED_FINDING",
    "INSUFFICIENT_ACTIONABLE_FINDINGS",
    "ID_CONFLICT",
    "DEPENDENCY_INVALID",
    "LIMIT_EXCEEDED",
    "DATE_INVALID",
]


class PublicActionError(DeterministicJobError):
    def __init__(self, kind: ActionErrorKind) -> None:
        super().__init__(
            f"V22_ACTIONS_{kind}",
            {
                "INPUT_INVALID": "Public action planning input is invalid.",
                "REFERENCE_INVALID": "Public action references are inconsistent.",
                "UNSUPPORTED_FINDING": "A public finding does not have an approved action mapping.",
                "INSUFFICIENT_ACTIONABLE_FINDINGS": "There is not enough supported evidence to produce three actions.",
                "ID_CONFLICT": "Public action identity has conflicting content.",
                "DEPENDENCY_INVALID": "Public action dependencies are invalid.",
                "LIMIT_EXCEEDED": "Public action planning exceeds its resource limit.",
                "DATE_INVALID": "Public action review dates could not be calculated.",
            }[kind],
        )
