"""Public competitor evidence stays competitor-scoped, including public GBP."""

from app.report_v22.evidence_adapters.common import coverage, fields
from app.report_v22.evidence_adapters.site import observations as site_observations
from app.report_v22.models import SourceLocator

REVIEW_LIMITATION = "Public reviews are the collected sample only, not the complete review population."


def observations(source):
    for index, item in enumerate(source.payload.competitors):
        competitor_id=item.competitor.competitor_id
        path=f"/payload/competitors/{index}"
        notes=sorted(set([*source.payload.limitations,*item.limitations]))
        locator=SourceLocator(url=item.competitor.website_url)
        kwargs=dict(competitor_id=competitor_id,locator=locator,limitations=notes)
        yield from fields(source,"competitor_field",competitor_id,item,path,
            ("query_appearance_count","best_position","analyzed_page_count"),**kwargs)
        for name in ("site_status","public_gbp_status","reviews_status"):
            status=getattr(item,name)
            if status != "available":
                yield coverage(source,[competitor_id,name],name,status,status,f"{path}/{name}",health_status="healthy" if status=="partial" else "unavailable",**kwargs)
        if item.site_inventory is not None:
            yield from site_observations(source,inventory=item.site_inventory,prefix=f"{path}/site_inventory",competitor_id=competitor_id,limitations=notes)
        if item.public_gbp is not None and item.public_gbp_status != "unavailable":
            profile=item.public_gbp
            yield from fields(source,"competitor_field",[competitor_id,"public_gbp",profile.request_record_id],profile,f"{path}/public_gbp",
                ("business_name","public_gbp_url","website_url","address","categories","rating","review_count"),
                competitor_id=competitor_id,locator=SourceLocator(url=profile.public_gbp_url or item.competitor.public_gbp_url or item.competitor.website_url),
                limitations=notes,collected_at=profile.collected_at)
        if item.reviews_status != "unavailable":
            for review_index, review in enumerate(item.reviews):
                yield from fields(source,"competitor_field",[competitor_id,review.review_record_id,review.request_record_id,review.provider_review_id],review,f"{path}/reviews/{review_index}",
                    ("rating","iso_date","original_date_text","text","owner_response_text","owner_response_iso_date"),
                    competitor_id=competitor_id,locator=SourceLocator(url=item.competitor.public_gbp_url or item.competitor.website_url),
                    limitations=[*notes,REVIEW_LIMITATION],collected_at=review.collected_at)
