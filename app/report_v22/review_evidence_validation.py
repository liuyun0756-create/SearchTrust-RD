"""Cross-source binding only; no I/O, identity discovery or live freshness claims."""
from urllib.parse import urlsplit
from app.report_v22.review_plan_basis import plan_business,plan_report_checksum
from app.jobs_v22.digest import verified_request_digest
from app.report_v22.capability import instant
from app.report_v22.ga4_session_reading import read_sessions
from app.report_v22.implementation_records import parse_implementation_record
from app.report_v22.metric_reading import metric_reading
from app.report_v22.page_implementation_checks import prepare_page_check
from app.report_v22.cross_source_findings import _domain


def source_references(bundle, selection):
    refs = set()
    if selection.action_id in {e.action_id for e in bundle.implementation.entries}:
        refs.add(f"implementation:{bundle.implementation.id}:{selection.action_id}")
    if selection.page:
        refs.add(f"page:{selection.page.current.id}")
        if selection.page.previous:
            refs.add(f"page:{selection.page.previous.id}")
    if selection.gsc:
        refs.add(f"gsc:{selection.gsc.id}")
    if selection.ga4:
        refs.add(f"ga4:{selection.ga4.id}")
    return sorted(refs)


def validate_evidence(b):
    report, delivery, record = b.report, b.delivery, b.implementation
    at = instant(b.evaluated_at)
    website = _domain(urlsplit(plan_business(report).site_url).hostname or "")
    if b.report_checksum != plan_report_checksum(report) or b.delivery_checksum != verified_request_digest(delivery.model_dump(mode="json")):
        raise ValueError("source checksum mismatch")
    parse_implementation_record(record.model_dump(mode="json"), report=report, user_id=b.user_id, case_id=b.case_id,
        delivery_revision_id=b.delivery_id, delivery_revision=b.delivery_revision, delivery_content=delivery)
    if instant(record.recorded_at) > at:
        raise ValueError("future implementation")
    if [s.action_id for s in b.selections] != [a.id for a in delivery.actions]:
        raise ValueError("exact approved action order required")
    identities = {}

    def register(kind, identifier, value):
        key, digest = (kind, str(identifier)), verified_request_digest(value)
        if key in identities and identities[key] != digest:
            raise ValueError("one source identity has conflicting contents")
        identities[key] = digest

    for s in b.selections:
        if s.page:
            for observation in (s.page.current, s.page.previous):
                if observation is None:
                    continue
                plan = observation.plan
                expected = prepare_page_check(implementation=record.model_dump(mode="json"), business=plan_business(report),
                    action_id=s.action_id, target_url=plan.target_url, conditions=plan.conditions,
                    report=report, user_id=b.user_id, case_id=b.case_id, delivery_revision_id=b.delivery_id,
                    delivery_revision=b.delivery_revision, delivery_content=delivery)
                if plan != expected or instant(observation.observed_at) > at:
                    raise ValueError("page evidence binding or time mismatch")
                register("page", observation.id, observation.model_dump(mode="json"))
        if s.gsc:
            g = s.gsc
            if (g.user_id, g.case_id, g.request.implementation, g.request.action_id) != (b.user_id, b.case_id, record, s.action_id):
                raise ValueError("GSC evidence binding mismatch")
            if g.request.scope.source != "gsc" or instant(g.request.evaluated_at) > at:
                raise ValueError("invalid GSC review source")
            metric_reading(g.result)
            register("gsc", g.id, g.model_dump(mode="json"))
            for snapshot in (g.request.before, g.request.after):
                if snapshot:
                    resource = snapshot.external_resource_id
                    if resource.startswith("sc-domain:"):
                        host = resource.removeprefix("sc-domain:")
                    else:
                        parsed = urlsplit(resource)
                        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                            raise ValueError("unsupported GSC property scope")
                        host = parsed.hostname or ""
                    if _domain(host) != website:
                        raise ValueError("GSC property does not match the approved business")
                    register("snapshot", snapshot.snapshot_id, snapshot.model_dump(mode="json"))
        if s.ga4:
            g = s.ga4
            if (g.user_id, g.case_id, g.record, g.selection.action_id) != (b.user_id, b.case_id, record, s.action_id):
                raise ValueError("GA4 evidence binding mismatch")
            if g.status != "succeeded" or instant(g.completed_at) > at:
                raise ValueError("GA4 review requires an already saved successful observation")
            if not g.plan.scope.hosts or any(_domain(host) != website for host in g.plan.scope.hosts):
                raise ValueError("GA4 scope includes another business host")
            read_sessions(g)
            register("ga4", g.id, g.model_dump(mode="json"))


def validate_decisions(b):
    original = {a.id for a in b.delivery.actions}
    implemented = {e.action_id for e in b.implementation.entries}
    refs = {s.action_id: set(source_references(b, s)) for s in b.selections}
    all_refs = set().union(*refs.values()) if refs else set()
    ids = [d.action_id for d in b.decisions]
    if len(ids) != len(set(ids)) or sum(d.disposition != "defer" for d in b.decisions) > 3:
        raise ValueError("duplicate or excessive next-stage decisions")
    for d in b.decisions:
        if d.evidence_checksum != b.evidence_checksum or instant(d.recorded_at) > instant(b.evaluated_at):
            raise ValueError("decision evidence or time mismatch")
        # Decisions refer to already available evidence, not future collections.
        selected = b.selections if d.disposition == "new" else [s for s in b.selections if s.action_id == d.action_id]
        times = [instant(b.report.generated_at), instant(b.implementation.recorded_at)]
        for s in selected:
            if s.page:
                times += [instant(o.observed_at) for o in (s.page.current, s.page.previous) if o]
            if s.gsc:
                times.append(instant(s.gsc.request.evaluated_at))
            if s.ga4:
                times.append(instant(s.ga4.completed_at))
        if instant(d.recorded_at) < max(times):
            raise ValueError("decision predates selected evidence")
        if (d.disposition == "new") == (d.action_id in original):
            raise ValueError("decision must preserve original identities")
        if not set(d.references) <= (all_refs if d.disposition == "new" else refs[d.action_id]):
            raise ValueError("decision reference outside selected action evidence")
        if d.disposition == "observe" and d.action_id not in implemented:
            raise ValueError("observation decision requires recorded implementation")
