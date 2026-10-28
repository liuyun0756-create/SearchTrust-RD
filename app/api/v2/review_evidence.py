"""Default-off, internal-auth owner workspace. Reads stored evidence only."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.v2.dependencies import require_internal_auth
from app.core.config import settings
from app.jobs_v22.digest import canonical_json_bytes
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.review_freeze_storage import ReviewFreezeRepository, decode
from app.report_v22.review_workspace import ReviewWorkspace
from app.report_v22.review_workspace_models import command_adapter

router = APIRouter(prefix="/api/v2", tags=["Review evidence workspace"])
HEADERS = {"Cache-Control": "private, no-store"}


async def enabled():
    if not settings.V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED or not settings.V22_CAPABILITY_EXECUTION_ENABLED:
        raise HTTPException(404, detail={"code": "NOT_FOUND"}, headers=HEADERS)


async def workspace_service():
    async with httpx.AsyncClient() as client:
        try:
            yield ReviewWorkspace(ReviewFreezeRepository(url=settings.V22_SUPABASE_URL,
                service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client,
                allow_multi_round=settings.V22_MULTI_ROUND_REVIEWS_ENABLED))
        except DeterministicJobError as error:
            if error.error_code == "V22_COMPARISON_NOT_CONFIGURED":
                raise HTTPException(503, detail={"code": "UNAVAILABLE"}, headers=HEADERS) from None
            raise


@router.post("/review-evidence", dependencies=[Depends(require_internal_auth), Depends(enabled)])
async def review_evidence(request: Request, service: ReviewWorkspace = Depends(workspace_service)):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"code": "INVALID"}, status_code=415, headers=HEADERS)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 48 * 1024:
            return JSONResponse({"code": "TOO_LARGE"}, status_code=413, headers=HEADERS)
        raw.extend(chunk)
    try:
        command = command_adapter.validate_json(canonical_json_bytes(decode(raw)))
    except (ValueError, TypeError, RecursionError):
        return JSONResponse({"code": "INVALID"}, status_code=400, headers=HEADERS)
    try:
        result = await service.run(command)
        return JSONResponse(result.model_dump(mode="json"), headers=HEADERS)
    except TransientJobError:
        return JSONResponse({"code": "UNAVAILABLE"}, status_code=503, headers=HEADERS)
    except DeterministicJobError as error:
        code = error.error_code.removeprefix("V22_REVIEW_")
        status = {"INVALID": 400, "UNAVAILABLE": 404, "CONFLICT": 409, "STALE": 409, "TOO_LARGE": 413}.get(code, 503)
        return JSONResponse({"code": code if status != 503 else "UNAVAILABLE"}, status_code=status, headers=HEADERS)
