"""Deterministic extraction of readable page fragments."""

from __future__ import annotations

import re
from typing import TypedDict


class PageFragment(TypedDict):
    source_type: str
    source_label: str
    page_section: str
    text: str
    kind: str


def extract_page_fragments(content: str) -> list[PageFragment]:
    """Return at most 240 readable fragments, each capped at 360 characters."""
    fragments: list[PageFragment] = []
    source_type = "page"
    source_label = "Checked page content"
    page_section = "Main page"

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        marker = re.match(r"^===\s*(.+?)\s*===$", line)
        if marker:
            marker_text = marker.group(1).strip().lower()
            if marker_text.startswith("end "):
                source_type, source_label, page_section = (
                    "page",
                    "Checked page content",
                    "Main page",
                )
                continue
            page_section = marker.group(1).strip()
            if "contact" in marker_text:
                source_type, source_label = "contact_page", "Checked contact page"
            elif any(value in marker_text for value in ("about", "our-story", "who-we-are")):
                source_type, source_label = "about_page", "Checked about page"
            else:
                source_type, source_label = "site_internal", "Checked internal page"
            continue

        readable = _readable_page_line(line)
        if not readable:
            continue
        text, kind = readable
        if len(text) < 12 and kind not in {"cta", "image"}:
            continue
        fragments.append({
            "source_type": source_type,
            "source_label": source_label,
            "page_section": page_section,
            "text": text[:360],
            "kind": kind,
        })
        if len(fragments) >= 240:
            break
    return fragments


def _readable_page_line(line: str) -> tuple[str, str] | None:
    image_alts = [
        re.sub(r"\s+", " ", alt).strip(" -_#")
        for alt in re.findall(r"!\[([^\]]*)\]\([^)]+\)", line)
        if _meaningful_image_alt(re.sub(r"\s+", " ", alt).strip(" -_#"))
    ]
    without_images = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", line)
    image_remainder = re.sub(r"\[[^\]]*\]\([^)]+\)", " ", without_images)
    if image_alts and not re.sub(r"[#*`\[\]()\s-]", "", image_remainder):
        return "; ".join(image_alts[:3])[:360], "image"

    link_targets = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", without_images)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", without_images)
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -*#|`\t\n")
    if not text or _is_technical_noise(text):
        return None

    is_cta_link = any(
        target.lower().startswith("tel:") or _looks_like_cta(label)
        for label, target in link_targets
    )
    kind = "cta" if is_cta_link or (len(text) <= 120 and _looks_like_cta(text)) else "text"
    return text[:360], kind


def _meaningful_image_alt(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    generic = {
        "", "image", "photo", "picture", "placeholder", "google", "map", "icon",
        "logo", "logo white", "logo dark", "white logo", "dark logo",
    }
    return normalized not in generic and len(normalized) >= 4


def _looks_like_cta(value: str) -> bool:
    return bool(re.search(
        r"\b(?:call|book|schedule|request|contact|get (?:a )?(?:quote|estimate)|learn more|start|apply)\b",
        value,
        flags=re.IGNORECASE,
    ))


def _is_technical_noise(value: str) -> bool:
    lowered = value.casefold()
    if any(marker in lowered for marker in ("data:image", "base64,", "<svg", "viewbox=", "xmlns=", "wp-content/uploads")):
        return True
    return len(value) > 220 and (value.count("%") >= 5 or value.count("/") >= 8)

