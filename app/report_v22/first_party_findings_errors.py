"""Payload-free deterministic failures for first-party Findings."""

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


FirstPartyFindingsErrorKind = Literal[
    "INPUT_INVALID",
    "BINDING_INVALID",
    "PARENT_REPORT_INVALID",
    "CHECKSUM_MISMATCH",
    "CONTENT_EXPIRED",
    "REFERENCE_INVALID",
    "ID_CONFLICT",
    "LIMIT_EXCEEDED",
]


class FirstPartyFindingsError(DeterministicJobError):
    def __init__(self, kind: FirstPartyFindingsErrorKind) -> None:
        messages = {
            "INPUT_INVALID": "First-party findings input is invalid or ambiguous.",
            "BINDING_INVALID": "A first-party snapshot is no longer bound to this Case.",
            "PARENT_REPORT_INVALID": "The prospect report is not a valid parent for this Case.",
            "CHECKSUM_MISMATCH": "First-party source content does not match its immutable checksum.",
            "CONTENT_EXPIRED": "Official GBP content has expired and must be synchronized again.",
            "REFERENCE_INVALID": "First-party finding references are inconsistent.",
            "ID_CONFLICT": "First-party finding identity has conflicting content.",
            "LIMIT_EXCEEDED": "First-party findings output exceeds its resource limit.",
        }
        super().__init__(f"V22_FIRST_PARTY_FINDINGS_{kind}", messages[kind])
