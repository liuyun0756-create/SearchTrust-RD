"""Bounded page-type differences, based on actual sampled HTML counts."""
from datetime import timedelta

from app.report_v22.evidence_adapters.site_counts import ASSET_PAGE_TYPES
from app.report_v22.findings_common import decision
from app.report_v22.findings_models import RuleTarget
from app.report_v22.public_rule_catalog import ASSET

WINDOW = timedelta(hours=24)


def evaluate(view):
    for kind in ASSET_PAGE_TYPES:
        target = RuleTarget(kind="page_type", page_type=kind)
        if not view.available("site"):
            yield decision(view, ASSET, target, "not_checked", view.missing_reason("site"))
            continue
        client = view.sources["site"]
        target = target.model_copy(update={"snapshot_id": client.binding.snapshot_id})
        total, total_refs = view.count(client, "/payload")
        count, count_refs = view.count(client, "/payload", kind)
        client_refs = total_refs + count_refs
        if total == 0:
            yield decision(view, ASSET, target, "not_checked", "insufficient_sample", evidence=client_refs)
            continue
        if count > 0:
            yield decision(view, ASSET, target, "not_triggered", "condition_not_met", evidence=client_refs)
            continue
        if not view.available("competitor"):
            yield decision(view, ASSET, target, "not_checked", view.missing_reason("competitor"), evidence=client_refs)
            continue
        source = view.sources["competitor"]
        candidates = []
        for i, item in enumerate(source.payload.competitors):
            if item.site_inventory is None or item.site_status == "unavailable":
                continue
            prefix = f"/payload/competitors/{i}/site_inventory"
            eligible, eligible_refs = view.count(source, prefix)
            if eligible == 0:
                continue
            present, present_refs = view.count(source, prefix, kind)
            candidates.append((item.competitor.competitor_id, present, eligible_refs + present_refs, item.site_inventory.completed_at))
        times = [client.payload.completed_at] + [candidate[3] for candidate in candidates]
        comparators = [key for candidate in candidates for key in candidate[2]]
        target = target.model_copy(update={"competitor_ids": sorted(candidate[0] for candidate in candidates)})
        if max(times) - min(times) > WINDOW:
            yield decision(view, ASSET, target, "not_checked", "comparison_time_gap", evidence=client_refs, comparators=comparators)
            continue
        positive = [candidate for candidate in candidates if candidate[1] > 0]
        if len(positive) >= 2:
            target = target.model_copy(update={"competitor_ids": sorted(candidate[0] for candidate in positive)})
            refs = [key for candidate in positive for key in candidate[2]]
            yield decision(view, ASSET, target, "triggered", "condition_met", evidence=client_refs, comparators=refs,
                           statement=f"The checked client HTML sample contained zero pages classified as {kind}, while {len(positive)} confirmed competitor samples contained that page type.",
                           notes=["This is a sampled page-type coverage difference, not proof of a missing exact service or city asset.",
                                  "Page classifications are heuristic; client and competitor discovery/deep-analysis limits differ.",
                                  *(["At least one confirmed competitor lacked an eligible site sample."] if len(candidates) < 3 else [])])
        else:
            state = "not_triggered" if len(candidates) == 3 else "not_checked"
            yield decision(view, ASSET, target, state, "condition_not_met" if state == "not_triggered" else "insufficient_sample",
                           evidence=client_refs, comparators=comparators)
