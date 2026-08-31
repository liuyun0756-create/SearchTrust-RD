"""Compare only URL-identified domains in the same immutable SERP group."""
from collections import defaultdict

from app.report_v22.evidence_bindings import host
from app.report_v22.findings_common import decision
from app.report_v22.findings_models import RuleTarget
from app.report_v22.public_rule_catalog import DOMAIN, AHEAD

RESULT_TYPES = ("maps", "local_pack", "organic")


def market_target(view, query, kind, source=None):
    point = source.payload.target_point if source else view.context.target_market
    return RuleTarget(kind="market", query=query, result_type=kind,
                      snapshot_id=source.binding.snapshot_id if source else None,
                      latitude=point.latitude, longitude=point.longitude,
                      country_code=point.country_code, language=view.context.search_language,
                      device=view.context.search_device)


def evaluate(view):
    if not view.available("serp"):
        for query in view.context.queries:
            for kind in RESULT_TYPES:
                for rule in (DOMAIN, AHEAD):
                    yield decision(view, rule, market_target(view, query, kind), "not_checked", view.missing_reason("serp"))
        return
    source = view.sources["serp"]
    snapshot = source.binding.snapshot_id
    client = host(view.context.site_url)
    competitors = {host(c.website_url): c.competitor_id for c in view.context.competitors}
    for index, run in enumerate(source.payload.query_runs):
        for kind in RESULT_TYPES:
            target = market_target(view, run.query, kind, source)
            name = "maps_call" if kind == "maps" else "google_call"
            call = getattr(run, name)
            records = [(i, r) for i, r in enumerate(run.results) if r.result_type == kind]
            if call.status != "succeeded" or not records:
                for rule in (DOMAIN, AHEAD):
                    yield decision(view, rule, target, "not_checked", "source_ineligible" if call.status != "succeeded" else "insufficient_sample")
                continue
            prefix = f"/payload/query_runs/{index}"
            call_refs = view.refs(snapshot, f"{prefix}/{name}/status", required=True)
            domains = defaultdict(list)
            all_url_refs = []
            for i, record in records:
                path = f"{prefix}/results/{i}"
                if record.url is not None:
                    domains[host(record.url)].append((record, path))
                    all_url_refs.extend(view.refs(snapshot, f"{path}/url", required=True))
            if any(record.url is None for _, record in records):
                yield decision(view, DOMAIN, target, "not_checked", "identity_unresolved", evidence=call_refs)
            else:
                absent = client not in domains
                yield decision(view, DOMAIN, target, "triggered" if absent else "not_triggered",
                               "condition_met" if absent else "condition_not_met", evidence=call_refs + all_url_refs,
                               statement=f"The saved {kind} result sample for query {run.query!r} did not contain the client site domain {client!r}.",
                               notes=["This is domain matching within returned records, not proof of business absence, zero exposure or an absolute ranking."])

            positions = {}
            conflicting = set()
            for domain, rows in domains.items():
                bases = {record.rank_source for record, _ in rows}
                if len(bases) != 1:
                    conflicting.add(domain)
                    continue
                minimum = min(record.position for record, _ in rows)
                best = [(record, path) for record, path in rows if record.position == minimum]
                refs = [key for _, path in best for key in view.field_refs(snapshot, path, "position", "url", "rank_source", "result_type")]
                positions[domain] = (minimum, next(iter(bases)), refs)
            if client not in positions:
                yield decision(view, AHEAD, target, "not_checked", "rank_basis_mismatch" if client in conflicting else "identity_unresolved", evidence=call_refs)
                continue
            client_position, basis, client_refs = positions[client]
            comparable = {domain: position for domain, position in positions.items() if domain in competitors and position[1] == basis}
            leaders = {domain: position for domain, position in comparable.items() if position[0] < client_position}
            if len(leaders) >= 2:
                chosen = leaders
                state, reason = "triggered", "condition_met"
            elif len(comparable) == 3:
                chosen = comparable
                state, reason = "not_triggered", "condition_not_met"
            else:
                chosen = comparable
                state = "not_checked"
                reason = "rank_basis_mismatch" if any(d in conflicting or (d in positions and positions[d][1] != basis) for d in competitors) else "insufficient_sample"
            target = target.model_copy(update={"competitor_ids": sorted(competitors[d] for d in chosen)})
            refs = [key for position in chosen.values() for key in position[2]]
            notes = ["Only the same result type and rank_source are compared; no ranking cause or commercial outcome is inferred."]
            if len(comparable) < 3:
                notes.append("Not all three confirmed competitor domains had comparable positions.")
            yield decision(view, AHEAD, target, state, reason, evidence=call_refs + client_refs, comparators=refs,
                           statement=f"In the saved {kind} sample for query {run.query!r}, {len(leaders)} confirmed competitor domains had positions ahead of the client site's best observed position {client_position}.", notes=notes)
