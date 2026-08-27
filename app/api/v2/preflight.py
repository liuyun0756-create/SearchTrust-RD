"""Authenticated low-cost preflight route for SearchTrust API v2."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.v2.dependencies import (
    parse_preflight_request,
    require_internal_auth,
    require_v22_preflight_enabled,
)
from app.api.v2.models import PreflightRequest, PreflightResponse
from app.preflight_v22.service import PreflightService, build_preflight_service
from app.preflight_v22.urls import UrlSafetyError


router = APIRouter(prefix="/api/v2", tags=["SearchTrust v2 preflight"])


async def get_preflight_service(request: Request) -> PreflightService:
    injected = getattr(request.app.state, "v22_preflight_service", None)
    if injected is not None:
        return injected
    runtime = getattr(request.app.state, "v22_runtime", None)
    redis = runtime.redis if runtime is not None else None
    return build_preflight_service(redis)


@router.post("/preflight", response_model=PreflightResponse)
async def run_preflight(
    body: Annotated[PreflightRequest, Depends(parse_preflight_request)],
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_preflight_enabled)],
    service: Annotated[PreflightService, Depends(get_preflight_service)],
) -> PreflightResponse:
    try:
        return await service.run(body)
    except UrlSafetyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.user_message},
        ) from exc
