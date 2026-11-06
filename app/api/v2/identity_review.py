"""Authenticated saved-input identity review; provider use is explicit and off by default."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from app.api.v2.dependencies import require_internal_auth
from app.core.config import settings
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.preflight_v22.gbp_identity import build_identity_gbp_lookup
from app.preflight_v22.identity_review import IdentityReviewCommand, IdentityReviewRepository, IdentityReviewService

router = APIRouter(prefix='/api/v2', tags=['Identity review'])
HEADERS = {'Cache-Control':'private, no-store'}
async def enabled():
    if not settings.V22_CAPABILITY_EXECUTION_ENABLED:
        raise HTTPException(404, detail={'code':'NOT_FOUND'}, headers=HEADERS)

async def service():
    async with httpx.AsyncClient() as client:
        try:
            repo = IdentityReviewRepository(url=settings.V22_SUPABASE_URL,
                service_role_key=settings.V22_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(), http_client=client)
            yield IdentityReviewService(repo, lookup_factory=lambda: build_identity_gbp_lookup(settings))
        except DeterministicJobError:
            raise HTTPException(503, detail={'code':'UNAVAILABLE'}, headers=HEADERS) from None

@router.post('/identity-review', dependencies=[Depends(require_internal_auth), Depends(enabled)])
async def identity_review(request: Request, intake: IdentityReviewService = Depends(service)):
    def reply(v, status=200):
        return JSONResponse(jsonable_encoder(v), status_code=status, headers=HEADERS)
    if request.query_params or request.headers.get('content-type','').split(';')[0].strip() != 'application/json':
        return reply({'code':'INVALID'},400)
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>8192: return reply({'code':'TOO_LARGE'},413)
        raw.extend(chunk)
    try:
        command=IdentityReviewCommand.model_validate_json(bytes(raw))
    except ValueError:
        return reply({'code':'INVALID'},400)
    try:
        return reply(await intake.run(command))
    except TransientJobError:
        return reply({'code':'UNAVAILABLE'},503)
    except DeterministicJobError as exc:
        return reply({'code':'CONFLICT' if exc.error_code.endswith('REJECTED') else 'UNAVAILABLE'},
            409 if exc.error_code.endswith('REJECTED') else 503)
    except ValueError as exc:
        code=str(exc)
        allowed={'CONFLICT':409,'UNAVAILABLE':404,'BRANCH_NOT_VERIFIED':409,
            'SAVED_BRANCH_DETAILS_REQUIRED':409,'SAVED_BRANCH_ADDRESS_REQUIRED':409,'UNEXPLAINED_FACT_CHANGE':409}
        return reply({'code':code if code in allowed else 'INVALID'}, allowed.get(code,400))
