# Nested local-pack website fixtures

These fixtures are projections of two previously saved real SerpAPI Google Search Nova archives, not new provider requests and not the All Star raw response. The original archive bytes are unchanged and are not committed.

- `local-pack-nested-redacted-1.json`: source `nova-serp-archive-6ae1e06df479a3a9495817d2.json`, SHA-256 `319c766e40deebcb7f2725511fd574dcec194a0ad84325506c183f8d4a5a8617`; 4 places, 3 with links.website, 0 titled entries without links.
- `local-pack-nested-redacted-2.json`: source `nova-serp-archive-6ae1e06fb6981e41408db151.json`, SHA-256 `9d59dea06b3d48647bb06db118e1a222a35bc195e80694ccc1ce53596f353430`; 5 places, 3 with links.website, 1 titled entries without links.

Redaction retains the local_results.places wrapper, original position numbers, presence/absence of title and the website/directions nesting. Business titles are placeholders; every retained URL uses a reserved .example hostname. Phone (including links.phone/whatsapp), addresses, lsig, place_id/search IDs, provider IDs, coordinates, images, descriptions, hours and unrelated response content are removed. Directions is a placeholder only, included to verify the sanitizer drops it. Missing titles and missing URLs are preserved as missing, never filled in. The original flat google-search.json fixture is unchanged.

Expected: 9 places total; 6 nested website records can yield a URL/domain, 1 titled record has no website and must keep nulls with a skipped-result limitation, and 2 records have no title and remain skipped. This does not prove that the 11 already-cleaned All Star records originally contained website URLs, or that any production report limitation is eliminated.

## Validation (2026-11-11)

Parser cache namespace: `serp_market_v3`. Only the reusable cache key changes; the snapshot ID formula and reads pinned to an existing snapshot ID are unchanged. Neither source archive nor any frozen snapshot is rewritten.

Executed from the backend repository:

```bash
.venv/bin/python -m pytest -q tests/test_v22_local_pack_urls.py tests/test_v22_local_pack_wrappers.py tests/test_v22_market_parser_cache.py tests/test_v22_serp_market_collector.py tests/test_v22_serp_market_stage.py
# 57 passed in 1.33s
.venv/bin/python scripts/run_v22_backend_quality.py fast
# 4222 passed, 265 deselected in 382.71s (0:06:22)
```

The three requested regressions in `tests/test_v22_local_pack_urls.py` passed:

- `test_real_redacted_nested_local_pack_urls_survive_sanitization`: both archived shapes (2 cases).
- `test_existing_flat_local_pack_urls_still_normalize`: existing flat fixture (1 case).
- `test_missing_urls_remain_null_and_only_local_pack_adds_result_skipped`: local_pack/maps/organic (3 cases). Only local_pack adds RESULT_SKIPPED for a missing URL; usable maps/organic records remain available without that limitation.

Additional checks cover the four-level safe-URL priority, invalid URL fallback, strict nested key/type/length bounds, non-object links, unchanged maps/organic selection, pre-fix canonical checksums for the existing flat fixtures, repeated checkpoint sanitization/reuse, and v2-to-v3 cache isolation while preserving historical bytes/checksum/expiry/TTL. Source archive SHA-256 values above were also rechecked unchanged.

These regressions use saved responses and fake providers; pytest blocks external sockets. Local validation involved no collection, payment, production data write or smoke test. CI and deployment are separate subsequent release steps. The fast command explicitly excludes Redis/native integration tests (265 deselected); these were not run locally because the Docker daemon was unavailable.

Production snapshot `badc0b7a-b9d8-3807-83fb-588b5ca03ebc` has NULL raw_payload and its cleaned records cannot recover discarded links. A separately authorized new real collection is still needed to verify actual local_pack URL/domain recovery, changes to findings, and whether the report limitations remain. This round makes no claim that those production limitations have been eliminated.
