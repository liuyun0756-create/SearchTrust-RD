# Worker-observed v2 Map response shape

Regression source: owner/Claude observation in production Worker at commit a0115f3, 2026-11-16: POST /v2/map returned HTTP 200, 4137 bytes, with exactly id/links/success at the top level, and url/title/description link objects.

This fixture reconstructs that observed shape, including id, rather than relying on the documentation-only v2 example. Site URLs, link text and descriptions are placeholders; the request ID uses the representative UUID supplied by the owner. This is a redacted shape fixture, not a byte-for-byte raw archive, so its byte size is not asserted to be 4137. No provider request was made to create it. Existing v1 and no-id v2 fixtures are unchanged.
