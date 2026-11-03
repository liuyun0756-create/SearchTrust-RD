"""Availability observations from a separately frozen rendered source."""
from app.report_v22.evidence_adapters.common import coverage, observe
from app.report_v22.models import SourceLocator
from app.collectors.site_render_models import RENDER_FAILURE
from app.collectors.site_render_models import RENDER_NOTE


def observations(source):
    for index, page in enumerate(source.payload.pages):
        path = f'/payload/pages/{index}'
        locator = SourceLocator(url=page.url)
        if page.deep_snapshot is None:
            yield coverage(source, str(page.url), 'render_availability', 'unavailable',
                'Additional page check unavailable', path + '/limitation', locator=locator, limitations=[RENDER_FAILURE])
        else:
            yield observe(source, 'site_field', str(page.url), 'rendered_page_status',
                page.deep_snapshot.status_code, path + '/deep_snapshot/status_code', locator=locator,
                limitations=[RENDER_NOTE])
