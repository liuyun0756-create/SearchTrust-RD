from types import SimpleNamespace

import pytest

from app.report_v22.verified_reprioritization_mapping import _first_relation_kind, normalize_query


def test_query_normalization_is_nfkc_whitespace_and_casefold_only() -> None:
    assert normalize_query("  ＰＬＵＭＢＥＲ\tAustin  ") == "plumber austin"
    assert normalize_query("plumber austin") != normalize_query("plumbing austin")


def test_unknown_future_first_party_rule_is_not_silently_accepted() -> None:
    evaluation = SimpleNamespace(rule_id="V22.FIRST_PARTY.GSC.UNKNOWN", target=SimpleNamespace(key="site"))
    with pytest.raises(KeyError):
        _first_relation_kind(evaluation, {})
