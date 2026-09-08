from app.google_connections_v22.ga4 import LandingPageRow
from app.report_v22.cross_source_pages import consolidate_pages, normalize_ga4_page, normalize_gsc_page


DOMAIN = "example.test"


def row(path):
    return LandingPageRow(landing_page=path, sessions=20, engaged_sessions=10,
        engagement_rate=.5, average_session_duration=10, screen_page_views=30,
        key_events=1.0, session_key_event_rate=.05)


def test_gsc_and_ga4_normalize_to_same_conservative_identity() -> None:
    assert normalize_gsc_page("http://www.example.test/a/%7Eb/?b=2&utm_x=z&a=1#x", DOMAIN) == \
        "https://example.test/a/~b?a=1&b=2"
    assert normalize_ga4_page("/a/%7eb/?b=2&a=1&gclid=x", DOMAIN) == \
        "https://example.test/a/~b?a=1&b=2"
    assert normalize_ga4_page("/a#ignored", DOMAIN) == "https://example.test/a"


def test_normalization_rejects_external_or_ambiguous_paths() -> None:
    rejected_gsc = ["https://other.test/", "https://example.test/a/../b", "https://example.test/%ZZ", "https://example.test:80/a"]
    rejected_ga4 = ["(not set)", "https://example.test/a", "/a/../b", "/bad%ZZ", "/bad\\path"]
    assert all(normalize_gsc_page(value, DOMAIN) is None for value in rejected_gsc)
    assert all(normalize_ga4_page(value, DOMAIN) is None for value in rejected_ga4)


def test_tracking_only_variants_consolidate_and_retain_raw_keys() -> None:
    grouped, rejected = consolidate_pages(
        [row("/service?utm_source=a"), row("/service?gclid=b")],
        key=lambda item: item.landing_page, normalizer=normalize_ga4_page, normalized_domain=DOMAIN,
    )
    assert rejected == ()
    assert list(grouped) == ["https://example.test/service"]
    assert grouped["https://example.test/service"].raw_keys == ("/service?gclid=b", "/service?utm_source=a")
