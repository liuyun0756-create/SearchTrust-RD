"""The v22 additions collect only a retained branch and leave history replayable."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import fakeredis.aioredis
import pytest

from app.core.config import Settings
from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.cost_models import PricingCatalog
from app.jobs_v22.customer_gbp_review_binding import bind_customer_review_sample, eligible_profile
from app.jobs_v22.customer_gbp_reviews import CustomerGbpReviewsStage, LOOKUP_OPERATION, OPERATION
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.comparison_reasons import prepare_reasons_input
from app.report_v22.customer_review_sample import CustomerReview, CustomerReviewPage, CustomerReviewSample
from app.report_v22.identity_inheritance import _bind_inheritance
from test_p1_02_gbp_comparison import comparison_input

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)
DATA_ID = "0x1:0x3039"
CONFIRMATION = UUID("11111111-1111-4111-8111-111111111116")


def review(index, t=NOW):
    days = 10 + index * 4 if index < 2 else 91 + index
    return CustomerReview(id=sha256(str(index).encode()).hexdigest(), provider_id=True,
                          iso_date=t - timedelta(days=days), reply_state="visible" if index < 20 else "not_visible")


def sample(branch, t=NOW, *, bound=False):
    return CustomerReviewSample(branch_ref=branch, data_id=DATA_ID,
        pages=[CustomerReviewPage(sampled_at=t, response_checksum="sha256:"+"a"*64, sort_verified=True,
                                  has_next=True, returned=10)]*3,
        reviews=[review(i, t) for i in range(30)], sampled_at=t, completed_at=t,
        stop="sample_cap", invalid_records=int(bound), discarded_recent_or_invalid=False)


def inputs():
    base, request = prepare_reasons_input(*comparison_input())
    manifest, request = _bind_inheritance(base, request, CONFIRMATION, "sha256:"+"b"*64, 4,
        (NOW - timedelta(days=1)).isoformat().replace("+00:00","Z"), True, False, False, False)
    return manifest, request


def output_fixture():
    old, request = inputs()
    branch = request.business_identity.selected_branch_ref
    m, q = bind_customer_review_sample(old, request, sample(branch))
    _, _, recipe = generation_recipe(m, q)
    report = build_public_capability_report(m, q)
    return {"manifest": m, "request": q, "recipe": recipe}, report


def test_frozen_review_sample_metrics_and_inference_are_bound_to_v22(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient.send", lambda *a, **kw: pytest.fail("network forbidden"))
    old, original = inputs()
    old_report = canonical_json_bytes(build_public_capability_report(old, original))
    bound, report = output_fixture()
    m, request = bound["manifest"], bound["request"]
    assert (m.schema_version, m.ruleset_version, report.schema_version) == (
        "v22_analysis_input_v12", "v22_capability_public_v22", "2.2.17")
    client = report.gbp_comparison.rows[0].metrics
    assert client["reviews_90d"].value == 2 and not client["reviews_90d"].lower_bound
    assert client["latest_review"].value == (NOW - timedelta(days=10)).isoformat()
    assert (client["visible_replies"].numerator, client["visible_replies"].denominator) == (20, 30)
    assert [d.status for d in report.gbp_comparison.differences[3:]] == ["observed", "observed", "not_checked"]
    assert report.possible_reasons.items[-1].metric == "recent_review_activity"
    assert canonical_json_bytes(build_public_capability_report(old, original)) == old_report
    assert not Settings(_env_file=None).V22_CUSTOMER_GBP_REVIEWS_ENABLED
    assert canonical_json_bytes(build_public_capability_report(m, request)) == canonical_json_bytes(report)


def test_full_sample_inside_window_is_a_lower_bound_even_on_final_page():
    data = sample("12345")
    data.reviews = [review(i).model_copy(update={"iso_date": NOW - timedelta(days=i)}) for i in range(30)]
    data.stop = "natural_end"
    data.pages[-1] = data.pages[-1].model_copy(update={"has_next": False})
    assert not data.exact_window


def test_review_collection_refuses_unretained_and_overlapping_bounds_do_not_compare():
    m, q = inputs()
    m2 = m.model_copy(update={"identity_inheritance":m.identity_inheritance.model_copy(update={"branch":"needs_confirmation"})})
    assert eligible_profile(m2, q) is None
    with pytest.raises(ValueError, match="not for the retained branch"):
        bind_customer_review_sample(m, q, sample("wrong"))
    manifest, request = bind_customer_review_sample(m, q, sample(q.business_identity.selected_branch_ref, bound=True))
    report = build_public_capability_report(manifest, request)
    assert report.gbp_comparison.rows[0].metrics["reviews_90d"].lower_bound
    assert report.gbp_comparison.differences[3].status == "not_checked"
    assert all(r.metric != "recent_review_activity" for r in report.possible_reasons.items)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("case,expected_attempts,stop", [
    ("bound", 4, "sample_cap"), ("mismatch", 2, "branch_unavailable"),
    ("empty", 2, "natural_end"), ("retries", 5, "budget"),
])
async def test_customer_review_provider_attempts_are_bounded_and_checkpointed(case, expected_attempts, stop):
    manifest, q = inputs()
    snapshot = eligible_profile(manifest, q).payload
    calls = []

    class Provider:
        async def request(self, params, *, before_attempt, after_attempt):
            if params["engine"] == "google_maps":
                await before_attempt(0,"fixture0")
                calls.append(params)
                await after_attempt(0,"fixture0",None)
                return SimpleNamespace(payload={"place_results":{"cid":"12345","data_id":DATA_ID}})
            assert params["engine"] == "google_maps_reviews" and params["data_id"] == DATA_ID
            assert params["sort_by"] == "newestFirst"
            if case == "retries":
                for i in range(9):
                    await before_attempt(i,"retry")
                    calls.append(params)
                    await after_attempt(i,"retry","http")
                pytest.fail("budget should deny before further requests")
            await before_attempt(0,"fixture0")
            calls.append(params)
            await after_attempt(0,"fixture0",None)
            page = len(calls)-2
            return SimpleNamespace(response_checksum="sha256:"+"a"*64, payload={
                "search_parameters":{"data_id": DATA_ID if case != "mismatch" else "0x1:0x4", "sort_by":"newestFirst"},
                "search_metadata":{"status":"Success", "created_at":NOW.isoformat()},
                "reviews":[] if case == "empty" else [
                    {"review_id":str(i), "rating":5, "iso_date":review(i).iso_date.isoformat(),
                     "response":{"snippet":"A reply that cannot be exported"}}
                    for i in range(page*10,page*10+10)],
                "serpapi_pagination": None if case == "empty" else {"next_page_token":str(len(calls))},
            })

    async with fakeredis.aioredis.FakeRedis() as redis:
        checkpoints = JobCheckpoints(redis,prefix="customer-reviews",ttl_seconds=600)
        ledger = JobCostLedger(redis,prefix="customer-reviews",job_id=manifest.job_id,
            ttl_seconds=600,pricing=PricingCatalog(),job_created_at=NOW,clock=lambda:NOW)
        stage = CustomerGbpReviewsStage(Provider(), clock=lambda:NOW)
        kwargs = dict(job_id=manifest.job_id,snapshot=snapshot,branch_ref="12345",checkpoints=checkpoints,
                      cost_ledger=ledger,deadline_at=NOW+timedelta(minutes=10))
        result = await stage.collect(**kwargs)
        assert result.stop == stop
        assert len(calls) == expected_attempts
        assert result == await stage.collect(**kwargs)
        assert len(calls) == expected_attempts
        assert sum(c.operation in {LOOKUP_OPERATION, OPERATION} for c in (await ledger.ensure()).claims) == expected_attempts
        assert b"A reply that cannot be exported" not in canonical_json_bytes(result)
        assert len(result.reviews) == (30 if case=="bound" else 0)
