"""Internal authentication and runtime dependencies for API v2."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status
from pydantic import SecretStr
from pydantic import ValidationError

from app.api.v2.models import AnalyzeRequest
from app.core.config import settings


def _secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


async def require_internal_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = _secret_value(settings.V22_INTERNAL_API_TOKEN)
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INTERNAL_AUTH_FAILED", "message": "Internal authentication failed."},
        )


async def require_v22_analyze_enabled() -> None:
    if not settings.V22_ANALYZE_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "V22_ANALYSIS_NOT_READY",
                "message": "SearchTrust v2.2 analysis is not available yet.",
            },
        )


async def get_v22_runtime(request: Request):
    runtime = getattr(request.app.state, "v22_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "QUEUE_UNAVAILABLE", "message": "The durable task queue is unavailable."},
        )
    return runtime


async def parse_analyze_request(request: Request) -> AnalyzeRequest:
    """Validate from raw JSON so strict UUID/date fields retain JSON semantics."""

    try:
        return AnalyzeRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": "The analysis request is invalid."},
        ) from exc
