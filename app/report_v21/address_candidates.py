"""Source-aware target-page address candidate discovery."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable


_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "br", "dd", "div", "dl", "dt", "footer",
    "header", "h1", "h2", "h3", "h4", "h5", "h6", "li", "main", "nav",
    "p", "section", "table", "td", "th", "tr",
})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
_ADDRESS_LABEL_RE = re.compile(
    r"^(?:business\s+)?(?:address|location|located\s+at)\s*:?$",
    re.IGNORECASE,
)
_STREET_DISCOVERY_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.'# -]{1,90}?\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
    r"Lane|Ln\.?|Court|Ct\.?|Highway|Hwy\.?|Way|Place|Pl\.?|Parkway|Pkwy\.?)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_MAP_URL_RE = re.compile(
    r"(?:google\.[^/]+/maps|goo\.gl/maps|maps\.app\.goo\.gl|maps\.google\.)",
    re.IGNORECASE,
)
_GENERIC_MAP_TEXT_RE = re.compile(
    r"^(?:directions?|get\s+directions?|view\s+map|map|location)$",
    re.IGNORECASE,
)
_PHONE_OR_EMAIL_RE = re.compile(
    r"(?:\b(?:phone|tel|call|fax|toll\s+free)\b|@|\d{3}[\s().-]+\d{3}[\s.-]+\d{4})",
    re.IGNORECASE,
)

_SOURCE_LABELS = {
    "page.jsonld.postal_address": "Target page · JSON-LD postal address",
    "page.microdata.postal_address": "Target page · Postal-address microdata",
    "page.dom.address_element": "Target page · Address element",
    "page.dom.labeled_address_block": "Target page · Labeled address block",
    "page.dom.map_address": "Target page · Map-associated visible address",
    "page.dom.map_url_only": "Target page · Map link without visible address",
    "page.dom.visible_address": "Target page · Visible address candidate",
}


@dataclass(frozen=True)
class AddressCandidate:
    raw_value: str
    source_type: str
    source_url: str = ""
    locator: str = ""
    excerpt: str = ""
    visibility: str = "visible"
    structured_components: dict[str, str] = field(default_factory=dict)
    country_code: str = ""
    map_url_only: bool = False

    @property
    def source_label(self) -> str:
        return _SOURCE_LABELS.get(self.source_type, self.source_type)


class _Node:
    def __init__(
        self,
        tag: str,
        attrs: dict[str, str],
        parent: _Node | None = None,
    ) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_Node | str] = []


class _DOMParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        node = _Node(
            normalized_tag,
            {str(key).casefold(): str(value or "") for key, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(node)
        if normalized_tag in _VOID_TAGS:
            return
        self.stack.append(node)
        if normalized_tag in _SKIP_TAGS:
            self.skip_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == normalized_tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data:
            self.stack[-1].children.append(data)


def build_address_candidates(
    visible_text: str,
    structured_content: str,
    jsonld_records: list[dict[str, Any]],
    *,
    source_url: str = "",
) -> list[AddressCandidate]:
    candidates: list[AddressCandidate] = []
    candidates.extend(_jsonld_candidates(jsonld_records, source_url))

    root = _parse_dom(structured_content)
    if root is not None:
        nodes = list(_nodes(root))
        candidates.extend(_microdata_candidates(nodes, source_url))
        candidates.extend(_address_element_candidates(nodes, source_url))
        candidates.extend(_labeled_block_candidates(nodes, source_url))
        candidates.extend(_map_candidates(nodes, source_url))
        # HTML input still needs the lowest-authority visible-text fallback.
        # Keep it line-bounded so separate DOM blocks never become an invented
        # address; multi-line assembly belongs only to explicit containers,
        # labels, or shared map targets above.
        candidates.extend(_visible_text_candidates(
            "\n".join(_node_lines(root)),
            source_url,
        ))

    candidates.extend(_visible_text_candidates(visible_text, source_url))
    return _unique_candidates(candidates)


def _jsonld_candidates(
    records: list[dict[str, Any]],
    source_url: str,
) -> list[AddressCandidate]:
    result: list[AddressCandidate] = []
    for record in records:
        value = record.get("address")
        if isinstance(value, str) and value.strip():
            result.append(AddressCandidate(
                _clean(value),
                "page.jsonld.postal_address",
                source_url,
                "script[type='application/ld+json'] address",
                _clean(value),
                "structured",
            ))
        elif isinstance(value, dict):
            components = _postal_components(value)
            raw = _format_components(components)
            if raw:
                result.append(AddressCandidate(
                    raw,
                    "page.jsonld.postal_address",
                    source_url,
                    "script[type='application/ld+json'] address",
                    raw,
                    "structured",
                    components,
                    components.get("country", ""),
                ))
    return result


def _microdata_candidates(
    nodes: list[_Node],
    source_url: str,
) -> list[AddressCandidate]:
    result: list[AddressCandidate] = []
    scopes = [
        node
        for node in nodes
        if (
            node.attrs.get("itemprop", "").casefold() == "address"
            or "postaladdress" in node.attrs.get("itemtype", "").casefold()
        )
    ]
    component_map = {
        "streetaddress": "street_address",
        "addresslocality": "city",
        "addressregion": "state",
        "postalcode": "postal_code",
        "addresscountry": "country",
    }
    for index, scope in enumerate(scopes):
        components: dict[str, str] = {}
        for node in _nodes(scope):
            key = component_map.get(node.attrs.get("itemprop", "").casefold())
            if not key:
                continue
            value = _clean(node.attrs.get("content") or _node_text(node))
            if value:
                components[key] = value
        raw = _format_components(components)
        if raw:
            result.append(AddressCandidate(
                raw,
                "page.microdata.postal_address",
                source_url,
                f"microdata PostalAddress {index + 1}",
                raw,
                "structured",
                components,
                components.get("country", ""),
            ))
    return result


def _address_element_candidates(
    nodes: list[_Node],
    source_url: str,
) -> list[AddressCandidate]:
    return [
        AddressCandidate(
            _join_lines(_node_lines(node)),
            "page.dom.address_element",
            source_url,
            f"address:nth-of-type({index + 1})",
            " | ".join(_node_lines(node)),
        )
        for index, node in enumerate(node for node in nodes if node.tag == "address")
        if _join_lines(_node_lines(node))
    ]


def _labeled_block_candidates(
    nodes: list[_Node],
    source_url: str,
) -> list[AddressCandidate]:
    result: list[AddressCandidate] = []
    for node_index, node in enumerate(nodes):
        lines = _node_lines(node)
        for line_index, line in enumerate(lines):
            if not _ADDRESS_LABEL_RE.fullmatch(line):
                continue
            following: list[str] = []
            for candidate_line in lines[line_index + 1:line_index + 5]:
                if _PHONE_OR_EMAIL_RE.search(candidate_line):
                    break
                following.append(candidate_line)
            for width in range(1, min(3, len(following)) + 1):
                raw = _join_lines(following[:width])
                if raw:
                    result.append(AddressCandidate(
                        raw,
                        "page.dom.labeled_address_block",
                        source_url,
                        f"{node.tag}[{node_index + 1}] after label line {line_index + 1}",
                        " | ".join([line, *following[:width]]),
                    ))
    return result


def _map_candidates(
    nodes: list[_Node],
    source_url: str,
) -> list[AddressCandidate]:
    grouped: dict[str, list[tuple[_Node, str]]] = {}
    for node in nodes:
        href = html.unescape(node.attrs.get("href", "")).strip()
        if node.tag != "a" or not href or not _MAP_URL_RE.search(href):
            continue
        grouped.setdefault(href, []).append((node, _clean(_node_text(node))))

    result: list[AddressCandidate] = []
    for group_index, (href, items) in enumerate(grouped.items()):
        visible_parts = _unique_text(value for _, value in items if value)
        useful_parts = [value for value in visible_parts if not _GENERIC_MAP_TEXT_RE.fullmatch(value)]
        if useful_parts:
            result.append(AddressCandidate(
                _join_lines(useful_parts),
                "page.dom.map_address",
                source_url,
                f"map link group {group_index + 1}",
                " | ".join(visible_parts),
            ))
        else:
            label = visible_parts[0] if visible_parts else "Map link"
            result.append(AddressCandidate(
                label,
                "page.dom.map_url_only",
                source_url,
                f"map link group {group_index + 1}",
                label,
                map_url_only=True,
            ))
    return result


def _visible_text_candidates(
    visible_text: str,
    source_url: str,
) -> list[AddressCandidate]:
    lines = [_clean(line) for line in str(visible_text or "").splitlines() if _clean(line)]
    result: list[AddressCandidate] = []
    for index, line in enumerate(lines):
        if _ADDRESS_LABEL_RE.fullmatch(line):
            # A labelled address may span street/city/state lines, but the
            # following footer column often immediately switches to phone or
            # email details.  Stop at that semantic boundary instead of
            # blindly treating the next three lines as address components.
            following: list[str] = []
            for candidate_line in lines[index + 1:index + 5]:
                if _PHONE_OR_EMAIL_RE.search(candidate_line):
                    break
                following.append(candidate_line)
            for width in range(1, min(3, len(following)) + 1):
                raw = _join_lines(following[:width])
                result.append(AddressCandidate(
                    raw,
                    "page.dom.labeled_address_block",
                    source_url,
                    f"visible lines {index + 2}-{index + width + 1}",
                    " | ".join(lines[index:index + width + 1]),
                ))
        for match in _STREET_DISCOVERY_RE.finditer(line):
            raw = match.group(0).strip()
            line_value = re.sub(
                r"^(?:business\s+)?(?:address|location|located\s+at)\s*:\s*",
                "",
                line,
                flags=re.IGNORECASE,
            ).strip()
            for candidate_value in _unique_text((line_value, raw)):
                result.append(AddressCandidate(
                    candidate_value,
                    "page.dom.visible_address",
                    source_url,
                    f"visible line {index + 1}",
                    line,
                ))
    return result


def _parse_dom(value: str) -> _Node | None:
    if not str(value or "").strip():
        return None
    parser = _DOMParser()
    try:
        parser.feed(str(value))
        parser.close()
    except Exception:
        return None
    return parser.root


def _nodes(root: _Node) -> Iterable[_Node]:
    yield root
    for child in root.children:
        if isinstance(child, _Node):
            yield from _nodes(child)


def _node_text(node: _Node) -> str:
    return " ".join(_node_lines(node))


def _node_lines(node: _Node) -> list[str]:
    parts: list[str] = []

    def walk(current: _Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            if child.tag in _BLOCK_TAGS:
                parts.append("\n")
            walk(child)
            if child.tag in _BLOCK_TAGS:
                parts.append("\n")

    walk(node)
    return [_clean(line) for line in "".join(parts).splitlines() if _clean(line)]


def _postal_components(value: dict[str, Any]) -> dict[str, str]:
    return {
        key: cleaned
        for key, cleaned in (
            ("street_address", _clean(value.get("streetAddress"))),
            ("city", _clean(value.get("addressLocality"))),
            ("state", _clean(value.get("addressRegion"))),
            ("postal_code", _clean(value.get("postalCode"))),
            ("country", _country(value.get("addressCountry"))),
        )
        if cleaned
    }


def _country(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("identifier")
    return _clean(value)


def _format_components(components: dict[str, str]) -> str:
    parts = [
        components.get("street_address", ""),
        components.get("city", ""),
        components.get("state", ""),
        components.get("postal_code", ""),
        components.get("country", ""),
    ]
    return ", ".join(part.strip(" ,") for part in parts if part.strip(" ,"))


def _join_lines(lines: Iterable[str]) -> str:
    cleaned = [_clean(line) for line in lines if _clean(line)]
    return ", ".join(line.rstrip(" ,") for line in cleaned)


def _clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).replace("\xa0", " ").split()).strip()


def _unique_text(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _clean(value).casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(_clean(value))
    return result


def _unique_candidates(values: Iterable[AddressCandidate]) -> list[AddressCandidate]:
    result: list[AddressCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for item in values:
        raw = _clean(item.raw_value)
        key = (raw.casefold(), item.source_type, item.locator)
        if raw and key not in seen:
            seen.add(key)
            result.append(item)
    return result
