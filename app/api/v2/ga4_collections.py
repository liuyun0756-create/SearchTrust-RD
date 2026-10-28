"""Explicit collection confirmation; the BFF supplies authenticated owner identity."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.v2.dependencies import require_internal_auth
from app.api.v2.comparisons import require_comparisons_enabled
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.ga4_collection_entry import Ga4CollectionEntry, Ga4CollectionCommand
from app.report_v22.ga4_collection_storage import Ga4CollectionRepository

router=APIRouter(prefix="/api/v2",tags=["Comparison collections"])


async def enabled():
    await require_comparisons_enabled()
    if not settings.V22_GA4_COMPARISON_COLLECTIONS_ENABLED:
        raise HTTPException(404,detail={"code":"NOT_FOUND"})


async def collection_service():
    async with httpx.AsyncClient() as client:
        try:
            yield Ga4CollectionEntry(Ga4CollectionRepository(url=settings.V22_SUPABASE_URL,
                service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),http_client=client))
        except DeterministicJobError as error:
            if error.error_code=="V22_COMPARISON_NOT_CONFIGURED":
                raise HTTPException(503,detail={"code":"UNAVAILABLE"}) from None
            raise


@router.post("/ga4-comparison-collections",dependencies=[Depends(require_internal_auth),Depends(enabled)])
async def collections(request:Request,service:Ga4CollectionEntry=Depends(collection_service)):
    if request.headers.get("content-type","").split(";")[0].strip()!="application/json":
        raise HTTPException(415,detail={"code":"INVALID"})
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>20000:raise HTTPException(413,detail={"code":"TOO_LARGE"})
    try:command=Ga4CollectionCommand.model_validate_json(bytes(raw))
    except (ValueError,TypeError):raise HTTPException(400,detail={"code":"INVALID"}) from None
    try:
        value=await service.run(command)
        return JSONResponse(value.model_dump(mode="json"),headers={"Cache-Control":"private, no-store"})
    except TransientJobError:raise HTTPException(503,detail={"code":"UNAVAILABLE"}) from None
    except DeterministicJobError as error:
        code=error.error_code
        status=404 if code=="V22_GA4_COLLECTION_NOT_FOUND" else 409 if code in ("V22_GA4_COLLECTION_CONFLICT","V22_GA4_COLLECTION_WAITING","V22_COMPARISON_REJECTED") else 503
        raise HTTPException(status,detail={"code":"WAITING" if code=="V22_GA4_COLLECTION_WAITING" else "NOT_FOUND" if status==404 else "CONFLICT" if status==409 else "UNAVAILABLE"}) from None
