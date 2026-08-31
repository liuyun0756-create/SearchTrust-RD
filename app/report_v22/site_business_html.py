"""Bounded source-position HTML tree, without rendering or inferred contact facts."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re

from app.jobs_v22.digest import request_digest
from app.report_v22.site_business_errors import require
from app.report_v22.site_business_models import CandidateDiagnostic, CandidateDraft, CandidateOrigin, FIELD_NAMES

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
IGNORED = frozenset("script style noscript template svg".split())
LABELS = {
    "business_name": ("business name", "company name", "商家名称", "公司名称"),
    "phone": ("phone", "telephone", "tel", "电话", "联系电话"),
    "address": ("address", "business address", "地址", "营业地址"),
    "service_area": ("areas served", "service areas", "we serve", "服务区域", "服务范围"),
}


@dataclass
class ParseBudget:
    limits: object
    html_bytes: int = 0
    html_nodes: int = 0


@dataclass
class Node:
    tag: str
    attrs: dict
    start: int
    content_start: int
    path: list[int]
    suppressed: bool = False
    hidden: bool = False
    end: int = 0
    content_end: int = 0
    closed: bool = False
    bad: bool = False
    parent: object = None
    children: list = field(default_factory=list)
    elements: list = field(default_factory=list)


class HtmlTree(HTMLParser):
    def __init__(self, text, origin_path, budget):
        super().__init__(convert_charrefs=True)
        self.text, self.origin_path, self.budget = text, origin_path, budget
        self.checksum = request_digest({"html": text})
        self.lines = [0, *[match.end() for match in re.finditer("\n", text)]]
        self.root = Node("document", {}, 0, 0, [], end=len(text), content_end=len(text), closed=True)
        self.stack, self.nodes, self.json_blocks = [self.root], [], []
        self.node_count, self.dom_failed = 0, False

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def count_node(self):
        self.node_count += 1
        self.budget.html_nodes += 1
        require(self.node_count <= self.budget.limits.max_html_nodes_per_page
                and self.budget.html_nodes <= self.budget.limits.max_html_nodes_total, "LIMIT_EXCEEDED")

    def handle_starttag(self, tag, attrs):
        self.count_node()
        require(len(self.stack) <= self.budget.limits.max_html_depth, "LIMIT_EXCEEDED")
        parent, start = self.stack[-1], self.source_offset()
        attrs_dict = dict(attrs)
        suppressed = parent.hidden or "hidden" in attrs_dict or (attrs_dict.get("aria-hidden") or "").casefold() == "true"
        node = Node(tag, attrs_dict, start, start + len(self.get_starttag_text()), [*parent.path, len(parent.elements)],
                    suppressed=suppressed, hidden=suppressed or tag in IGNORED, parent=parent,
                    bad=len(attrs_dict) != len(attrs))
        if node.bad:
            self.dom_failed = True
        parent.children.append(node)
        parent.elements.append(node)
        self.nodes.append(node)
        if tag in VOID:
            node.closed, node.end, node.content_end = True, node.content_start, node.content_start
        else:
            self.stack.append(node)
        if tag == "script" and (attrs_dict.get("type") or "").strip().casefold() == "application/ld+json" and not suppressed:
            require(len(self.json_blocks) < self.budget.limits.max_jsonld_blocks, "LIMIT_EXCEEDED")
            self.json_blocks.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            node = self.stack.pop()
            node.closed, node.end, node.content_end = True, node.content_start, node.content_start

    def handle_endtag(self, tag):
        position = next((i for i in range(len(self.stack) - 1, 0, -1) if self.stack[i].tag == tag), None)
        if position is None:
            self.dom_failed = True
            return
        offset = self.source_offset()
        end = self.text.find(">", offset) + 1
        node = self.stack[position]
        if position != len(self.stack) - 1:
            self.dom_failed = node.bad = True
            for unclosed in self.stack[position + 1:]:
                unclosed.end = unclosed.content_end = offset
                unclosed.bad = True
        node.closed, node.end, node.content_end = True, end, offset
        del self.stack[position:]

    def handle_data(self, data):
        if data:
            self.count_node()
            self.stack[-1].children.append(data)

    def handle_comment(self, data):
        self.count_node()

    def finish(self):
        try:
            self.feed(self.text)
            self.close()
        except (AssertionError, ValueError):
            self.dom_failed = True
        if len(self.stack) > 1:
            self.dom_failed = True
            for node in self.stack[1:]:
                node.end = node.content_end = len(self.text)
                node.bad = True
        return self

    def at_path(self, path):
        node = self.root
        for index in path:
            node = node.elements[index]
        return node


def parse_html(text, origin_path, budget):
    size = len(text.encode("utf-8"))
    budget.html_bytes += size
    require(size <= budget.limits.max_html_bytes_per_page and budget.html_bytes <= budget.limits.max_html_bytes_total, "LIMIT_EXCEEDED")
    return HtmlTree(text, origin_path, budget).finish()


def visible_text(node):
    if node.hidden or node.bad or not node.closed:
        return ""
    if node.tag == "br":
        return "\n"
    return "".join(child if isinstance(child, str) else visible_text(child) for child in node.children)


def ambiguous_label_value(node):
    """Do not flatten nested contact groups or lists into an outer label's value."""
    return any(not child.hidden and (
        child.tag in {"dl", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}
        or ambiguous_label_value(child)) for child in node.elements)


def origin(tree, node, *, pointer=None, record_pointer=None, component=None, attribute=None):
    is_json = pointer is not None
    start, end = (node.content_start, node.content_end) if is_json else (node.start, node.end)
    return CandidateOrigin(origin_path=tree.origin_path, decoded_html_checksum=tree.checksum, start=start, end=end,
        kind="jsonld" if is_json else "dom", json_pointer=pointer, record_pointer=record_pointer,
        element_path=[] if is_json else node.path, attribute=attribute, component=component,
        excerpt=tree.text[start:min(end, start + 360)], excerpt_truncated=end - start > 360)


def diagnostic(tree, code, fields=FIELD_NAMES, node=None, *, pointer=None, scope="extraction"):
    return CandidateDiagnostic(code=code, fields=list(fields), scope=scope, origin_path=tree.origin_path,
        start=node.start if node else None, end=node.end if node else None, json_pointer=pointer)


def valid_text(value, field, component=None):
    maximum = (500 if component == "streetAddress" else 240) if component else {
        "business_name": 240, "phone": 120, "address": 500, "service_area": 240}[field]
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and (field != "phone" or any(c.isdigit() for c in value)))


def dom_candidates(tree):
    drafts, diagnostics = [], []
    if tree.dom_failed:
        diagnostics.append(diagnostic(tree, "html_parse_failed"))

    def emit(field, node, kind, value, attribute=None):
        if kind == "dom_label" and ambiguous_label_value(node):
            diagnostics.append(diagnostic(tree, "unsupported_field_shape", [field], node))
            return
        if not valid_text(value, field):
            diagnostics.append(diagnostic(tree, "invalid_field_value", [field], node))
            return
        drafts.append(CandidateDraft(field=field, source_kind=kind, ownership_status="unresolved", scalar_value=value,
                                     origins=[origin(tree, node, attribute=attribute)]))
        require(len(drafts) <= tree.budget.limits.max_candidates_per_page, "LIMIT_EXCEEDED")

    for node in tree.nodes:
        if node.hidden or node.bad or not node.closed:
            continue
        href = node.attrs.get("href") or ""
        if node.tag == "a" and href[:4].casefold() == "tel:":
            emit("phone", node, "tel_link", href[4:], "href")
        if node.tag == "address":
            emit("address", node, "dom_address", visible_text(node))
        label = " ".join(visible_text(node).split()).casefold()
        if label.endswith((":", "：")):
            label = label[:-1].strip()
        field_name = next((key for key, labels in LABELS.items() if label in labels), None)
        if field_name is None:
            continue
        siblings = node.parent.elements
        index = node.path[-1]
        value_node = siblings[index + 1] if index + 1 < len(siblings) else None
        is_label = (node.tag == "dt" and node.parent.tag == "dl"
                    or node.tag in {"td", "th"} and node.parent.tag == "tr"
                    or node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"})
        if not is_label:
            continue
        allowed = {"dd"} if node.tag == "dt" else {"td", "th"} if node.tag in {"td", "th"} else {"p", "div", "address", "ul", "ol"}
        if value_node is None or value_node.tag not in allowed or value_node.bad or not value_node.closed or value_node.hidden:
            diagnostics.append(diagnostic(tree, "unsupported_field_shape", [field_name], node))
            continue
        if field_name == "service_area" and value_node.tag in {"ul", "ol"}:
            values = [child for child in value_node.elements if child.tag == "li"]
            require(len(values) <= tree.budget.limits.max_field_values, "LIMIT_EXCEEDED")
            if not values:
                diagnostics.append(diagnostic(tree, "invalid_field_value", [field_name], value_node))
            for child in values:
                if not child.hidden:
                    emit(field_name, child, "dom_label", visible_text(child))
        else:
            emit(field_name, value_node, "dom_label", visible_text(value_node))
    return drafts, diagnostics
