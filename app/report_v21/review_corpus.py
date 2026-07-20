"""Build the immutable review corpus shared by Dify and report evidence."""

from __future__ import annotations

import json
import re
from typing import Any


_REVIEW_HEADING = re.compile(
    r"\b(review|reviews|testimonial|testimonials|what (?:our )?customers say|customer stories)\b",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CONTENT_MARKER = re.compile(r"^===\s*(.+?)\s*===$")


def build_review_corpus(
    content: str,
    gbp_data: dict[str, Any] | None,
    *,
    page_url: str = "",
    gbp_url: str = "",
) -> list[dict[str, Any]]:
    """Return one stable review snapshot for both rule checks and evidence."""
    corpus: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, item in enumerate(_page_review_items(content), start=1):
        text = item["text"]
        fingerprint = _fingerprint(text)
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        corpus.append({
            "id": f"page-review-{index:02d}",
            "source": "page",
            "source_label": "Page review or testimonial",
            "source_url": page_url or None,
            "section": item["section"],
            "text": text,
            "author": None,
            "rating": None,
            "date": None,
        })

    gbp = gbp_data if isinstance(gbp_data, dict) else {}
    reviews = gbp.get("review_list") if isinstance(gbp.get("review_list"), list) else []
    for index, review in enumerate(reviews[:30], start=1):
        if not isinstance(review, dict):
            continue
        text = str(review.get("text") or "").strip()
        fingerprint = _fingerprint(text)
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        corpus.append({
            "id": f"gbp-review-{index:02d}",
            "source": "gbp",
            "source_label": f"Recent GBP review {index}",
            "source_url": gbp_url or None,
            "section": "Recent GBP reviews",
            "text": text,
            "author": _optional_text(review.get("author")),
            "rating": review.get("rating"),
            "date": _optional_text(review.get("date")),
        })

    return corpus


def serialize_review_corpus(corpus: list[dict[str, Any]]) -> str:
    """Serialize the exact review records consumed by Dify."""
    return json.dumps(corpus, ensure_ascii=False, separators=(",", ":"))


def _page_review_items(content: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    in_review_section = False
    review_heading_level: int | None = None
    section = "Page reviews"

    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        marker = _CONTENT_MARKER.match(line)
        if marker:
            marker_text = marker.group(1).strip()
            if marker_text.lower().startswith("end "):
                in_review_section = False
                review_heading_level = None
                section = "Page reviews"
            elif _REVIEW_HEADING.search(marker_text):
                in_review_section = True
                review_heading_level = None
                section = marker_text
            continue

        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            if _REVIEW_HEADING.search(heading_text):
                in_review_section = True
                review_heading_level = level
                section = heading_text
            elif in_review_section and review_heading_level is not None and level <= review_heading_level:
                in_review_section = False
                review_heading_level = None
            continue

        if not in_review_section:
            continue
        text = re.sub(r"\s+", " ", line).strip(" -*>\t")
        if len(text) < 20 or _REVIEW_HEADING.fullmatch(text):
            continue
        items.append({"text": text[:1200], "section": section})
        if len(items) >= 20:
            break

    return items


def _fingerprint(value: str) -> str:
    return re.sub(r"\W+", "", value, flags=re.UNICODE).casefold()


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
