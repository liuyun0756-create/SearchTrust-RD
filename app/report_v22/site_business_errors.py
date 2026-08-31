"""Payload-free errors for the offline website candidate boundary."""
from app.jobs_v22.errors import DeterministicJobError


class SiteBusinessError(DeterministicJobError):
    def __init__(self, kind):
        messages = {
            "INPUT_INVALID": "Website candidate input is invalid.",
            "BINDING_INVALID": "Website snapshot binding is invalid.",
            "CHECKSUM_MISMATCH": "Website snapshot checksum does not match.",
            "REFERENCE_INVALID": "Website candidate references are invalid.",
            "ID_CONFLICT": "Website candidate identity has conflicting observations.",
            "LIMIT_EXCEEDED": "Website candidate extraction exceeds its resource limit.",
        }
        super().__init__(f"V22_SITE_FACTS_{kind}", messages[kind])


def require(condition, kind="BINDING_INVALID"):
    if not condition:
        raise SiteBusinessError(kind)
