# Fresno location fixtures

Captured 2026-11-06 from the public SerpApi `locations.json` endpoint
(no API key, no cost) with `limit=10`. Fields trimmed to those read by
`app/collectors/serp_market_location.py`. Queries:

- `locations-fresno-93722.json` — `q=93722` (postal code only result)
- `locations-fresno-ca.json` — `q=Fresno, CA` (returns only the Fresno-Visalia DMA)
- `locations-fresno-california.json` — `q=Fresno, California` (City first)

Source of the regression: All Star Plumbing Fresno smoke test, 2026-11-06,
`SERP_LOCATION_NOT_FOUND`. See search-trust docs/acquisition-plan/reviews/release-2026-11-06.md.
