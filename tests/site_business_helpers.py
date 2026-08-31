"""Fixed synthetic snapshots, never live pages or allocated storage IDs."""
import json

from app.jobs_v22.digest import request_digest
from evidence_helpers import CASE, NOW, deep_site_source


def business_html(**changes):
    record = {"@context": "https://schema.org", "@type": "LocalBusiness", "@id": "#business",
              "name": " Fixture Plumbing ", "telephone": ["+1 512 555 0100", "+1 512 555 0101"],
              "address": {"streetAddress": "123 Fixture St", "addressLocality": "Austin"},
              "areaServed": ["Austin", "Round Rock"]}
    record.update(changes)
    return '<script type="application/ld+json">' + json.dumps(record, ensure_ascii=False) + '</script>'


def source(html=None):
    value = deep_site_source()
    page = value.payload.selected_pages[0].deep_snapshot
    page.html = html if html is not None else business_html()
    page.text = "Saved text is not an extraction fallback."
    page.response_bytes = len(page.html.encode())
    page.content_checksum = request_digest({"upstream_fixture_bytes": page.html})
    value.binding.payload_checksum = request_digest(value.payload)
    return value


def request(html=None):
    from app.report_v22.site_business_models import SiteBusinessFactsInput
    return SiteBusinessFactsInput(context=dict(case_id=CASE, report_type="prospect", site_url="https://example.test/", evaluated_at=NOW),
                                  source=source(html))


def two_pages(html=None):
    from pydantic import HttpUrl
    value = request(html)
    payload = value.source.payload
    url = HttpUrl("https://example.test/contact")
    payload.pages.append(payload.pages[0].model_copy(update={"url": url}))
    selected = payload.selected_pages[0].model_copy(deep=True)
    selected.url = selected.deep_snapshot.url = url
    # Both requests may reach one final URL; they remain separate observations.
    payload.selected_pages.append(selected)
    payload.discovered_url_count = payload.structurally_checked_count = payload.deep_analyzed_count = 2
    payload.page_type_counts[0].count = payload.source_counts[0].count = 2
    value.source.binding.payload_checksum = request_digest(payload)
    return value
