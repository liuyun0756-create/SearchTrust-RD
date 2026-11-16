# Map response regression shapes

The v1 fixture reconstructs the owner-confirmed 2026-11-16 Worker observation: HTTP 200, success=true, 18 links, and exactly success/links/warnings/replacement top-level fields. It is a shape regression, not a raw response archive. All site URLs and page text are replaced by fixture.example placeholders.

The v2 fixture uses the documented url/title/description link objects with the same placeholder URLs. Source: https://docs.firecrawl.dev/api-reference/endpoint/map . No provider requests were made to create either fixture. Warning contents are never included in application logs.
