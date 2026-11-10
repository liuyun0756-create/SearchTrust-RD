# Frozen local-pack structure

`local-pack-partial-redacted.json` preserves the Google Search wrapper and the
field presence of an existing frozen response; it contains no original query,
business details, search ID, `lsig` values, credentials or original result URL.

Read-only sources (never edited or submitted):

- `.release-private/2026-10-28/nova-serp-archive-6ae1e06df479a3a9495817d2.json`
  SHA-256 `319c766e40deebcb7f2725511fd574dcec194a0ad84325506c183f8d4a5a8617`:
  `engine=google`, `local_results` is an object, `places` contains four entries.
- `.release-private/2026-10-28/nova-serp-archive-6ae1e06fb6981e41408db151.json`
  SHA-256 `9d59dea06b3d48647bb06db118e1a222a35bc195e80694ccc1ce53596f353430`:
  same wrapper, five entries.

Both lists contain entries with only `position` and `lsig`. They prove the
wrapper exists, not that named local businesses can be recovered. Tests retain
`RESULT_SKIPPED` for these entries, without inventing names or reporting a
`SECTION_INVALID` for the valid wrapper. Named-result tests use the existing
synthetic `google-search.json` records within the same wrapper.

SerpAPI's primary documentation corroborates this shape:
https://serpapi.com/local-pack . It does not justify marking a returned local
pack as "not requested". No new provider calls were made for these fixtures.
