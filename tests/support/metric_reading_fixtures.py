"""Arithmetic examples, not client observations or statistical calibration."""
from app.jobs_v22.digest import verified_request_digest, verified_canonical_json_bytes
from app.report_v22.comparison import compare_implementation, _metrics
from app.report_v22.comparison_models import ComparisonResult
from tests.support.comparison_fixtures import parsed, request_raw, ga4_request_raw


def count_result(before, after):
    body=compare_implementation(parsed(request_raw())).model_dump(mode="json")
    body["metrics"]=_metrics(before,after)
    body["checksum"]=verified_request_digest({k:v for k,v in body.items() if k!="checksum"})
    return ComparisonResult.model_validate_json(verified_canonical_json_bytes(body))


def examples():
    for name, before, after in [
        ("one_to_two",(1,100),(2,100)), ("hundred_to_two_hundred",(100,10000),(200,10000)),
        ("zero_baseline",(0,100),(4,120)), ("zero_after",(4,120),(0,100)),
        ("both_zero",(0,0),(0,0)), ("no_before_impressions",(0,0),(4,100)),
        ("no_after_impressions",(4,100),(0,0)), ("unchanged",(20,400),(20,400)),
        ("equal_ctr_different_volume",(10,200),(40,800)),
        ("same_clicks_different_ctr",(10,100),(10,10000)),
        ("full_ctr",(2,2),(3,3)), ("falling",(20,400),(19,400)),
        ("large_counts",(1_000_000_000,100_000_000_000),(1_000_000_001,100_000_000_000)),
        ("safe_integer_limit",(2**53-2,2**53-1),(2**53-1,2**53-1)),
    ]:
        yield name,count_result(before,after)
    raw=request_raw();raw["before"]=None
    yield "missing_source",compare_implementation(parsed(raw))
    yield "ga4_withheld",compare_implementation(parsed(ga4_request_raw()))
