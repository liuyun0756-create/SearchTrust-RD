"""Payload-free deterministic failures for V22-073 version differences."""

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


VersionDiffErrorKind = Literal[
    "INPUT_INVALID",
    "BINDING_INVALID",
    "CHECKSUM_MISMATCH",
    "UPSTREAM_MISMATCH",
    "PARENT_MISMATCH",
    "FINGERPRINT_MISMATCH",
    "REFERENCE_INVALID",
    "ID_CONFLICT",
    "UNSUPPORTED_CHANGE",
    "LIMIT_EXCEEDED",
]


class VersionDiffError(DeterministicJobError):
    def __init__(self, kind: VersionDiffErrorKind) -> None:
        messages = {
            "INPUT_INVALID": "Version difference input is invalid or ambiguous.",
            "BINDING_INVALID": "Version difference inputs are not bound to the same Case, report, or evaluation.",
            "CHECKSUM_MISMATCH": "A supplied parent or upstream value was modified.",
            "UPSTREAM_MISMATCH": "The supplied verified reprioritization cannot be reproduced.",
            "PARENT_MISMATCH": "The parent report does not match its bound public findings and actions.",
            "FINGERPRINT_MISMATCH": "A parent Finding fingerprint could not be verified.",
            "REFERENCE_INVALID": "A version difference reference is inconsistent.",
            "ID_CONFLICT": "A current Finding, Evidence, or audit identity has conflicting content.",
            "UNSUPPORTED_CHANGE": "A requested version change is not supported by this ruleset.",
            "LIMIT_EXCEEDED": "Version difference generation exceeds its resource limit.",
        }
        super().__init__(f"V22_VERSION_DIFF_{kind}", messages[kind])
