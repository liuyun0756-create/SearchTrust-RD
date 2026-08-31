"""Bound, read-only lookup over the evidence result and its validated sources."""
from collections import defaultdict
from uuid import UUID

from app.report_v22.evidence import resolve_origin
from app.report_v22.evidence_adapters.site_counts import COUNT_VERSION
from app.report_v22.evidence_models import EvidenceBuildInput, EvidenceBuildResult
from app.report_v22.findings_errors import FindingsError


class EvidenceView:
    def __init__(self, request: EvidenceBuildInput, evidence: EvidenceBuildResult):
        self.context = request.context
        self.evidence = evidence
        self.sources = {source.kind: source for source in request.sources}
        self.items = {item.evidence_id: item for item in evidence.evidence_index}
        self.traces = {trace.evidence_id: trace for trace in evidence.source_traces}
        self.summaries = {item.snapshot_id: item for item in evidence.source_summaries}
        self._paths = defaultdict(set)
        self._descendants = defaultdict(set)
        snapshots = {source.binding.snapshot_id: source.model_dump(mode="json") for source in request.sources}
        if (len(self.items) != len(evidence.evidence_index)
                or len(self.traces) != len(evidence.source_traces)
                or set(self.items) != set(self.traces)
                or set(snapshots) != set(self.summaries)):
            raise FindingsError("REFERENCE_INVALID")
        for identifier, trace in self.traces.items():
            item = self.items[identifier]
            if item.snapshot_id != trace.snapshot_id or trace.snapshot_id not in snapshots:
                raise FindingsError("REFERENCE_INVALID")
            for path in trace.origin_paths:
                resolve_origin(snapshots[trace.snapshot_id], path)
                self._paths[(trace.snapshot_id, path)].add(identifier)
                if item.source_type != "coverage":
                    parts = path.split("/")
                    for length in range(2, len(parts)):
                        self._descendants[(trace.snapshot_id, "/".join(parts[:length]))].add(identifier)

    def available(self, kind: str) -> bool:
        source = self.sources.get(kind)
        return source is not None and self.summaries[source.binding.snapshot_id].business_eligible

    def missing_reason(self, kind: str):
        return "source_ineligible" if kind in self.sources else "source_missing"

    def refs(self, snapshot_id: UUID, *paths: str, required: bool = False) -> list[str]:
        identifiers = set()
        for path in paths:
            identifiers.update(self._paths.get((snapshot_id, path), ()))
        identifiers = {key for key in identifiers if self.items[key].source_type != "coverage"}
        if required and not identifiers:
            raise FindingsError("REFERENCE_INVALID")
        return sorted(identifiers)

    def field_refs(self, snapshot_id: UUID, prefix: str, *fields: str) -> list[str]:
        identifiers = set()
        for field in fields:
            path = f"{prefix}/{field}"
            identifiers.update(self.refs(snapshot_id, path))
            # Repeated scalar fields (e.g. meta_robots) have element pointers.
            identifiers.update(self._descendants.get((snapshot_id, path), ()))
        return sorted(identifiers)

    def count(self, source, prefix: str, page_type: str | None = None):
        field = "eligible_html_page_count" if page_type is None else "eligible_page_type_count"
        keys = self.refs(source.binding.snapshot_id, f"{prefix}/pages")
        matches = [key for key in keys if self.traces[key].selector.field == field
                   and self.traces[key].selector.record_context[2:] == [page_type, COUNT_VERSION]]
        if len(matches) != 1 or type(self.items[matches[0]].normalized_value) is not int:
            raise FindingsError("REFERENCE_INVALID")
        return self.items[matches[0]].normalized_value, matches

    def notes(self, identifiers) -> list[str]:
        notes = set()
        for identifier in identifiers:
            item = self.items.get(identifier)
            if item is None:
                raise FindingsError("REFERENCE_INVALID")
            notes.update(item.limitations)
            notes.update(self.summaries[item.snapshot_id].limitations)
        return sorted(notes)
