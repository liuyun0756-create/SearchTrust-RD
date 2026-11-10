"""Legal slugs must win over embedded Service, FAQ and article schemas."""
from collections import Counter
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.collectors.site_inventory_selection import classify_page
from test_v22_site_inventory_selection import structure


@pytest.mark.parametrize('slug', ['privacy-policy', 'terms-of-use', 'privacy', 'cookie-policy', 'cookie_policy', 'privacy_policy', 'terms_of_use', 'cookies', 'legal', 'company/privacy-policy'])
@pytest.mark.parametrize('schemas', [(), ('Service',), ('Service', 'FAQPage'), ('Product', 'Article')])
def test_legal_path_tokens_precede_other_content_signals(slug, schemas):
    result = classify_page(url='https://example.test/'+slug, root_url='https://example.test/',
        structure=structure(title='Common questions about our services', schema_types=schemas))
    assert result.page_type == 'legal' and result.reasons == ('legal_path',)


@pytest.mark.parametrize('slug,expected', [('contact-us', 'contact'), ('get-in-touch', 'contact'), ('about-us', 'about'), ('our-story', 'about'),
    ('case-study/customer', 'case_study_portfolio'), ('case-studies/customer', 'case_study_portfolio'), ('our-work', 'case_study_portfolio'),
    ('service-area/fresno', 'service_area'), ('service-areas/fresno', 'service_area'), ('areas-we-serve', 'service_area'), ('what-we-do', 'service_index')])
def test_existing_compound_path_aliases_are_preserved(slug, expected):
    assert classify_page(url='https://example.test/'+slug, root_url='https://example.test/', structure=structure()).page_type == expected


@pytest.mark.parametrize('slug', ['privacyproof-installation', 'private-services', 'legalisation-services'])
def test_legal_tokens_do_not_match_arbitrary_substrings(slug):
    assert classify_page(url='https://example.test/'+slug, root_url='https://example.test/', structure=structure(schema_types=('Service',))).page_type == 'service_detail'


@pytest.mark.parametrize('slug,title,schemas,expected', [
    ('faq', '', ('Service', 'FAQPage'), 'faq'),
    ('questions', '', ('Service',), 'faq'),
    ('frequently-asked-questions', '', ('Service',), 'faq'),
    ('support', 'Frequently Asked Questions', ('Service',), 'faq'),
    ('support', 'Common Questions', (), 'faq'),
    ('faucet-installation', 'Faucet Installation', ('Service', 'FAQPage'), 'service_detail'),
    ('contact', 'Contact us', ('Service', 'FAQPage'), 'contact'),
    ('about', 'About us', ('Service', 'FAQPage'), 'about'),
    ('unclassified', 'Ordinary page', ('FAQPage',), 'other'),
    ('article', 'Article', ('FAQPage', 'Article'), 'blog_post'),
    ('product', 'Product', ('FAQPage', 'Product'), 'product_detail'),
])
def test_faq_requires_a_page_level_path_or_heading_signal(slug, title, schemas, expected):
    assert classify_page(url='https://example.test/'+slug, root_url='https://example.test/', structure=structure(title=title,schema_types=schemas)).page_type == expected


FROZEN = json.loads((Path(__file__).parent/'fixtures/v22_site_inventory/all-star-912ddcd2-classification.json').read_text())
SERVICE_PATHS = {
    '/backflow-testing', '/bathroom-remodel', '/drain-cleaning', '/faucet-installation',
    '/garbage-disposal-installation', '/hydro-jetting', '/kitchen-remodel', '/leak-detection',
    '/plumbing-remodeling', '/repipe-specialists', '/sewer-camera-inspection', '/sewer-line-cleaning',
    '/sewer-repair', '/sink-installation', '/tankless-water-heaters', '/toilet-installation',
    '/water-filter-installation', '/water-heater-installation', '/water-heater-repair', '/water-softener-installation',
}


def frozen_classification(row):
    return classify_page(url=row['url'],root_url=FROZEN['root_url'],structure=structure(
        title=row['title'] or '',h1=row['h1'] or '',schema_types=tuple(row['schema_types'])))


@pytest.mark.parametrize('row', FROZEN['pages'], ids=lambda row: urlsplit(row['url']).path)
def test_exact_912ddcd2_frozen_page_metadata_classifies_by_page_purpose(row):
    path = urlsplit(row['url']).path
    expected = {'/':'home','/privacy-policy':'legal','/terms-of-use':'legal','/contact':'contact','/about':'about'}
    assert path in SERVICE_PATHS or path in expected
    assert frozen_classification(row).page_type == expected.get(path,'service_detail')


def test_exact_912ddcd2_frozen_counts_and_twenty_service_urls():
    assert FROZEN['source_job_id'] == '912ddcd2-7135-439a-afa4-de6fcf1e9079'
    assert FROZEN['payload_checksum'] == 'sha256:6159615add026fd0cd950b6e7d81a01993623fb0470565c5ccb5f29a31db9131'
    assert len(FROZEN['pages']) == 25
    assert all('Service' in row['schema_types'] for row in FROZEN['pages'])
    assert sum('FAQPage' in row['schema_types'] for row in FROZEN['pages']) == 20
    assert {urlsplit(row['url']).path for row in FROZEN['pages'] if row['old_page_type']=='faq'} == SERVICE_PATHS
    assert Counter(frozen_classification(row).page_type for row in FROZEN['pages']) == {
        'home':1,'about':1,'contact':1,'legal':2,'service_detail':20}
