# P0-03 regression evidence

- `footer.html`: synthetic pure-text phone/address/hours (no `tel:`, `<address>` or labels).
- `legacy/*.input.json`: synthetic, frozen v3/v4/v5 input manifests and requests saved **before the P0-03 implementation**.
- `legacy/*.report.json`: original `canonical_json_bytes(report)` output, compared byte for byte by `test_frozen_v3_v4_v5_reports_match_pre_change_bytes`. Seven scenarios × three versions; do not regenerate these expected results to accept a failure. `legacy/hashes.json` records the original SHA-256 values.
- `v6-footer.report.json`: actual v6 backend output from the synthetic footer, also copied to the frontend as `src/lib/report-capability/fixtures/v6-footer.json`. The backend test recomputes it; frontend tests validate and render it.
- `visiontech-swansea.html`: unmodified static HTTP response body fetched once from https://visiontechav.com/swansea/ on 2026-11-02. `visiontech-swansea.source.json` records URL, retrieval time, size and SHA-256. No rendering or further crawling was performed. The test snapshot applies the existing HTML field's whitespace normalization before sealing its synthetic checksum.

The Vision-Tech replay uses a **synthetic GBP reference** copied from the page, not a newly acquired or verified GBP snapshot. Its site identity/URL and snapshot envelope are synthetic test bindings. It demonstrates text extraction and conditional comparison only; it does not establish live business identity or actual GBP agreement. The existing unresolved `tel:` observation still blocks phone text fallback. The postcode is only a partial address match, and hours are recorded without alignment.
