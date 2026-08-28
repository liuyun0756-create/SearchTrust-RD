"""Deterministic page classification and later deep-page selection rules."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from app.collectors.site_inventory_html import HtmlStructure
from app.collectors.site_inventory_models import SitePageType


@dataclass(frozen=True)
class ClassificationResult:
    page_type: SitePageType
    reasons: tuple[str, ...]


def _path_segments(url: str) -> list[str]:
    return [segment.casefold() for segment in urlsplit(url).path.split("/") if segment]


def classify_page(
    *,
    url: str,
    root_url: str,
    structure: HtmlStructure,
) -> ClassificationResult:
    path = urlsplit(url).path.rstrip("/") or "/"
    root_path = urlsplit(root_url).path.rstrip("/") or "/"
    segments = _path_segments(url)
    joined_path = "/".join(segments)
    text = " ".join(filter(None, (structure.title, structure.h1))).casefold()
    schema_types = {item.casefold() for item in structure.schema_types}

    if path == root_path and urlsplit(url).hostname == urlsplit(root_url).hostname:
        return ClassificationResult("home", ("root_url",))
    if any(token in segments for token in {"privacy", "terms", "legal", "cookies"}):
        return ClassificationResult("legal", ("legal_path",))
    if "faqpage" in schema_types or "frequently asked" in text or "common questions" in text:
        return ClassificationResult("faq", ("faq_signal",))
    if "product" in schema_types:
        return ClassificationResult("product_detail", ("schema_product",))
    if schema_types.intersection({"blogposting", "newsarticle", "article"}):
        return ClassificationResult("blog_post", ("schema_article",))
    if any(token in segments for token in {"contact", "contact-us", "get-in-touch"}):
        return ClassificationResult("contact", ("contact_path",))
    if any(token in segments for token in {"about", "about-us", "our-story"}):
        return ClassificationResult("about", ("about_path",))
    if any(token in segments for token in {"team", "staff", "leadership", "people"}):
        return ClassificationResult("team", ("team_path",))
    if any(token in segments for token in {"reviews", "testimonials", "testimonial"}):
        return ClassificationResult("review_testimonial", ("review_path",))
    if any(
        token in segments
        for token in {"case-study", "case-studies", "portfolio", "projects", "our-work"}
    ):
        return ClassificationResult("case_study_portfolio", ("case_study_path",))
    if any(token in segments for token in {"service-area", "service-areas", "areas-we-serve"}):
        return ClassificationResult("service_area", ("service_area_path",))
    if any(token in segments for token in {"location", "locations", "branches", "stores"}):
        return ClassificationResult("location", ("location_path",))
    if segments and segments[-1] in {"services", "service", "what-we-do"}:
        return ClassificationResult("service_index", ("service_index_path",))
    if "services" in segments or "service" in schema_types:
        return ClassificationResult("service_detail", ("service_detail_signal",))
    if segments and segments[-1] in {"blog", "news", "insights", "resources"}:
        return ClassificationResult("blog_index", ("blog_index_path",))
    if any(token in segments for token in {"blog", "news", "insights"}):
        return ClassificationResult("blog_post", ("blog_path",))
    if segments and segments[-1] in {"products", "shop", "catalog"}:
        return ClassificationResult("product_category", ("product_index_path",))
    if any(token in segments for token in {"products", "product", "shop", "items"}):
        return ClassificationResult("product_detail", ("product_path",))
    return ClassificationResult("other", (f"unmatched_path:{joined_path or '/'}",))
