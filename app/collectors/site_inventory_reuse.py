"""Derive complete structure only from an already successful, bounded deep fetch."""
from urllib.parse import urlsplit

from app.collectors.site_inventory_fetcher import SiteFetchResponse
from app.collectors.site_inventory_html import HtmlStructure
from app.collectors.site_inventory_selection import classify_page
from app.collectors.site_inventory_urls import SiteScope, canonicalize_inventory_url


def restored_record(record, response: SiteFetchResponse, structure: HtmlStructure, *, scope: SiteScope, max_bytes: int):
    if not getattr(record, 'structural_truncated', False):
        return record
    requested = canonicalize_inventory_url(response.requested_url, scope=scope)
    final = canonicalize_inventory_url(response.final_url, scope=scope)
    host = urlsplit(response.final_url).hostname
    if (requested is None or requested != canonicalize_inventory_url(str(record.url), scope=scope)
            or final is None or not host or not scope.contains_host(host)
            or not 200 <= response.status_code < 300 or response.truncated
            or response.content_type.split(';', 1)[0].strip().casefold() not in {'text/html', 'application/xhtml+xml'}
            or response.response_bytes > max_bytes):
        return record
    classification = classify_page(url=str(record.url), root_url=scope.root_url, structure=structure)
    return type(record).model_validate({**record.model_dump(mode='json'),
        'structural_truncated': False, 'status_code': response.status_code, 'final_url': response.final_url,
        'content_type': response.content_type[:200], 'response_bytes': response.response_bytes,
        'title': structure.title, 'h1': structure.h1, 'canonical_url': structure.canonical_url,
        'meta_robots': list(structure.meta_robots), 'schema_types': list(structure.schema_types),
        'internal_links': list(structure.internal_links), 'page_type': classification.page_type,
        'classification_reasons': list(classification.reasons)})
