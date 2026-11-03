"""Opt-in v6 text observations: bounded strings, never independent business identity."""
import re
from functools import lru_cache

from app.report_v22.site_business_errors import require

TEXT_NOTE = "Based on text recognition in saved HTML; confirm the business details before relying on this comparison."
HOURS_NOTE = "Opening hours were recognized as text only; no hours alignment was performed."
SEP = r"[ \t\u00a0.\-]*"
PHONES = (
    re.compile(r"(?<![\w+])(?:\+?1" + SEP + r")?(?:\(\d{3}\)|\d{3})" + SEP + r"\d{3}" + SEP + r"\d{4}(?!\w)"),
    re.compile(r"(?<![\w+])(?:0|\+44" + SEP + r"(?:\(0\)" + SEP + r")?)(?:\d" + SEP + r"){8,9}\d(?!\w)"),
)
HOURS = re.compile(
    r"\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|opening hours|hours)"
    r"[^\n]{0,65}?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–—]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?",
    re.I,
)
BLOCKS = {"p", "div", "li", "section", "article", "header", "footer", "address", "tr", "h1", "h2", "h3", "h4"}
HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important)?\s*(?:;|$)", re.I)


def text_visible(node):
    # Do not broaden or change the historical v1 visibility implementation.
    if node.hidden or node.bad or not node.closed or node.tag in {"head", "title"} or HIDDEN_STYLE.search(node.attrs.get("style") or ""):
        return ""
    if node.tag == "br":
        return "\n"
    return "".join(child if isinstance(child, str) else
                   ("\n" if child.tag in BLOCKS else "") + text_visible(child) + ("\n" if child.tag in BLOCKS else "")
                   for child in node.children)


def phone_key(value, country):
    digits = re.sub(r"\D", "", value)
    if country in {"US", "CA"} and len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if country == "GB":
        if digits.startswith("44"):
            digits = digits[2:]
        digits = digits.removeprefix("0")
    return digits if 9 <= len(digits) <= 15 else None


@lru_cache(maxsize=128)
def _address_patterns(gbp_address, country):
    """Look for GBP-anchored street or postcode only, never parse a page as an address."""
    if not gbp_address:
        return []
    from app.report_v22.site_gbp_comparators import normalize_address
    components = normalize_address(gbp_address, country).components
    street = None
    if country == "US" and components.get("house") and components.get("street"):
        street = components["house"] + " " + components["street"]
    else:
        match = re.match(r"\s*\d+[A-Za-z]?\s+[^,\n]+", gbp_address)
        if match:
            street = match.group().strip()
    aliases = {"st": "(?:st|street)", "rd": "(?:rd|road)", "ave": "(?:ave|avenue)",
               "dr": "(?:dr|drive)", "ln": "(?:ln|lane)", "blvd": "(?:blvd|boulevard)"}
    patterns = []
    if street:
        tokens = re.findall(r"\w+", street.casefold())
        patterns.append(("street", r"(?<!\w)" + r"[ \t.\-]+".join(aliases.get(t, re.escape(t)) for t in tokens) + r"(?!\w)"))
    postal = components.get("postal")
    if country == "GB":
        match = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", gbp_address, re.I)
        postal = "".join(match.groups()) if match else None
    if postal:
        patterns.append(("postal", r"(?<![\w-])" + r"[ \t]*".join(re.escape(c) for c in postal) + r"(?![\w-])"))
    return tuple(patterns)


def address_matches(text, gbp_address, country):
    return [(match.group(), kind) for kind, pattern in _address_patterns(gbp_address, country)
            for match in re.finditer(pattern, text, re.I)]


def text_candidates(tree, *, gbp_phone=None, gbp_address=None, country=""):
    from app.report_v22.site_business_html import origin
    from app.report_v22.site_business_models import CandidateDraft
    result = []
    # Cache once: avoids rescanning descendants for each extracted phone/address.
    visible = {tuple(node.path): text_visible(node) for node in tree.nodes}
    for node in tree.nodes:
        ancestor = node.parent
        hidden = False
        while ancestor is not None:
            if ancestor.hidden or ancestor.tag in {"head", "title"} or HIDDEN_STYLE.search(ancestor.attrs.get("style") or ""):
                hidden = True
                break
            ancestor = ancestor.parent
        if hidden:
            continue
        text = visible[tuple(node.path)]
        values = {( "phone", match.group()) for pattern in PHONES for match in pattern.finditer(text)}
        if gbp_phone:
            digits = re.sub(r"\D", "", gbp_phone)
            if 9 <= len(digits) <= 15:
                pattern = r"(?<!\w)\+?" + r"[ \t().\-]*".join(digits) + r"(?!\w)"
                values.update(("phone", match.group()) for match in re.finditer(pattern, text))
        values.update(("address", value) for value, _ in address_matches(text, gbp_address, country))
        values.update(("opening_hours", match.group().strip()) for match in HOURS.finditer(text))
        for field, value in sorted(values):
            if not value or len(value) > (120 if field == "phone" else 240):
                continue
            # Keep the smallest source element containing the actual text.
            if any(value in visible[tuple(child.path)] for child in node.elements):
                continue
            result.append(CandidateDraft(field=field, source_kind="text_pattern", ownership_status="unresolved",
                                         scalar_value=value, origins=[origin(tree, node)],
                                         limitations=[HOURS_NOTE if field == "opening_hours" else TEXT_NOTE]))
            require(len(result) <= tree.budget.limits.max_candidates_per_page, "LIMIT_EXCEEDED")
    return result
