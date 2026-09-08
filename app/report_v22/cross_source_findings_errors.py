"""Payload-free deterministic failures for V22-071 cross-source Findings."""

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


CrossSourceFindingsErrorKind = Literal[
    "INPUT_INVALID",
    "BINDING_INVALID",
    "CHECKSUM_MISMATCH",
    "REFERENCE_INVALID",
    "ID_CONFLICT",
    "LIMIT_EXCEEDED",
]


class CrossSourceFindingsError(DeterministicJobError):
    def __init__(self, kind: CrossSourceFindingsErrorKind) -> None:
        messages = {
            "INPUT_INVALID": "Cross-source findings input is invalid or ambiguous.",
            "BINDING_INVALID": "Cross-source inputs are not bound to the same Case and report.",
            "CHECKSUM_MISMATCH": "The supplied first-party findings input or result was modified.",
            "REFERENCE_INVALID": "Cross-source finding references are inconsistent.",
            "ID_CONFLICT": "Cross-source finding identity has conflicting content.",
            "LIMIT_EXCEEDED": "Cross-source findings output exceeds its resource limit.",
        }
        super().__init__(f"V22_CROSS_SOURCE_FINDINGS_{kind}", messages[kind])
