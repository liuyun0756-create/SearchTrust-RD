"""Observed SERP values, not newly calculated rankings or market absence claims."""

from app.api.v2.models import DimensionValue
from app.report_v22.evidence_adapters.common import coverage, fields, observe
from app.report_v22.models import SourceLocator


def observations(source):
    p = source.payload
    for index, count in enumerate(p.result_counts):
        yield observe(source,"serp_field",["result_count",count.result_type],"count",count.count,
            f"/payload/result_counts/{index}/count",limitations=p.limitations)
    for index, run in enumerate(p.query_runs):
        path = f"/payload/query_runs/{index}"
        notes = sorted(set([*p.limitations,*run.limitations]))
        for name in ("maps_call","google_call"):
            call = getattr(run,name)
            locator = SourceLocator(query=run.query,latitude=call.latitude,longitude=call.longitude,language=call.language,device=call.device)
            dims=[DimensionValue(key="country_code",value=call.country_code)]
            kwargs=dict(locator=locator,dimensions=dims,limitations=notes,collected_at=call.completed_at)
            if call.status != "succeeded":
                yield coverage(source,call.call_id,"status","unavailable",call.status,f"{path}/{name}/status",health_status="unavailable",**kwargs)
                continue
            yield observe(source,"serp_field",call.call_id,"status",call.status,f"{path}/{name}/status",**kwargs)
            for result_type in call.result_types:
                if not any(r.call_id==call.call_id and r.result_type==result_type for r in run.results):
                    yield coverage(source,[call.call_id,result_type],"results","empty",None,f"{path}/results",health_status="healthy",**kwargs)
        for result_index, result in enumerate(run.results):
            locator=SourceLocator(url=result.url,query=result.query,latitude=result.latitude,longitude=result.longitude,language=result.language,device=result.device)
            yield from fields(source,"serp_field",[result.call_id,result.record_id,result.result_type],result,f"{path}/results/{result_index}",
                ("result_type","position","rank_source","display_name","url","rating","review_count"),
                locator=locator,dimensions=[DimensionValue(key="country_code",value=result.country_code)],limitations=notes,collected_at=result.observed_at)
        if not run.complete:
            yield coverage(source,run.query,"complete","partial",run.complete,f"{path}/complete",locator=SourceLocator(query=run.query),limitations=notes,health_status="healthy")
