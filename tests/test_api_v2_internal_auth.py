import httpx
import pytest

from app.core.config import settings
from app.main import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_v2_runtime_rejects_missing_internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", "expected-token")
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v2/tasks/55555555-5555-4555-8555-555555555555")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INTERNAL_AUTH_FAILED"


@pytest.mark.anyio
async def test_v1_health_does_not_require_redis_or_internal_token() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

