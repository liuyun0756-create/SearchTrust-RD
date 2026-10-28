"""Internal-auth owner entry. Static page checks are independent of Google flags."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.v2.dependencies import require_internal_auth
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.report_v22.page_check_entry import PageCheckCommand, PageCheckEntry
from app.report_v22.page_check_limits import PageCheckRateLimited
from app.report_v22.page_check_storage import PageCheckRepository

router = APIRouter(prefix="/api/v2", tags=["Page checks"])
HEADERS = {"Cache-Control": "private, no-store"}


async def enabled():
    if not settings.V22_PAGE_CHECKS_ENABLED or not settings.V22_CAPABILITY_EXECUTION_ENABLED:
        raise HTTPException(404, detail={"code": "NOT_FOUND"}, headers=HEADERS)


async def page_check_service():
    async with httpx.AsyncClient() as client:
        try:
            yield PageCheckEntry(PageCheckRepository(url=settings.V22_SUPABASE_URL,
                service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client))
        except DeterministicJobError as error:
            if error.error_code == "V22_COMPARISON_NOT_CONFIGURED":
                raise HTTPException(503, detail={"code": "UNAVAILABLE"}, headers=HEADERS) from None
            raise


@router.post("/page-checks", dependencies=[Depends(require_internal_auth), Depends(enabled)])
async def page_checks(request: Request, service: PageCheckEntry = Depends(page_check_service)):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, detail={"code": "INVALID"}, headers=HEADERS)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 24576:
            raise HTTPException(413, detail={"code": "TOO_LARGE"}, headers=HEADERS)
        raw.extend(chunk)
    try:
        command = PageCheckCommand.model_validate_json(bytes(raw))
    except (ValueError, TypeError):
        raise HTTPException(400, detail={"code": "INVALID"}, headers=HEADERS) from None
    try:
        value = await service.run(command)
        return JSONResponse(value.model_dump(mode="json"), headers=HEADERS)
    except PageCheckRateLimited as error:
        return JSONResponse({"code": "PAGE_CHECK_RATE_LIMIT", "limits": error.limits.model_dump(mode="json")}, status_code=429, headers=HEADERS)
    except TransientJobError:
        raise HTTPException(503, detail={"code": "UNAVAILABLE"}, headers=HEADERS) from None
    except DeterministicJobError as error:
        status = 404 if error.error_code == "V22_PAGE_CHECK_NOT_FOUND" else 409 if error.error_code in (
            "V22_PAGE_CHECK_CONFLICT", "V22_PAGE_CHECK_REJECTED") else 503
        raise HTTPException(status, detail={"code": "NOT_FOUND" if status == 404 else "CONFLICT" if status == 409 else "UNAVAILABLE"}, headers=HEADERS) from None
