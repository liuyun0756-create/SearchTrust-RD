"""Default-off owner decision editing. Internal credentials only."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.v2.dependencies import require_internal_auth
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.review_freeze_storage import ReviewFreezeRepository
from app.report_v22.review_decision_storage import ReviewDecisionRepository
from app.report_v22.review_decision_service import ReviewDecisionService
from app.report_v22.review_decision_workspace import DecisionWorkspace, parse_command, MAX_COMMAND

router = APIRouter(prefix="/api/v2", tags=["Review decisions"])
HEADERS = {"Cache-Control": "private, no-store"}


async def enabled():
    if not (settings.V22_REVIEW_DECISIONS_WORKSPACE_ENABLED and settings.V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED and settings.V22_CAPABILITY_EXECUTION_ENABLED):
        raise HTTPException(404, detail={"code": "NOT_FOUND"}, headers=HEADERS)


async def decision_workspace():
    async with httpx.AsyncClient() as client:
        try:
            kw = dict(url=settings.V22_SUPABASE_URL, service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client)
            yield DecisionWorkspace(ReviewDecisionService(ReviewDecisionRepository(**kw), ReviewFreezeRepository(**kw,allow_multi_round=settings.V22_MULTI_ROUND_REVIEWS_ENABLED)))
        except DeterministicJobError:
            raise HTTPException(503, detail={"code": "UNAVAILABLE"}, headers=HEADERS) from None


@router.post("/review-decisions", dependencies=[Depends(require_internal_auth), Depends(enabled)])
async def review_decisions(request: Request, service: DecisionWorkspace = Depends(decision_workspace)):
    if request.query_params or request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"code": "INVALID"}, status_code=400, headers=HEADERS)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_COMMAND:
            return JSONResponse({"code": "TOO_LARGE"}, status_code=413, headers=HEADERS)
        raw.extend(chunk)
    try:
        c = parse_command(bytes(raw))
        result = await service.run(c)
        return JSONResponse(result.model_dump(mode="json"), headers=HEADERS)
    except TransientJobError:
        return JSONResponse({"code": "UNAVAILABLE"}, status_code=503, headers=HEADERS)
    except DeterministicJobError as e:
        code = e.error_code.removeprefix("V22_REVIEW_DECISION_")
        status = {"INVALID":400,"UNAVAILABLE":404,"CONFLICT":409,"STALE":409,"TOO_LARGE":413}.get(code,503)
        return JSONResponse({"code":code if status != 503 else "UNAVAILABLE"}, status_code=status, headers=HEADERS)
