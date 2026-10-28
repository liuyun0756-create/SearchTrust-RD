"""Read exact revisions from disposable PostgreSQL; never dispatch or bill."""
from uuid import uuid4

import pytest

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.review_generation_content import build_review_generation_content
from app.report_v22.review_generation_inputs import ReviewGenerationPreparer
from app.report_v22.review_generation_models import recipe_for
from tests.integration.test_native_review_decisions import decisions, save
from tests.integration.test_native_review_freeze import review_setup, prepared, native_database, inspect
from tests.test_v22_review_decisions import request, item

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(70)]


async def prepare(p, decision):
    preparer = ReviewGenerationPreparer(p["decision_service"], clock=lambda: "2027-12-18T08:00:00Z")
    selection = dict(job_id=str(uuid4()), review_id=str(p["evidence"].id),
        review_checksum=p["evidence"].bundle.checksum, decision_id=str(decision.id), decision_checksum=decision.checksum)
    return preparer, await preparer.prepare(**p["owner"], selection=selection)


async def test_native_generation_pins_old_revision_while_later_decisions_exist(decisions):
    p = decisions; saved = p["evidence"]
    original = saved.model_dump_json()
    first = await save(p, request(saved))
    await save(p, request(saved, [item(saved, "adjust")], previous_id=str(first.id), previous_checksum=first.checksum))
    preparer, value = await prepare(p, first)
    content = build_review_generation_content(value)
    assert content.seo_actions[0].selected_decision.disposition == "continue"
    assert value.decision.id == first.id and value.decision.revision == 1
    replayed = await preparer.resolve(**p["owner"], recipe=recipe_for(value))
    assert replayed == value
    assert build_review_generation_content(replayed) == content
    assert (await p["reviews"].read(**p["identity"], review_id=saved.id)).model_dump_json() == original
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


async def test_native_generation_rechecks_owner_active_case_and_exact_ids(decisions):
    p = decisions; first = await save(p, request(p["evidence"]))
    preparer, value = await prepare(p, first)
    recipe = recipe_for(value)
    for key in ("user_id", "case_id", "report_id"):
        with pytest.raises(DeterministicJobError):
            await preparer.resolve(**(p["owner"] | {key: uuid4()}), recipe=recipe)
    raw = recipe.model_dump(mode="json") | dict(decision_id=str(uuid4()))
    with pytest.raises(DeterministicJobError): await preparer.resolve(**p["owner"], recipe=raw)
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s", [value.case_id])
    with pytest.raises(DeterministicJobError): await preparer.resolve(**p["owner"], recipe=recipe)
