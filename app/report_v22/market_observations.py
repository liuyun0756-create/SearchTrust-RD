"""Position facts only; existing absence/comparison rules remain independent."""
from app.report_v22.evidence_bindings import host
from app.report_v22.findings_common import decision
from app.report_v22.market_findings import market_target
from app.report_v22.public_rule_catalog import OBSERVED


def evaluate(view):
    if not view.available("serp"):
        return
    source = view.sources["serp"]
    snapshot = source.binding.snapshot_id
    client = host(view.context.site_url)
    for index, run in enumerate(source.payload.query_runs):
        for kind in ("maps", "organic"):
            name = "maps_call" if kind == "maps" else "google_call"
            if getattr(run, name).status != "succeeded":
                continue
            rows = [(i, r) for i, r in enumerate(run.results)
                    if r.result_type == kind and r.url is not None and host(r.url) == client]
            if not rows:
                continue
            prefix = f"/payload/query_runs/{index}"
            evidence = view.refs(snapshot, f"{prefix}/{name}/status", required=True)
            for i, _ in rows:
                for field in ("position", "url", "rank_source", "result_type"):
                    evidence += view.refs(snapshot, f"{prefix}/results/{i}/{field}", required=True)
            # Preserve every distinct position/source pair; do not compare or
            # choose a minimum across different ranking bases.
            pairs = sorted({(r.position, r.rank_source) for _, r in rows})
            positions = ", ".join(f"{position} (rank_source={basis})" for position, basis in pairs)
            noun = "position" if len(pairs) == 1 else "positions"
            yield decision(view, OBSERVED, market_target(view, run.query, kind, source),
                "triggered", "condition_met", evidence=evidence,
                statement=f"In the saved {kind} sample for query {run.query!r}, the client site domain {client!r} was observed at {noun} {positions}.",
                notes=["This records positions in this sample only, not an absolute ranking, cause or business outcome."])
