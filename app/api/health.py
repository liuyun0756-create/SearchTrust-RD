"""Public liveness endpoint independent of report generation runtimes."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


router = APIRouter(prefix="/api/v1", tags=["Health"])


@router.get("/health", response_model=HealthResponse, summary="Service health check")
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.APP_VERSION)

