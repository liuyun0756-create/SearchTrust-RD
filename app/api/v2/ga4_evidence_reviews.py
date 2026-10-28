"""Read-only provider audit + owner-recorded checks, gated with GA4 collections."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.v2.dependencies import require_internal_auth
from app.api.v2.ga4_collections import enabled
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.ga4_collection_storage import Ga4CollectionRepository
from app.report_v22.ga4_evidence_review import Ga4EvidenceReview, Ga4ReviewCommand

router = APIRouter(prefix="/api/v2", tags=["GA4 evidence review"])


async def review_service():
    async with httpx.AsyncClient() as client:
        try:
            yield Ga4EvidenceReview(Ga4CollectionRepository(url=settings.V22_SUPABASE_URL,
                service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client))
        except DeterministicJobError as error:
            if error.error_code == "V22_COMPARISON_NOT_CONFIGURED":
                raise HTTPException(503, detail={"code": "UNAVAILABLE"}) from None
            raise


@router.post("/ga4-evidence-reviews", dependencies=[Depends(require_internal_auth), Depends(enabled)])
async def reviews(request: Request, service: Ga4EvidenceReview = Depends(review_service)):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, detail={"code": "INVALID"})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20000:
            raise HTTPException(413, detail={"code": "TOO_LARGE"})
    try:
        command = Ga4ReviewCommand.model_validate_json(bytes(raw))
    except (ValueError, TypeError):
        raise HTTPException(400, detail={"code": "INVALID"}) from None
    try:
        result = await service.run(command)
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "private, no-store"})
    except TransientJobError:
        raise HTTPException(503, detail={"code": "UNAVAILABLE"}) from None
    except DeterministicJobError as error:
        status = 404 if error.error_code == "V22_GA4_REVIEW_NOT_FOUND" else 409 if error.error_code in (
            "V22_GA4_REVIEW_CONFLICT", "V22_COMPARISON_REJECTED") else 503
        raise HTTPException(status, detail={"code": "NOT_FOUND" if status == 404 else "CONFLICT" if status == 409 else "UNAVAILABLE"}) from None
