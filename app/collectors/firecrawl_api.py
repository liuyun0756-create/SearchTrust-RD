"""Resolve v2 endpoints from versioned or unversioned Firecrawl base URLs."""

from typing import Literal


def firecrawl_v2_endpoint(api_url: str, operation: Literal["map", "scrape"]) -> str:
    base = api_url.rstrip("/")
    prefix, _, version = base.rpartition("/")
    if version in {"v1", "v2"}:
        base = prefix
    return f"{base}/v2/{operation}"
