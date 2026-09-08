"""Payload-free deterministic failures for V22-072 verified reprioritization."""

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


VerifiedReprioritizationErrorKind = Literal[
    "INPUT_INVALID",
    "BINDING_INVALID",
    "CHECKSUM_MISMATCH",
    "UPSTREAM_MISMATCH",
    "UNSUPPORTED_RULE",
    "REFERENCE_INVALID",
    "ID_CONFLICT",
    "DEPENDENCY_INVALID",
    "INSUFFICIENT_ACTIONS",
    "LIMIT_EXCEEDED",
]


class VerifiedReprioritizationError(DeterministicJobError):
    def __init__(self, kind: VerifiedReprioritizationErrorKind) -> None:
        messages = {
            "INPUT_INVALID": "Verified reprioritization input is invalid or ambiguous.",
            "BINDING_INVALID": "Verified inputs are not bound to the same Case, report, or evaluation.",
            "CHECKSUM_MISMATCH": "A supplied upstream input or result was modified.",
            "UPSTREAM_MISMATCH": "A supplied upstream result cannot be reproduced.",
            "UNSUPPORTED_RULE": "A verified Finding rule is not supported by this ruleset.",
            "REFERENCE_INVALID": "Verified action or Finding references are inconsistent.",
            "ID_CONFLICT": "A verified action or relation identity has conflicting content.",
            "DEPENDENCY_INVALID": "Verified action dependencies cannot be ordered safely.",
            "INSUFFICIENT_ACTIONS": "Fewer than three safe verified actions are available.",
            "LIMIT_EXCEEDED": "Verified reprioritization exceeds its resource limit.",
        }
        super().__init__(f"V22_VERIFIED_REPRIORITIZATION_{kind}", messages[kind])
