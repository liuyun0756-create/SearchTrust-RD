from uuid import UUID

import httpx
import pytest

from app.api.v2.models import ModuleAvailability, PreflightResponse
from app.api.v2.preflight import get_preflight_service
from app.core.config import settings
from app.main import create_app
from app.preflight_v22.urls import UrlSafetyError


AUTH_HEADERS = {"Authorization": "Bearer test-internal-token"}
MODULE_KEYS = [
    "site_inventory",
    "site_deep_analysis",
    "serp_maps",
    "serp_local_pack",
    "serp_organic",
    "public_gbp",
    "competitor_analysis",
    "pagespeed",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def response_payload() -> PreflightResponse:
    return PreflightResponse(
        preflight_id=UUID("11111111-1111-4111-8111-111111111111"),
        normalized_site_url="https://example.com/",
        module_availability=[
            ModuleAvailability(module_key=key, available=True, reason="Prerequisites available.")
            for key in MODULE_KEYS
        ],
        estimated_duration_bucket="10_to_15_minutes",
        coverage_summary="All eight later public-data modules meet their prerequisites.",
    )


class FakeService:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    async def run(self, _):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def build_app(monkeypatch: pytest.MonkeyPatch, service: FakeService, *, enabled: bool = True):
    monkeypatch.setattr(settings, "V22_PREFLIGHT_ENABLED", enabled)
    monkeypatch.setattr(settings, "V22_INTERNAL_API_TOKEN", "test-internal-token")
    app = create_app()
    app.state.v22_runtime = None
    app.dependency_overrides[get_preflight_service] = lambda: service
    return app


@pytest.mark.anyio
async def test_preflight_feature_flag_blocks_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(AssertionError("service should not run"))
    app = build_app(monkeypatch, service, enabled=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v2/preflight",
            headers=AUTH_HEADERS,
            json={"site_url": "https://example.com"},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "V22_PREFLIGHT_NOT_READY"
    assert service.calls == 0


@pytest.mark.anyio
async def test_preflight_requires_internal_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(response_payload())
    app = build_app(monkeypatch, service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v2/preflight",
            json={"site_url": "https://example.com"},
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INTERNAL_AUTH_FAILED"
    assert service.calls == 0


@pytest.mark.anyio
async def test_preflight_returns_frozen_response_without_queue_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(response_payload())
    app = build_app(monkeypatch, service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v2/preflight",
            headers=AUTH_HEADERS,
            json={"site_url": "https://example.com"},
        )

    assert response.status_code == 200
    assert response.json() == response_payload().model_dump(mode="json")
    assert service.calls == 1


@pytest.mark.anyio
async def test_preflight_rejects_unknown_request_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(response_payload())
    app = build_app(monkeypatch, service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v2/preflight",
            headers=AUTH_HEADERS,
            json={"site_url": "https://example.com", "finding": "forbidden"},
        )

    assert response.status_code == 422
    assert service.calls == 0


@pytest.mark.anyio
async def test_preflight_maps_url_safety_error_to_stable_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(UrlSafetyError("URL_ADDRESS_FORBIDDEN", "Unsafe public target."))
    app = build_app(monkeypatch, service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v2/preflight",
            headers=AUTH_HEADERS,
            json={"site_url": "http://127.0.0.1"},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "URL_ADDRESS_FORBIDDEN"


def test_preflight_openapi_uses_frozen_response_model(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(response_payload())
    app = build_app(monkeypatch, service)

    operation = app.openapi()["paths"]["/api/v2/preflight"]["post"]

    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/PreflightResponse")
