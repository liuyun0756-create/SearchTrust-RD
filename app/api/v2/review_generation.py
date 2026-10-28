"""Internal authenticated entry. Browser identity is supplied only by the BFF."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from app.api.v2.dependencies import require_internal_auth
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.review_intake import ReviewIntakeRepository, ReviewIntakeService, parse_command, MAX_COMMAND
from app.report_v22.review_generation_inputs import ReviewGenerationPreparer
from app.report_v22.review_decision_service import ReviewDecisionService
from app.report_v22.review_decision_storage import ReviewDecisionRepository
from app.report_v22.review_freeze_storage import ReviewFreezeRepository

router = APIRouter(prefix="/api/v2", tags=["Review generation"])
HEADERS = {"Cache-Control": "private, no-store"}

async def enabled():
    if not (settings.V22_REVIEW_GENERATION_ENABLED and settings.V22_REVIEW_DECISIONS_WORKSPACE_ENABLED
        and settings.V22_REVIEW_EVIDENCE_WORKSPACE_ENABLED and settings.V22_CAPABILITY_EXECUTION_ENABLED):
        raise HTTPException(404, detail={"code": "NOT_FOUND"}, headers=HEADERS)

async def service():
    async with httpx.AsyncClient() as client:
        try:
            kw = dict(url=settings.V22_SUPABASE_URL, service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client)
            yield ReviewIntakeService(ReviewIntakeRepository(**kw), ReviewGenerationPreparer(
                ReviewDecisionService(ReviewDecisionRepository(**kw), ReviewFreezeRepository(**kw,allow_multi_round=settings.V22_MULTI_ROUND_REVIEWS_ENABLED)),allow_multi_round=settings.V22_MULTI_ROUND_REVIEWS_ENABLED))
        except DeterministicJobError:
            raise HTTPException(503, detail={"code": "UNAVAILABLE"}, headers=HEADERS) from None

@router.post("/review-generation", dependencies=[Depends(require_internal_auth), Depends(enabled)])
async def review_generation(request: Request, intake: ReviewIntakeService = Depends(service)):
    if request.query_params or request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"code": "INVALID"}, status_code=400, headers=HEADERS)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>MAX_COMMAND:
            return JSONResponse({"code":"TOO_LARGE"},status_code=413,headers=HEADERS)
        raw.extend(chunk)
    try:
        result = await intake.run(parse_command(bytes(raw)))
        return JSONResponse(result.model_dump(mode="json") if result else None, headers=HEADERS)
    except TransientJobError:
        return JSONResponse({"code":"UNAVAILABLE"},status_code=503,headers=HEADERS)
    except DeterministicJobError as exc:
        code = exc.error_code.removeprefix("V22_REVIEW_START_")
        status = {"INVALID":400,"UNAVAILABLE":404,"CONFLICT":409,"STALE":409,"BUSY":409,"INSUFFICIENT_CREDITS":402}.get(code,503)
        return JSONResponse({"code":code if status!=503 else "UNAVAILABLE"},status_code=status,headers=HEADERS)
