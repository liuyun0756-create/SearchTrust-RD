"""Existing normalized metrics only: no estimation, aggregation or zero filling."""

from datetime import date

from app.report_v22.evidence_adapters.common import coverage, observe
from app.report_v22.models import SourceLocator

DEVICE_LIMITATION = "The supplied device (including tablet) is retained in the internal trace; the frozen locator supports only desktop/mobile."
QUERY_LIMITATION = "The full supplied query is retained in the internal dimensions; it exceeds the frozen locator length."


def observations(source):
    p=source.payload
    context=p.provider_request_context
    payload=p.normalized_payload
    notes=sorted(set([*payload.limitations,*p.health_reasons]))
    if not payload.rows and not payload.aggregates:
        yield coverage(source,context.external_resource_id,"normalized_payload","empty",None,"/payload/normalized_payload",health_status=p.health_status,
            locator=SourceLocator(external_resource_id=context.external_resource_id),request_context=context,limitations=notes)
    groups=[("aggregate",[],payload.aggregates,"/payload/normalized_payload/aggregates")]
    groups.extend(("row",row.dimensions,row.metrics,f"/payload/normalized_payload/rows/{index}/metrics") for index,row in enumerate(payload.rows))
    for category, dimensions, metrics, path in groups:
        dims={d.key:d.value for d in dimensions}
        device=dims.get("device",context.device)
        query=dims.get("query")
        local_notes=list(notes)
        if device is not None and device not in {"desktop","mobile"}:
            local_notes.append(DEVICE_LIMITATION)
        if query is not None and len(query)>300:
            local_notes.append(QUERY_LIMITATION)
        locator=SourceLocator(external_resource_id=context.external_resource_id,
            query=query if query is not None and len(query)<=300 else None,device=device if device in {"desktop","mobile"} else None)
        for index, metric in enumerate(metrics):
            yield observe(source,"metric",[category,context.external_resource_id],"value",metric.value,f"{path}/{index}/value",
                locator=locator,dimensions=dimensions,metric_key=metric.key,unit=metric.unit,request_context=context,
                limitations=local_notes,collected_at=p.fetched_at,
                coverage_start=date.fromisoformat(context.start_date) if context.start_date else None,
                coverage_end=date.fromisoformat(context.end_date) if context.end_date else None)
