import re

from app.report_v22.public_action_catalog import ACTION_TEMPLATES, RULE_ACTIONS
from app.report_v22.public_rule_catalog import GBP_RULES, RULES


def test_catalog_covers_every_current_public_finding_rule() -> None:
    assert set(RULE_ACTIONS) == {*RULES, *GBP_RULES}
    assert {binding.template_key for binding in RULE_ACTIONS.values()} <= set(ACTION_TEMPLATES)


def test_catalog_versions_match_the_current_rule_contract() -> None:
    for rule_id, binding in RULE_ACTIONS.items():
        expected = "1.1.0" if rule_id in GBP_RULES else "1.0.0"
        assert binding.rule_version == expected


def test_catalog_templates_do_not_contain_outcome_promises_or_fabricated_metrics() -> None:
    encoded = repr(ACTION_TEMPLATES).casefold()
    forbidden = [r"\d+%", r"guarantee", r"revenue", r"lead growth", r"traffic growth", r"rank #?1"]
    assert not any(re.search(pattern, encoded) for pattern in forbidden)
    for template in ACTION_TEMPLATES.values():
        assert isinstance(template.steps, tuple)
        assert isinstance(template.definition_of_done, tuple)
        assert isinstance(template.required_client_assets, tuple)
        assert template.steps
        assert template.definition_of_done
        assert template.metric_key
        assert template.allowed_source_types
        assert template.copy_allowed_fact_fields
