# SearchTrust report_v2_1 Backend Support

This package adds backend support for the SearchTrust v2.1 report contract without removing legacy report fields.

## Modules

- `models.py` defines Pydantic v2 models for the `report_v2_1` contract.
- `normalize.py` parses Dify outputs and returns a normalized `{ "report_v2_1": ... }` envelope.
- `validate.py` performs lightweight safety checks before later full schema validation work.
- `dedupe.py` performs deterministic duplicate merging for repeated report content.
- `scoring.py` applies deterministic v2.1 scoring when rule IDs are available.
- `quality.py` rejects native output that makes a high/medium issue or weak/medium layer conclusion without traceable evidence; the pipeline retries the complete Dify workflow for that retryable failure.
- `coverage.py` owns GBP status, the verified GBP profile snapshot, conservative field-level GBP alignment, schema coverage, and page-coverage disclosure.
- `fixtures/` contains example output shapes for local checks.

## Supported output shapes

- Native v2.1 object output: `{ "report_v2_1": { ... } }`.
- Native v2.1 string output: `{ "report_v2_1": "{\"report_v2_1\": {...}}" }` or `{ "report_v2_1": "{...}" }`.
- Legacy production output with `score`, `trust_status`, `ranking_potential`, and `risk_level`.

## Pipeline behavior

`app.tasks.pipeline` validates native output inside the complete Dify retry loop, then attaches `final_report["report_v2_1"]`. Existing `score`, `trust_status`, `ranking_potential`, `risk_level`, and `gbp_connected` fields are preserved.

The backend owns fixed L1-L8 labels, assessment coverage, aggregate score cards, GBP status/source, and any generated GBP alignment row. Dify supplies the report narrative and evidence, not the final contract semantics.

## Known limitations

- Legacy-adapted reports use placeholder `not_available` evidence when old modules do not include structured evidence.
- Deterministic backend scoring preserves Dify/legacy values when `triggered_rule_ids` are unavailable.
- Schema is marked checked only when a direct page response was successfully inspected for JSON-LD. Citations, competitor pages, and Map Pack / geo-grid data remain explicitly not assessed until a dedicated collector is added.
- The contract additions are nested in the existing `reports.report_v2_1` JSONB value; no new Supabase field is required for this release. See `docs/CHANGE_MANAGEMENT.md` before adding any persisted report or agency-branding field.
