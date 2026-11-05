# P1-05 phase 2 saved GBP regressions

`saved-candidates.json` contains whitelisted, verbatim field excerpts from P0-09
saved `candidates.json` and `result.json`. `provenance` records original file
SHA-256; `source` + `selector` locate each array/object. Extraction checked all
six original file hashes against committed P0-09 `checksums.json` before copying.
Original files remain under `.release-private/2026-11-03-gold/`; tests use this
small excerpt and do not need the private archive or a network connection.

Market lifetime is copied from each site's frozen `bound-p0-10.json.gz` source
binding (one day). Tests evaluate on 2026-11-03 within that original lifetime;
they do not claim these market samples are fresh today. The old lookup candidate
envelopes have no declared expiry; no new expiry is invented for them.

- Seamless: all Q1 market records retained, including Gutter Solutions & Home
  Improvements at position 18, CID 16921898606511158364. No brand guessed from URL.
- Vision-Tech: original two US companies and subsequently saved Vision Tech AV
  LTD. Matching phone observations come from the saved site's page observations.
- Volcanoes: original two matching CIDs. Explicit branch selection is the owner's
  P0-09 decision; address presence is not an automatic selection rule.
- The real address-only shortlink record (`10025 Ashton Brosnaham Dr`) belongs
  to Seamless. The Volcanoes address-only test reuses that saved record as a
  **cross-site negative boundary**. It is not a Volcanoes provider observation.
- The old page regex scan included `8504557246` from an executable Divi script
  (`et_link_options_data`); it is not a visible/contact element. The test request
  marks that observation as unowned. The production bridge uses existing page
  extraction/entity selection, excluding executable scripts and external entities
  (covered separately), rather than trusting the old unscoped regex scan.

Other unit cases are explicitly synthetic boundary variations. Mock provider
responses reuse saved records; transport/key-rotation tests use dummy credentials
and `httpx.MockTransport`. No API request, shortlink fetch or truth mutation occurs.
