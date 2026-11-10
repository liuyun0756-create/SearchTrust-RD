"""Normalize new collection results before they acquire canonical identities.

This is deliberately separate from digest/checkpoint readers: existing evidence
must continue to be verified against the bytes originally collected.
"""
import logging
from typing import Any, Literal
from uuid import UUID

from app.jobs_v22.errors import DeterministicJobError

SourceKind = Literal["site", "serp", "competitor", "public_gbp", "rendered"]
logger = logging.getLogger(__name__)
SOURCE_KINDS = frozenset({"site", "serp", "competitor", "public_gbp", "rendered"})


class CollectionJsonKeyCollision(DeterministicJobError):
    def __init__(self):
        super().__init__("COLLECTION_JSON_KEY_COLLISION",
            "The collected evidence contains conflicting field names.")


def normalize_collection_json(value: Any, *, source: SourceKind, job_id: UUID) -> Any:
    """Remove actual NULs, including keys, without mutating the provider result.

    Literal backslash-u0000 text is not a NUL. Raw response/body digests remain
    provenance of the original network bytes, rather than normalized payloads.
    """
    if source not in SOURCE_KINDS or not isinstance(job_id, UUID):
        raise ValueError("Invalid collection normalization context")
    count = 0

    def clean(item):
        nonlocal count
        if isinstance(item, str):
            removed = item.count("\0")
            count += removed
            return item.replace("\0", "") if removed else item
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                normalized_key = clean(key)
                if normalized_key in result:
                    raise CollectionJsonKeyCollision()
                result[normalized_key] = clean(child)
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, tuple):
            return tuple(clean(child) for child in item)
        return item

    result = clean(value)
    if count:
        logger.info("collection JSON normalized source=%s removed_characters=%d job_id_suffix=%s",
            source, count, str(job_id)[-8:])
    return result if count else value
