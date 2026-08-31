"""Deterministic, payload-free errors at the evidence builder boundary."""

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError

ErrorKind = Literal["SOURCE_INVALID", "SNAPSHOT_BINDING_INVALID", "CHECKSUM_MISMATCH", "ID_CONFLICT", "LIMIT_EXCEEDED"]


class EvidenceError(DeterministicJobError):
    def __init__(self, kind: ErrorKind) -> None:
        super().__init__(f"V22_EVIDENCE_{kind}", {
            "SOURCE_INVALID": "Evidence source input is invalid.",
            "SNAPSHOT_BINDING_INVALID": "Evidence snapshot binding is invalid.",
            "CHECKSUM_MISMATCH": "Evidence snapshot checksum does not match.",
            "ID_CONFLICT": "Evidence identity has conflicting observations.",
            "LIMIT_EXCEEDED": "Evidence build exceeds its resource limit.",
        }[kind])
