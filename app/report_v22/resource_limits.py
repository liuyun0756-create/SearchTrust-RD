"""Deterministic presentation limits for public v9; sources remain immutable."""
from collections import defaultdict, deque


def select_evidence(ids, source_kind, limit=20):
    ids = set(ids)
    groups = defaultdict(deque)
    for key in sorted(set(ids)):
        groups[source_kind(key)].append(key)
    selected = []
    while len(selected) < limit and any(groups.values()):
        for kind in sorted(groups):
            if groups[kind] and len(selected) < limit:
                selected.append(groups[kind].popleft())
    return sorted(selected), len(set(ids)) - len(selected)
