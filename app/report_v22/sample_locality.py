"""V10-only offline geographic admission, with auditable source fragments.

Request coordinates never locate a result. Unknown records remain in N.
"""
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from app.report_v22.site_gbp_comparators import _US_REGIONS

LOCALITY_NOTE = "This organic search sample mostly reflected other areas, so it is not used to judge local visibility."


def words(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def canonical_url(value):
    p = urlsplit(str(value))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(sorted(parse_qsl(p.query, keep_blank_values=True))), ""))


def classify_record(record, market):
    get = record.get if isinstance(record, dict) else lambda k, default=None: getattr(record, k, default)
    city = words(market.city or "")
    region = words(market.region or "")
    region = _US_REGIONS.get(region, region)
    signals = []
    query_terms = set(words(get("query", "")).split()) | {"best", "top", "hour", "services", "service"}

    def add(field, fragment, place, state=None, uncertain=False):
        place = words(place)
        if not place and not state:
            return
        signals.append(dict(field=field, fragment=fragment, city=place or None,
                            region=state, uncertain=uncertain or bool(re.search(r"\b(county|region|area)\b", place))))

    def clean_place(raw):
        raw = re.split(r"[·;:|]|\b(?:in|near|serving|serves)\s+", raw, flags=re.I)[-1]
        tokens = words(raw).split()
        while tokens and (tokens[0] in query_terms or tokens[0].isdigit()):
            tokens.pop(0)
        return " ".join(tokens)

    # Explicit location parameter, never arbitrary words in a hostname.
    url = str(get("url") or "")
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.casefold() == "find_loc":
            m = re.fullmatch(r"(.+?),\s*([A-Za-z]{2})(?:\s+\d[\d -]*)?", value)
            if m and m[2].lower() in _US_REGIONS.values():
                add("url", value, m[1], m[2].lower())
    parts = [unquote(p) for p in urlsplit(url).path.strip("/").split("/")]
    if market.country_code == "US":
        for i, part in enumerate(parts):
            code = _US_REGIONS.get(words(part))
            if i and parts[i-1] == "united-states" and code:
                add("url", "/".join(parts[i-1:i+1]), "", code)
            elif i and parts[i-1] == "near-me" and code and i+1 < len(parts):
                add("url", "/".join(parts[i-1:i+2]), parts[i+1], code)
            elif part.casefold() in _US_REGIONS.values() and i+1 == len(parts)-1:
                add("url", "/".join(parts[i:]), parts[i+1], part.casefold())
    texts = [(field, get(field) or "") for field in ("address", "display_name", "snippet")]
    for field, text in texts:
        if city and re.search(r"(?<!\w)" + re.escape(city) + r"(?!\w)", words(text)):
            add(field, market.city, city)
        if market.country_code != "US":
            continue
        # State abbreviations need a clear token boundary; IN in ordinary prose
        # is not a state. Upper-case postal forms only, with a comma or title start.
        pattern = r"([A-Za-z][A-Za-z .'’-]*?),\s*([A-Z]{2})(?=\s|[.,;]|$)"
        for m in re.finditer(pattern, text):
            if m[2].lower() in _US_REGIONS.values():
                add(field, m[0], clean_place(m[1]), m[2].lower())
        if field == "display_name":
            m = re.match(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\s+([A-Z]{2})\s", text)
            if m and m[2].lower() in _US_REGIONS.values():
                add(field, m[0], m[1], m[2].lower())
            for name, code in _US_REGIONS.items():
                # Full state at the end (Santa Clara California), or explicit
                # state-level destination (in Idaho, United States).
                m = re.search(r"\bin\s+(" + re.escape(name) + r"),\s*United States\b", text, re.I)
                if m:
                    add(field, m[0], "", code)
                m = re.fullmatch(r"([A-Za-z .'-]+)\s+(" + re.escape(name) + r")", text, re.I)
                if m:
                    add(field, m[0], clean_place(m[1]), code)
        # Explicit unqualified business location must not borrow a state from a
        # different city elsewhere in the result (e.g. Wichita vs Hutchinson).
        for m in re.finditer(r"\b(?:based in|located in|serving|serves|in|near)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)(?=,|\s+(?:but|and|serving)|[.;·]|$)", text):
            region_code = _US_REGIONS.get(words(m[1]))
            if region_code and any(s["field"] == field and s["city"] is None and s["region"] == region_code for s in signals):
                continue  # Same explicit state span, not a second city.
            add(field, m[0], m[1])
    # Merge only identical cities. A bare target-city mention can share an
    # explicit state for that SAME city; never transfer it to another city.
    places = {}
    for s in signals:
        places.setdefault(s["city"], set()).add(s["region"])
    normalized = set()
    for place, states in places.items():
        explicit = states - {None}
        for state in explicit or {None}:
            normalized.add((place, state))
    uncertain = any(s["uncertain"] for s in signals)
    if not normalized or uncertain:
        state, reason = "unknown", "no_unambiguous_location"
    elif len(normalized) > 1:
        outside = bool(region) and all(r is not None and r != region for _, r in normalized)
        state, reason = ("non_local", "all_locations_explicitly_outside_region") if outside else ("unknown", "multiple_locations_not_all_outside_region")
    else:
        place, observed_region = next(iter(normalized))
        if observed_region and region and observed_region != region:
            state, reason = "non_local", "different_region"
        elif place == city and city:
            state, reason = "local", "target_city"
        elif place and observed_region and region == observed_region:
            state, reason = "non_local", "different_city"
        else:
            state, reason = "unknown", "insufficient_location"
    return dict(state=state, reason=reason, signals=signals)


def audit_sample(records, market):
    rows, groups = [], {}
    for index, record in enumerate(records):
        get = record.get if isinstance(record, dict) else lambda k, default=None: getattr(record, k, default)
        url = get("url")
        row = dict(index=index, position=get("position"), url=str(url) if url else None,
                   **classify_record(record, market))
        if not url:
            row.update(state="unknown", reason="url_missing")
        key = canonical_url(url) if url else f"missing:{index}"
        groups.setdefault(key, []).append(row)
        rows.append(row)
    votes = []
    for group in groups.values():
        # Duplicate URLs with different assertions cannot reinforce a majority.
        states = {r["state"] for r in group}
        places = {tuple(sorted({(s["city"] or "", s["region"] or "") for s in r["signals"]})) for r in group}
        state = next(iter(states)) if len(states) == 1 and len(places) == 1 else "unknown"
        for row in group:
            row["vote_state"] = state
        votes.append(state)
    n, o, l = len(votes), votes.count("non_local"), votes.count("local")
    return dict(N=n, O=o, L=l, unknown=n-o-l, non_local_majority=n >= 3 and o > n / 2, rows=rows)


def audit_source(source, market):
    """Internal audit only: every result type, including non-filtered Maps."""
    audits = []
    for run_index, run in enumerate(source.payload.query_runs):
        for kind in ("maps", "local_pack", "organic"):
            records = [(i, r) for i, r in enumerate(run.results) if r.result_type == kind]
            audit = audit_sample([r for _, r in records], market)
            for row in audit["rows"]:
                source_index = records[row["index"]][0]
                for signal in row["signals"]:
                    signal["field_path"] = f"/payload/query_runs/{run_index}/results/{source_index}/{signal['field']}"
            audits.append(dict(query=run.query, result_type=kind, snapshot_id=str(source.binding.snapshot_id), **audit))
    return audits
