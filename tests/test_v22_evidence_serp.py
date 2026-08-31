from app.report_v22.evidence_adapters.serp import observations
from app.report_v22.evidence_identity import evidence_id
from evidence_helpers import serp_source


def test_query_result_identity_and_successful_empty_response():
    items = list(observations(serp_source()))
    positions = [i for i in items if i.selector.field == "position"]
    assert len(positions) == 3
    assert len({evidence_id(i) for i in positions}) == 3
    assert {i.source_locator.query for i in positions} == {"plumber", "emergency plumber", "water heater repair"}
    assert all(i.source_locator.latitude == 30.2672 for i in positions)
    assert any(i.gap_reason == "empty" and i.source_type == "coverage" for i in items)


def test_four_queries_with_three_complete_keep_successes_and_failed_call_coverage():
    from test_v22_serp_market_models import query_run
    from evidence_helpers import bind, build_input
    from app.report_v22.evidence import build_evidence_index
    source=serp_source()
    p=source.payload
    last=query_run(4,"fixture fourth query")
    last.google_call.status="failed_transient"
    last.google_call.error_code="SERP_PROVIDER_TEMPORARY"
    last.google_call.response_checksum=None
    last.complete=False
    p.query_runs.append(last)
    p.queries.append(last.query)
    p.result_counts[0].count=4
    p.budget.logical_calls_planned=8
    p.budget.logical_calls_used=8
    p.budget.provider_attempt_limit=24
    p.budget.provider_attempts=8
    source.binding=bind(p,"serp",12)
    result=build_evidence_index(build_input(source,queries=p.queries))
    assert {g.reason for g in result.coverage_gaps}>={"unavailable","partial"}
    assert sum(t.selector.field=="position" for t in result.source_traces)==4


def test_local_pack_and_organic_results_remain_separate():
    from evidence_helpers import bind, build_input
    from app.report_v22.evidence import build_evidence_index
    from app.jobs_v22.digest import request_digest
    source=serp_source()
    run=source.payload.query_runs[0]
    for kind in ("local_pack","organic"):
        result=run.results[0].model_copy(update={"result_type":kind,"call_id":run.google_call.call_id,"record_id":request_digest(kind)})
        run.results.append(result)
        next(c for c in source.payload.result_counts if c.result_type==kind).count=1
    source.binding=bind(source.payload,"serp",12)
    result=build_evidence_index(build_input(source))
    types=[e.normalized_value for e,t in zip(result.evidence_index,result.source_traces) if t.selector.field=="result_type"]
    assert set(types)=={"maps","local_pack","organic"}
