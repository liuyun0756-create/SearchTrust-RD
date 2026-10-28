"""Authenticated, gated reads only; never dispatch a collection or a report."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.v2.dependencies import require_internal_auth
from app.api.v2.ga4_collections import enabled as collections_enabled
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.ga4_collection_storage import Ga4CollectionRepository
from app.report_v22.ga4_session_reading import Ga4SessionCommand, Ga4SessionService

router = APIRouter(prefix="/api/v2", tags=["Saved GA4 sessions"])


async def enabled():
    await collections_enabled()
    if not settings.V22_GA4_SESSION_SNAPSHOT_READING_ENABLED:
        raise HTTPException(404, detail={"code": "NOT_FOUND"})


async def session_service():
    async with httpx.AsyncClient() as client:
        try:
            yield Ga4SessionService(Ga4CollectionRepository(url=settings.V22_SUPABASE_URL,
                service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client))
        except DeterministicJobError as error:
            if error.error_code == "V22_COMPARISON_NOT_CONFIGURED":
                raise HTTPException(503, detail={"code": "UNAVAILABLE"}) from None
            raise


@router.post("/ga4-session-readings", dependencies=[Depends(require_internal_auth), Depends(enabled)])
async def reading(request: Request, service: Ga4SessionService = Depends(session_service)):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, detail={"code": "INVALID"})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4000:
            raise HTTPException(413, detail={"code": "TOO_LARGE"})
    try:
        command = Ga4SessionCommand.model_validate_json(bytes(raw))
    except (ValueError, TypeError):
        raise HTTPException(400, detail={"code": "INVALID"}) from None
    try:
        result = await service.run(command)
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "private, no-store"})
    except TransientJobError:
        raise HTTPException(503, detail={"code": "UNAVAILABLE"}) from None
    except DeterministicJobError as error:
        status = 404 if error.error_code == "V22_GA4_SESSION_NOT_FOUND" else 409 if error.error_code in (
            "V22_GA4_SESSION_CONFLICT", "V22_COMPARISON_REJECTED") else 503
        raise HTTPException(status, detail={"code": "NOT_FOUND" if status == 404 else "CONFLICT" if status == 409 else "UNAVAILABLE"}) from None
