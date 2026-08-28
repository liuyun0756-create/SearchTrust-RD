import pytest

from app.collectors.site_inventory_html import HtmlStructure
from app.collectors.site_inventory_selection import classify_page


def structure(*, title: str = "", h1: str = "", schema_types: tuple[str, ...] = ()) -> HtmlStructure:
    return HtmlStructure(
        title=title or None,
        h1=h1 or None,
        canonical_url=None,
        meta_robots=(),
        schema_types=schema_types,
        internal_links=(),
        visible_text="",
    )


@pytest.mark.parametrize(
    ("url", "title", "schema_types", "expected"),
    [
        ("https://example.com/", "Example", (), "home"),
        ("https://example.com/services", "Our Services", (), "service_index"),
        ("https://example.com/services/plumbing", "Plumbing", (), "service_detail"),
        ("https://example.com/service-areas/austin", "Austin Plumber", (), "service_area"),
        ("https://example.com/locations/downtown", "Downtown Office", (), "location"),
        ("https://example.com/about", "About Us", (), "about"),
        ("https://example.com/contact", "Contact", (), "contact"),
        ("https://example.com/team", "Our Team", (), "team"),
        ("https://example.com/reviews", "Testimonials", (), "review_testimonial"),
        ("https://example.com/case-studies/acme", "Acme Project", (), "case_study_portfolio"),
        ("https://example.com/questions", "Common Questions", ("FAQPage",), "faq"),
        ("https://example.com/blog", "Blog", (), "blog_index"),
        ("https://example.com/blog/how-to-fix", "How to Fix", ("BlogPosting",), "blog_post"),
        ("https://example.com/products", "Products", (), "product_category"),
        ("https://example.com/items/widget", "Widget", ("Product",), "product_detail"),
        ("https://example.com/privacy", "Privacy Policy", (), "legal"),
        ("https://example.com/company/history", "Our History", (), "other"),
    ],
)
def test_classifies_fixed_page_types(
    url: str,
    title: str,
    schema_types: tuple[str, ...],
    expected: str,
) -> None:
    result = classify_page(
        url=url,
        root_url="https://example.com/",
        structure=structure(title=title, h1=title, schema_types=schema_types),
    )

    assert result.page_type == expected
    assert result.reasons


def test_classification_is_deterministic() -> None:
    page = structure(title="Emergency Plumbing", schema_types=("Service",))

    first = classify_page(
        url="https://example.com/services/emergency",
        root_url="https://example.com/",
        structure=page,
    )
    second = classify_page(
        url="https://example.com/services/emergency",
        root_url="https://example.com/",
        structure=page,
    )

    assert first == second
