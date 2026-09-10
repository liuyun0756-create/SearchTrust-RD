from typing import Any

import httpcore
import pytest

from app.security_v22.http import PinnedNetworkBackend
from app.security_v22.urls import SafeUrl, resolve_public_url


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_dns_is_resolved_once_and_the_connection_uses_only_the_validated_address() -> None:
    resolver_calls = 0

    async def rebinding_resolver(_: str) -> list[str]:
        nonlocal resolver_calls
        resolver_calls += 1
        return ["93.184.216.34"] if resolver_calls == 1 else ["127.0.0.1"]

    target = await resolve_public_url("https://example.com/path", resolver=rebinding_resolver)
    connections: list[tuple[str, int]] = []
    stream = object()

    class Delegate(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host: str, port: int, **_: Any) -> Any:
            connections.append((host, port))
            return stream

        async def connect_unix_socket(self, path: str, **_: Any) -> Any:
            raise AssertionError(path)

        async def sleep(self, seconds: float) -> None:
            return None

    backend = PinnedNetworkBackend(target, delegate=Delegate())
    assert await backend.connect_tcp("example.com", 443) is stream
    assert resolver_calls == 1
    assert connections == [("93.184.216.34", 443)]


@pytest.mark.anyio
async def test_pinned_transport_rejects_a_host_substitution_before_connecting() -> None:
    target = SafeUrl(
        normalized_url="https://example.com/",
        request_url="https://example.com/",
        scheme="https",
        hostname="example.com",
        port=443,
        addresses=("93.184.216.34",),
    )

    class Delegate(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host: str, port: int, **_: Any) -> Any:
            raise AssertionError("unsafe delegate connection")

        async def connect_unix_socket(self, path: str, **_: Any) -> Any:
            raise AssertionError(path)

        async def sleep(self, seconds: float) -> None:
            return None

    backend = PinnedNetworkBackend(target, delegate=Delegate())
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("attacker.example", 443)
