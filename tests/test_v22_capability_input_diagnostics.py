"""Preparation diagnostics reveal code locations, never exception values."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jobs_v22 import capability_input_repository as repository_module
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.errors import DeterministicJobError
from test_v22_capability_public import inputs


@pytest.fixture
def anyio_backend():
    return "asyncio"


def repository():
    result = object.__new__(SupabaseCapabilityInputRepository)
    result.rpc = SimpleNamespace(post=AsyncMock())
    return result


@pytest.mark.anyio
@pytest.mark.parametrize("kind", [ValueError, TypeError, AttributeError, OverflowError, RecursionError])
async def test_prepare_logs_exception_type_and_location_without_its_value(monkeypatch, caplog, kind):
    manifest, request = inputs()
    secret = "private-person private-query +1-555-111-2222 token=private-token"
    def fail(*_args):
        raise kind(secret)
    monkeypatch.setattr(repository_module, "generation_recipe", fail)
    caplog.set_level(logging.WARNING, logger=repository_module.__name__)
    repo = repository()
    with pytest.raises(DeterministicJobError) as caught:
        await repo.prepare(manifest=manifest, request=request, run_generation=1)
    assert caught.value.error_code == "V22_CAPABILITY_INPUT_INVALID"
    assert caught.value.user_message == "The saved analysis sources could not be prepared."
    assert caught.value.__suppress_context__
    repo.rpc.post.assert_not_awaited()
    records = [r for r in caplog.records if r.name == repository_module.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.exception_type == kind.__name__
    assert record.exception_location.startswith("app.jobs_v22.capability_input_repository:prepare:")
    assert record.job_id_suffix == str(manifest.job_id)[-8:]
    assert record.error_code == "V22_CAPABILITY_INPUT_INVALID"
    assert record.exc_info is None and record.stack_info is None
    assert secret not in caplog.text and "private-" not in caplog.text
    assert str(manifest.job_id) not in caplog.text


@pytest.mark.anyio
async def test_real_generation_validation_logs_its_innermost_application_frame(caplog):
    manifest, request = inputs()
    request = request.model_copy(deep=True)
    request.limits.max_findings -= 1
    caplog.set_level(logging.WARNING, logger=repository_module.__name__)
    repo = repository()
    with pytest.raises(DeterministicJobError):
        await repo.prepare(manifest=manifest, request=request, run_generation=1)
    record = next(r for r in caplog.records if r.name == repository_module.__name__)
    assert record.exception_type == "ValueError"
    assert record.exception_location.startswith("app.jobs_v22.capability_inputs:validate_generation_input:")
    assert "Invalid generation identity" not in caplog.text
    repo.rpc.post.assert_not_awaited()


@pytest.mark.anyio
async def test_bad_manifest_cannot_replace_the_original_validation_error(caplog):
    caplog.set_level(logging.WARNING, logger=repository_module.__name__)
    repo = repository()
    with pytest.raises(DeterministicJobError, match="^V22_CAPABILITY_INPUT_INVALID$"):
        await repo.prepare(manifest={"job_id": "private-not-a-uuid"}, request={}, run_generation=1)
    record = next(r for r in caplog.records if r.name == repository_module.__name__)
    assert record.job_id_suffix == "invalid"
    assert "private-not-a-uuid" not in caplog.text
    repo.rpc.post.assert_not_awaited()
