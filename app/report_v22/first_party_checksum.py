"""Canonical source-set checksum shared by first-party consumer boundaries."""

from __future__ import annotations

from app.jobs_v22.digest import request_digest


def semantic_first_party_input_checksum(value) -> str:
    """Bind the same GSC/GA4/GBP source set independent of traversal order."""
    payload = value.model_dump(mode="json")
    payload["snapshots"] = sorted(payload["snapshots"], key=lambda item: item["source_type"])
    return request_digest(payload)
