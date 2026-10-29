from types import SimpleNamespace
from app.report_v22.search_relevance import organic_scope_supported


def row(title, snippet=None, url=None):
    return SimpleNamespace(display_name=title, snippet=snippet, url=url)


def test_unrelated_me_results_do_not_support_plumbing_visibility():
    assert not organic_scope_supported("Plumbing repair near me", [row("ME/CFS Basics"), row("WHAT IS M.E.?", url="https://me.example/")])


def test_generic_topic_only_does_not_establish_specific_query_context():
    assert not organic_scope_supported("best Plumbing repair Fresno", [row("Plumbing"), row("Plumbing licensing basics")])
    assert not organic_scope_supported("best Plumbing repair Fresno", [row("Plumbing repairs explained")], market="Fresno")
    assert organic_scope_supported("best Plumbing repair Fresno", [row("Plumbing repairs in Fresno")], market="Fresno")


def test_local_directory_and_service_inflections_are_eligible_without_domain_bias():
    assert organic_scope_supported("best Plumbing repair Fresno", [row("Plumbers in Fresno", url="https://directory.example/")])
    assert organic_scope_supported("water heater repair", [row("Water heaters", "Maintenance and repairs")])


def test_context_terms_cannot_be_assembled_from_unrelated_records():
    assert not organic_scope_supported("Plumbing repair Fresno", [row("Plumbing"), row("Fresno city council")])
    assert not organic_scope_supported("best near me", [row("Anything")])
