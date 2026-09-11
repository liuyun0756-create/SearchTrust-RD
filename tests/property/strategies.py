"""Bounded, synthetic Hypothesis strategies shared by V2.2 property tests."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import strategies as st


SAFE_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=" -_",
    ),
    min_size=1,
    max_size=40,
).filter(lambda value: bool(value.strip()))

JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(
        min_value=-(10**9),
        max_value=10**9,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    ),
    SAFE_TEXT,
)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=6),
        st.dictionaries(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12),
            children,
            max_size=6,
        ),
    ),
    max_leaves=20,
)

DOMAIN_LABEL = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=12,
).filter(lambda value: value[0].isalnum() and value[-1].isalnum())
DOMAINS = st.lists(DOMAIN_LABEL, min_size=2, max_size=4).map(".".join)
URLS = st.builds(
    lambda domain, path: f"https://{domain}/{path}",
    DOMAINS,
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", max_size=30),
)


def identifiers(prefix: str) -> st.SearchStrategy[str]:
    return st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
        min_size=3,
        max_size=24,
    ).filter(lambda value: value[0].isalnum()).map(lambda value: f"{prefix}_{value}")


COUNTER_VALUES = st.integers(min_value=0, max_value=10**9)
MONEY_MICROS = st.integers(min_value=0, max_value=10**12)
PROVIDER_OUTCOMES = st.lists(
    st.sampled_from(("success", "failure", "unknown")),
    min_size=1,
    max_size=12,
)


@dataclass(frozen=True)
class EvidenceGraph:
    evidence_ids: tuple[str, ...]
    finding_evidence: tuple[tuple[str, tuple[str, ...]], ...]
    action_findings: tuple[tuple[str, tuple[str, ...]], ...]


@st.composite
def evidence_graphs(draw: st.DrawFn) -> EvidenceGraph:
    evidence_count = draw(st.integers(min_value=1, max_value=8))
    finding_count = draw(st.integers(min_value=1, max_value=5))
    evidence = tuple(f"ev_generated_{index}" for index in range(evidence_count))
    findings: list[tuple[str, tuple[str, ...]]] = []
    for index in range(finding_count):
        refs = draw(
            st.lists(
                st.sampled_from(evidence),
                min_size=1,
                max_size=evidence_count,
                unique=True,
            )
        )
        findings.append((f"fn_generated_{index}", tuple(sorted(refs))))
    finding_ids = tuple(item[0] for item in findings)
    actions = tuple(
        (
            f"ac_generated_{index}",
            tuple(
                sorted(
                    draw(
                        st.lists(
                            st.sampled_from(finding_ids),
                            min_size=1,
                            max_size=finding_count,
                            unique=True,
                        )
                    )
                )
            ),
        )
        for index in range(1, 4)
    )
    return EvidenceGraph(evidence, tuple(findings), actions)
