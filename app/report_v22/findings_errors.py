"""Payload-free deterministic failures at the public Findings boundary."""
from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


class FindingsError(DeterministicJobError):
    def __init__(self, kind: Literal["INPUT_INVALID", "REFERENCE_INVALID", "ID_CONFLICT", "LIMIT_EXCEEDED"]):
        super().__init__(f"V22_FINDINGS_{kind}", {
            "INPUT_INVALID": "Public findings input is invalid or ambiguous.",
            "REFERENCE_INVALID": "Public findings references are inconsistent.",
            "ID_CONFLICT": "Public finding identity has conflicting content.",
            "LIMIT_EXCEEDED": "Public findings output exceeds its resource limit.",
        }[kind])
