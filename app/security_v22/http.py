"""DNS-pinned HTTP transport shared by V2.2 public URL fetchers."""

from app.preflight_v22.fetcher import (
    _PinnedAsyncHTTPTransport as PinnedAsyncHTTPTransport,
)
from app.preflight_v22.fetcher import _PinnedNetworkBackend as PinnedNetworkBackend

__all__ = ["PinnedAsyncHTTPTransport", "PinnedNetworkBackend"]
