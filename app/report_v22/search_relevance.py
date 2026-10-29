"""Conservative lexical admission, not a ranking or semantic-quality score.

Passing does not prove relevance. Failing means the sample cannot support a
visibility judgment; records and their checksums remain unchanged for review.
"""
import re
import unicodedata

_QUALIFIERS = {"best", "top", "near", "me", "the", "a", "an", "in", "of", "for", "and", "to"}


def _terms(value):
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())
    result = set()
    for word in words:
        if word in _QUALIFIERS or len(word) < 3:
            continue
        # Share common English inflections, e.g. plumbing/plumber/plumbers.
        for suffix in ("ers", "ing", "er", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[:-len(suffix)]
                break
        result.add(word)
    return result


def organic_scope_supported(query, records, *, market=None):
    terms = _terms(query)
    if not terms:
        return False
    required = min(2, len(terms))
    location = _terms(market or "")
    explicit_location = location if location <= terms else set()
    for record in records:
        observed = _terms(" ".join(filter(None, (
            record.display_name, record.snippet, str(record.url) if record.url else None,
        ))))
        if len(terms & observed) >= required and explicit_location <= observed:
            return True
    return False
