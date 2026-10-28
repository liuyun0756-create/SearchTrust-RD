"""Read-only selection and explicit free snapshot saving, never paid review dispatch."""
from app.report_v22.review_plan_basis import plan_business
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.review_evidence_models import ReviewEvidenceCore
from app.report_v22.review_freeze_models import checked_selection, invalid
from app.report_v22.review_freeze_storage import decode
from app.report_v22.review_projection import build_review_projection
from app.report_v22.review_workspace_models import Directory, WorkspaceView, command_adapter

MAX_VIEW_BYTES = 512 * 1024


def unavailable():
    return DeterministicJobError("V22_REVIEW_UNAVAILABLE", "This review evidence is not available.")


def fingerprint(selection, resolved):
    core = resolved.bundle.model_dump(mode="json", include=set(ReviewEvidenceCore.model_fields) - {"evaluated_at"})
    return verified_request_digest(dict(selection=selection.model_dump(mode="json"), core_without_evaluated_at=core, case_updated_at=resolved.case_updated_at))


def view(bundle, selection, state, *, confirmation=None, saved=None):
    p = build_review_projection(bundle)
    value = WorkspaceView(state=state, case_id=bundle.case_id, report_id=bundle.report.id,
        report_version=bundle.report.version, site_url=plan_business(bundle.report).site_url,
        selection=selection, evaluated_at=bundle.evaluated_at, prior_diagnosis=p.prior_diagnosis,
        actions=[a.facts for a in p.client_actions], confirmation_fingerprint=confirmation,
        review_id=saved.id if saved else None, frozen_at=saved.frozen_at if saved else None)
    # Client facts exclude private implementation notes, raw payloads, leases and user identity.
    if len(value.model_dump_json().encode()) > MAX_VIEW_BYTES:
        raise DeterministicJobError("V22_REVIEW_TOO_LARGE", "The review exceeds the supported view size.")
    return value


class ReviewWorkspace:
    def __init__(self, repository):
        self.repository = repository

    async def basis(self, c):
        raw = await self.repository._rpc("get_v22_review_workspace_basis", dict(p_user_id=str(c.user_id),
            p_case_id=str(c.case_id), p_report_id=str(c.report_id), p_delivery_id=str(c.target.delivery_id),
            p_implementation_id=str(c.target.implementation_id), p_implementation_revision=c.target.implementation_revision))
        try:
            s = checked_selection(decode(raw))
        except (ValueError, TypeError):
            raise invalid() from None
        if (s.report.id, s.delivery.id, s.implementation.id, s.implementation.revision) != (
                c.report_id, c.target.delivery_id, c.target.implementation_id, c.target.implementation_revision):
            raise invalid()
        if any(any(getattr(a, k) is not None for k in ("page_current_id", "page_previous_id", "gsc_id", "ga4_id")) for a in s.actions):
            raise invalid()
        return s

    async def directory(self, c):
        t = c.target
        raw = await self.repository._rpc("list_v22_review_workspace", dict(p_user_id=str(c.user_id), p_case_id=str(c.case_id),
            p_report_id=str(c.report_id), p_kind=c.kind, p_delivery_id=str(t.delivery_id if t else c.delivery_id) if t or c.delivery_id else None,
            p_implementation_id=str(t.implementation_id) if t else None, p_implementation_revision=t.implementation_revision if t else None,
            p_action_id=c.action_id, p_cursor=c.cursor.model_dump(mode="json") if c.cursor else None))
        try:
            value = Directory.model_validate_json(canonical_json_bytes(decode(raw)))
            if (value.case_id, value.report_id, value.kind) != (c.case_id, c.report_id, c.kind):
                raise ValueError("directory identity")
            if (t or c.delivery_id) and any(x.delivery_id != (t.delivery_id if t else c.delivery_id) for x in value.items):
                raise ValueError("directory delivery")
            return value
        except (ValueError, TypeError):
            raise invalid() from None

    async def run(self, c):
        c = command_adapter.validate_json(c.model_dump_json())
        if c.operation in ("candidates", "history", "versions"):
            return await self.directory(c)
        identity = dict(user_id=c.user_id, case_id=c.case_id)
        if c.operation in ("read", "save"):
            saved = await self.repository.read(**identity, review_id=c.review_id)
            if saved:
                if saved.bundle.report.id != c.report_id:
                    raise unavailable()
                if c.operation == "save" and saved.selection != c.selection:
                    raise DeterministicJobError("V22_REVIEW_CONFLICT", "This request belongs to a different selection.")
                return view(saved.bundle, saved.selection, "saved", saved=saved)
            if c.operation == "read":
                raise unavailable()
        if c.operation in ("context", "source"):
            selection = await self.basis(c)
            if c.operation == "source":
                raw = selection.model_dump(mode="json")
                action = next((a for a in raw["actions"] if a["action_id"] == c.action_id), None)
                if action is None:
                    raise unavailable()
                action[{"page": "page_current_id", "gsc": "gsc_id", "ga4": "ga4_id"}[c.kind]] = str(c.source_id)
                selection = checked_selection(raw)
        else:
            selection = checked_selection(c.selection)
        # URL/report binding is checked before resolve or any write.
        if selection.report.id != c.report_id:
            raise unavailable()
        resolved = await self.repository.resolve(**identity, selection=selection)
        if c.operation == "save":
            if fingerprint(selection, resolved) != c.confirmation_fingerprint:
                raise DeterministicJobError("V22_REVIEW_STALE", "The review basis changed. Confirm again before saving.")
            # Reject an unrenderable snapshot BEFORE the durable write. Reserve room
            # for the saved ID/timestamp so an oversized response is never mistaken
            # for a rejected save after it actually committed.
            projected = view(resolved.bundle, selection, "preview", confirmation=c.confirmation_fingerprint)
            if len(projected.model_dump_json().encode()) > MAX_VIEW_BYTES - 1024:
                raise DeterministicJobError("V22_REVIEW_TOO_LARGE", "The review exceeds the supported view size.")
            saved = await self.repository.save(**identity, review_id=c.review_id, selection=selection, resolved=resolved)
            return view(saved.bundle, saved.selection, "saved", saved=saved)
        return view(resolved.bundle, selection, c.operation,
            confirmation=fingerprint(selection, resolved) if c.operation == "preview" else None)
