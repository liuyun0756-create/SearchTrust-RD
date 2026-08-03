# SearchTrust report_v2_1 Backend Support

This package adds backend support for the SearchTrust v2.1 report contract without removing legacy report fields.

## Modules

- `models.py` defines Pydantic v2 models for the `report_v2_1` contract.
- `normalize.py` parses Dify outputs and returns a normalized `{ "report_v2_1": ... }` envelope.
- `validate.py` performs lightweight safety checks before later full schema validation work.
- `dedupe.py` performs deterministic duplicate merging for repeated report content.
- `scoring.py` applies deterministic v2.1 scoring when rule IDs are available.
- `business_presence.py` builds the backend-owned Business Presence Audit from page and public GBP observations.
- `fixtures/` contains example output shapes for local checks.

## Supported output shapes

- Native v2.1 object output: `{ "report_v2_1": { ... } }`.
- Native v2.1 string output: `{ "report_v2_1": "{\"report_v2_1\": {...}}" }` or `{ "report_v2_1": "{...}" }`.
- Legacy production output with `score`, `trust_status`, `ranking_potential`, and `risk_level`.

## Pipeline behavior

`app.tasks.pipeline` attaches `final_report["report_v2_1"]` after the existing legacy parsing step. Existing `score`, `trust_status`, `ranking_potential`, `risk_level`, and `gbp_connected` fields are preserved.

The backend overwrites `business_presence_audit` after Dify returns. Dify remains responsible for report conclusions; it is not the source of truth for page/GBP values, review excerpts, or comparison results. Business Presence Audit checks are informational and do not change rule hits, layer statuses, scoring ratios, or thresholds.

## Business Presence Audit

- GBP x Page checks cover observed business name, phone, address, website, hours, service area, and categories/service intent.
- Missing public GBP fields remain `not_checked` when the provider response is insufficient to verify a comparison.
- Service area is the explicit exception: when GBP was checked, the page identifies a service area, and the GBP response returns no service-area value, the comparison is a `mismatch`. An explicitly empty service area for a service-area business can be `missing`; storefront-only businesses can be `not_applicable`.
- Review analysis is limited to the 30 most recent publicly returned reviews. The report records the actual sample size and treats a zero-record endpoint response as partial/error when the profile reports existing reviews.
- Photo and post counts represent public records returned by the configured provider. Missing dates remain explicit limitations.
- External citations/NAP network checking is deferred and appears only as `not_checked` in Audit Scope.

The audit summary and bounded review sample are stored inside the existing report JSON. No database schema change is required for this release. A future decision to retain large raw review/photo/post histories would require a separate database design and a manual database migration before deployment.

## Known limitations

- Legacy-adapted reports use placeholder `not_available` evidence when old modules do not include structured evidence.
- Deterministic backend scoring preserves Dify/legacy values when `triggered_rule_ids` are unavailable.
