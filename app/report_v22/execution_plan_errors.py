"""Payload-free deterministic failures for V22-074 execution plans."""

from typing import Literal

from app.jobs_v22.errors import DeterministicJobError


ExecutionPlanErrorKind = Literal[
    "INPUT_INVALID",
    "BINDING_INVALID",
    "CHECKSUM_MISMATCH",
    "UPSTREAM_MISMATCH",
    "BASELINE_UNAVAILABLE",
    "METRIC_TARGET_MISMATCH",
    "REFERENCE_INVALID",
    "ID_CONFLICT",
    "DEPENDENCY_INVALID",
    "PRIVACY_VIOLATION",
    "LIMIT_EXCEEDED",
]


class ExecutionPlanError(DeterministicJobError):
    def __init__(self, kind: ExecutionPlanErrorKind) -> None:
        messages = {
            "INPUT_INVALID": "Verified execution plan input is invalid or ambiguous.",
            "BINDING_INVALID": "Verified execution inputs are not bound to the same Case, report, or evaluation.",
            "CHECKSUM_MISMATCH": "A supplied verified execution input or result was modified.",
            "UPSTREAM_MISMATCH": "A supplied upstream result cannot be reproduced.",
            "BASELINE_UNAVAILABLE": "A required verified baseline is unavailable.",
            "METRIC_TARGET_MISMATCH": "A selected metric is not bound to the action target.",
            "REFERENCE_INVALID": "Verified execution references are inconsistent.",
            "ID_CONFLICT": "A Finding, Evidence, Action, or audit identity has conflicting content.",
            "DEPENDENCY_INVALID": "The verified action roadmap contains an unsafe dependency.",
            "PRIVACY_VIOLATION": "The verified execution result contains prohibited data.",
            "LIMIT_EXCEEDED": "Verified execution planning exceeds its resource limit.",
        }
        super().__init__(f"V22_EXECUTION_PLAN_{kind}", messages[kind])
