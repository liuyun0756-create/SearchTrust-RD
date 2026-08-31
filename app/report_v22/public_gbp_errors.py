"""Safe deterministic errors for the offline public profile boundary."""
from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


class PublicGbpError(DeterministicJobError):
    def __init__(self, kind: Literal["INPUT_INVALID", "REFERENCE_INVALID", "LIMIT_EXCEEDED"]):
        super().__init__(f"V22_PUBLIC_GBP_{kind}", {
            "INPUT_INVALID": "Public customer profile input is invalid.",
            "REFERENCE_INVALID": "Public customer profile reference is inconsistent.",
            "LIMIT_EXCEEDED": "Public customer profile exceeds its resource limit.",
        }[kind])
