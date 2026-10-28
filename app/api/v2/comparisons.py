"""Free comparison entry. Owner identity is supplied only by the authenticated BFF."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.v2.dependencies import require_internal_auth
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.comparison_entry import ComparisonEntry, EntryCommand
from app.report_v22.comparison_storage import ComparisonRepository

router = APIRouter(prefix="/api/v2", tags=["Comparison checks"])


async def require_comparisons_enabled():
    if not settings.V22_COMPARISONS_ENABLED or not settings.V22_CAPABILITY_EXECUTION_ENABLED:
        raise HTTPException(404, detail={"code":"NOT_FOUND"})


async def comparison_service():
    async with httpx.AsyncClient() as client:
        try:
            yield ComparisonEntry(ComparisonRepository(url=settings.V22_SUPABASE_URL,
                service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client))
        except DeterministicJobError as error:
            if error.error_code == "V22_COMPARISON_NOT_CONFIGURED":
                raise HTTPException(503, detail={"code":"UNAVAILABLE"}) from None
            raise


@router.post("/comparisons", dependencies=[Depends(require_internal_auth), Depends(require_comparisons_enabled)])
async def comparisons(request: Request, service: ComparisonEntry = Depends(comparison_service)):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, detail={"code":"INVALID"})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>20000:
            raise HTTPException(413, detail={"code":"TOO_LARGE"})
    try:
        command = EntryCommand.model_validate_json(bytes(raw))
    except (ValueError, TypeError):
        raise HTTPException(400, detail={"code":"INVALID"}) from None
    try:
        result = await service.run(command)
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control":"private, no-store"})
    except TransientJobError:
        raise HTTPException(503, detail={"code":"UNAVAILABLE"}) from None
    except DeterministicJobError as error:
        code = error.error_code
        status = 404 if code == "V22_COMPARISON_NOT_FOUND" else 409 if code in {"V22_COMPARISON_CONFLICT","V22_COMPARISON_REJECTED"} else 503
        raise HTTPException(status, detail={"code":"NOT_FOUND" if status==404 else "CONFLICT" if status==409 else "UNAVAILABLE"}) from None
