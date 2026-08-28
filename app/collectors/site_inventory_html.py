"""Bounded HTML structure extraction for site inventory pages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from app.collectors.site_inventory_urls import SiteScope, canonicalize_inventory_url


_IGNORED_TEXT_TAGS = {"script", "style", "noscript", "template", "svg"}


@dataclass(frozen=True)
class HtmlStructure:
    title: str | None
    h1: str | None
    canonical_url: str | None
    meta_robots: tuple[str, ...]
    schema_types: tuple[str, ...]
    internal_links: tuple[str, ...]
    visible_text: str


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.links: list[str] = []
        self.canonical_href: str | None = None
        self.robot_tokens: list[str] = []
        self.json_ld_documents: list[str] = []
        self._json_ld_parts: list[str] = []
        self._in_title = 0
        self._in_h1 = 0
        self._ignored_depth = 0
        self._in_json_ld = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        if lowered == "title":
            self._in_title += 1
        if lowered == "h1" and not self.h1_parts:
            self._in_h1 += 1
        if lowered == "script" and attributes.get("type", "").casefold() == "application/ld+json":
            self._in_json_ld += 1
            self._json_ld_parts = []
        if lowered in _IGNORED_TEXT_TAGS:
            self._ignored_depth += 1
        if lowered == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        if lowered == "link" and self.canonical_href is None:
            rel_tokens = {item.casefold() for item in attributes.get("rel", "").split()}
            if "canonical" in rel_tokens and attributes.get("href"):
                self.canonical_href = attributes["href"]
        if lowered == "meta":
            name = attributes.get("name", "").casefold()
            if name in {"robots", "googlebot"}:
                for token in re.split(r"[\s,]+", attributes.get("content", "").casefold()):
                    if token and token not in self.robot_tokens:
                        self.robot_tokens.append(token)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title" and self._in_title:
            self._in_title -= 1
        if lowered == "h1" and self._in_h1:
            self._in_h1 -= 1
        if lowered == "script" and self._in_json_ld:
            document = "".join(self._json_ld_parts).strip()
            if document:
                self.json_ld_documents.append(document)
            self._json_ld_parts = []
            self._in_json_ld -= 1
        if lowered in _IGNORED_TEXT_TAGS and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_parts.append(data)
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if not self._ignored_depth:
            self.visible_parts.append(data)


def _clean_text(parts: list[str], *, limit: int) -> str:
    value = " ".join(" ".join(parts).split())
    return value[:limit]


def _collect_schema_types(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        raw_type = value.get("@type")
        if isinstance(raw_type, str) and raw_type.strip():
            found.add(raw_type.strip())
        elif isinstance(raw_type, list):
            for item in raw_type:
                if isinstance(item, str) and item.strip():
                    found.add(item.strip())
        for child in value.values():
            _collect_schema_types(child, found)
    elif isinstance(value, list):
        for child in value:
            _collect_schema_types(child, found)


def extract_html_structure(
    html: str,
    *,
    page_url: str,
    scope: SiteScope,
    max_links: int = 500,
    max_text_chars: int = 200_000,
) -> HtmlStructure:
    parser = _StructureParser()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        pass

    links: list[str] = []
    for raw_link in parser.links:
        normalized = canonicalize_inventory_url(raw_link, base_url=page_url, scope=scope)
        if normalized is not None and normalized not in links:
            links.append(normalized)
        if len(links) >= max_links:
            break

    canonical_url = None
    if parser.canonical_href:
        canonical_url = canonicalize_inventory_url(
            parser.canonical_href,
            base_url=page_url,
            scope=scope,
        )

    schema_types: set[str] = set()
    for raw_json_ld in parser.json_ld_documents:
        try:
            _collect_schema_types(json.loads(raw_json_ld), schema_types)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            pass

    title = _clean_text(parser.title_parts, limit=500) or None
    h1 = _clean_text(parser.h1_parts, limit=500) or None
    return HtmlStructure(
        title=title,
        h1=h1,
        canonical_url=canonical_url,
        meta_robots=tuple(parser.robot_tokens[:20]),
        schema_types=tuple(sorted(schema_types, key=str.casefold)[:50]),
        internal_links=tuple(links),
        visible_text=_clean_text(parser.visible_parts, limit=max_text_chars),
    )
