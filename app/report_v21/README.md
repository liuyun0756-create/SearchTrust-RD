# SearchTrust report_v2_1 Backend Support

This package adds backend support for the SearchTrust v2.1 report contract without removing legacy report fields.

## Modules

- `models.py` defines Pydantic v2 models for the `report_v2_1` contract.
- `normalize.py` parses Dify outputs and returns a normalized `{ "report_v2_1": ... }` envelope.
- `validate.py` performs lightweight safety checks before later full schema validation work.
- `dedupe.py` performs deterministic duplicate merging for repeated report content.
- `scoring.py` applies deterministic v2.1 scoring when rule IDs are available.
- `fixtures/` contains example output shapes for local checks.

## Supported output shapes

- Native v2.1 object output: `{ "report_v2_1": { ... } }`.
- Native v2.1 string output: `{ "report_v2_1": "{\"report_v2_1\": {...}}" }` or `{ "report_v2_1": "{...}" }`.
- Legacy production output with `score`, `trust_status`, `ranking_potential`, and `risk_level`.

## Pipeline behavior

`app.tasks.pipeline` attaches `final_report["report_v2_1"]` after the existing legacy parsing step. Existing `score`, `trust_status`, `ranking_potential`, `risk_level`, and `gbp_connected` fields are preserved.

## Known limitations

- Legacy-adapted reports use placeholder `not_available` evidence when old modules do not include structured evidence.
- Deterministic backend scoring preserves Dify/legacy values when `triggered_rule_ids` are unavailable.
