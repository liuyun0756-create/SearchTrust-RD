"""Case-local storage identity, distinct from the immutable shared source ID."""
from uuid import NAMESPACE_URL, UUID, uuid3


def case_market_binding_id(case_id: UUID, source_id: UUID) -> UUID:
    # UUIDv3 is only an identity derivation; SHA-256 still validates all content.
    return uuid3(NAMESPACE_URL, f"searchtrust:capability:serp-binding:{case_id}:{source_id}")
