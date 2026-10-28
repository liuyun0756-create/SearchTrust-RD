"""Source creation is confined to the isolated browser integration harness."""
from datetime import datetime, timezone

import pytest

from support.first_service_delivery_bridge import seed
from support.first_service_entry_bridge import freshly_collected, SourceStage
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import TransientJobError


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("clean", [False, True])
def test_new_synthetic_collection_does_not_mutate_stored_private_or_public_sources(clean):
    _, value = seed(clean)
    before = canonical_json_bytes(value)
    timestamp = datetime.now(timezone.utc)
    for source in value.public_input.evidence_input.sources:
        fresh = freshly_collected(source.payload, timestamp)
        assert fresh.started_at == timestamp and fresh.completed_at == timestamp
        if hasattr(fresh, "expires_at"):
            assert fresh.expires_at > timestamp
    assert canonical_json_bytes(value) == before


@pytest.mark.anyio
async def test_provider_fault_is_once_and_does_not_fabricate_a_result():
    stage = SourceStage(lambda _: "collected", fail_once=True)
    with pytest.raises(TransientJobError):
        await stage.collect()
    assert stage.calls == 1
    assert await stage.collect() == "collected" and stage.calls == 2
