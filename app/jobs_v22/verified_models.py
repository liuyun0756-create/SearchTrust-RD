"""Strict reference-only contracts for durable Verified analysis requests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, Field, PrivateAttr, model_validator

from app.collectors.site_inventory_models import SiteInventorySnapshot
from app.collectors.serp_market_models import SerpMarketSnapshot
from app.jobs_v22.digest import request_digest, verified_request_digest
from app.report_v22.models import ReportV22, StrictModel

if TYPE_CHECKING:
    from app.api.v2.models import AnalyzeRequest
    from app.competitors_v22.models import CompetitorCollectionSnapshot, SharedMarketSnapshot
    from app.report_v22.first_party_findings_models import TrustedFirstPartySnapshot


class VerifiedTaskRequest(StrictModel):
    schema_version: Literal["v22_verified_task_request_v1"] = "v22_verified_task_request_v1"
    case_id: UUID
    parent_report_id: UUID
    gsc_snapshot_id: UUID
    ga4_snapshot_id: UUID
    public_gbp_snapshot_id: UUID
    input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_distinct_sources(self) -> "VerifiedTaskRequest":
        source_ids = (
            self.parent_report_id,
            self.gsc_snapshot_id,
            self.ga4_snapshot_id,
            self.public_gbp_snapshot_id,
        )
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Verified parent report and snapshot identities must be distinct")
        return self


class VerifiedRequestEnvelope(StrictModel):
    schema_version: Literal["v22_verified_request_envelope_v1"]
    verified_request: VerifiedTaskRequest


SnapshotPayload = TypeVar("SnapshotPayload")


class VerifiedSnapshotRow(StrictModel, Generic[SnapshotPayload]):
    snapshot_id: UUID
    case_id: UUID
    source_type: Literal["site", "serp", "competitor"]
    schema_version: str
    normalized_payload: SnapshotPayload
    payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    created_at: AwareDatetime
    fetched_at: AwareDatetime
    expires_at: AwareDatetime | None

    @model_validator(mode="after")
    def validate_source(self):
        expected = {"site": "site_inventory_snapshot_v1", "serp": "serp_market_snapshot_v1",
            "competitor": "competitor_collection_snapshot_v1"}[self.source_type]
        if self.schema_version != expected or self.normalized_payload.schema_version != expected:
            raise ValueError("snapshot schema mismatch")
        if request_digest(self.normalized_payload) != self.payload_checksum:
            raise ValueError("snapshot checksum mismatch")
        if self.fetched_at != self.normalized_payload.completed_at:
            raise ValueError("snapshot collection date mismatch")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("snapshot expiry must follow creation")
        if self.source_type == "serp" and self.expires_at is None:
            raise ValueError("shared market requires expiry")
        return self


class VerifiedResolvedInput(StrictModel):
    """Frozen RPC graph, with lazy linking to avoid API import cycles.

    API request models import VerifiedTaskRequest, so collection/first-party
    types must be linked after that API module finishes initialization.
    """

    schema_version: Literal["v22_verified_resolved_input_v1"]
    job_id: UUID
    case_id: UUID
    parent_report: ReportV22
    parent_payload_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    site_snapshot: VerifiedSnapshotRow[SiteInventorySnapshot]
    serp_snapshot: VerifiedSnapshotRow[SerpMarketSnapshot]
    competitor_snapshot: VerifiedSnapshotRow[CompetitorCollectionSnapshot]
    first_party_snapshots: list[TrustedFirstPartySnapshot] = Field(min_length=2, max_length=2)
    _parent_integrity_seal: str | None = PrivateAttr(default=None)

    def model_post_init(self, __context) -> None:
        # Separate from the frontend raw checksum: defaults and normalization have
        # now been applied. Private state is neither accepted nor emitted as JSON.
        if self._parent_integrity_seal is not None:
            self.validate_parent_integrity()
        elif hasattr(self, "parent_report"):
            self._parent_integrity_seal = request_digest(self.parent_report)

    def validate_parent_integrity(self) -> None:
        if self._parent_integrity_seal is None or request_digest(self.parent_report) != self._parent_integrity_seal:
            raise ValueError("validated parent report was mutated")

    @classmethod
    def model_rebuild(cls, **kwargs):
        from app.competitors_v22.models import CompetitorCollectionSnapshot
        from app.report_v22.first_party_findings_models import TrustedFirstPartySnapshot

        kwargs["_types_namespace"] = {
            **(kwargs.get("_types_namespace") or {}),
            "CompetitorCollectionSnapshot": CompetitorCollectionSnapshot,
            "TrustedFirstPartySnapshot": TrustedFirstPartySnapshot,
        }
        return super().model_rebuild(**kwargs)

    @model_validator(mode="after")
    def validate_graph(self):
        parent = self.parent_report
        if (parent.identity.case_id != self.case_id or parent.report_version.report_type != "prospect"
                or parent.report_version.parent_report_id is not None or parent.report_version.report_id == self.job_id):
            raise ValueError("parent report identity mismatch")
        rows = [self.site_snapshot, self.serp_snapshot, self.competitor_snapshot]
        if [row.source_type for row in rows] != ["site", "serp", "competitor"]:
            raise ValueError("public source mismatch")
        if any(row.case_id != self.case_id for row in rows + self.first_party_snapshots):
            raise ValueError("snapshot Case mismatch")
        all_ids = [row.snapshot_id for row in rows + self.first_party_snapshots]
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("snapshot identities must be unique")
        sources = {snapshot.source_type for snapshot in self.first_party_snapshots}
        if sources != {"gsc", "ga4"}:
            raise ValueError("exactly GSC and GA4 required")
        if len({snapshot.binding_id for snapshot in self.first_party_snapshots}) != 2:
            raise ValueError("distinct first-party bindings required")
        for snapshot in self.first_party_snapshots:
            if (snapshot.identity_match_status != "matched" or snapshot.health_status != "healthy"
                    or snapshot.raw_payload is not None
                    or request_digest(snapshot.normalized_payload) != snapshot.payload_checksum):
                raise ValueError("first-party snapshot integrity mismatch")
            from app.google_connections_v22.gsc import GscSnapshot
            from app.google_connections_v22.ga4 import Ga4Snapshot

            payload_type = GscSnapshot if snapshot.source_type == "gsc" else Ga4Snapshot
            if snapshot.source_type == "gsc":
                validate_exact_gsc_fields(snapshot.normalized_payload)
            payload = payload_type.model_validate_json(json.dumps(snapshot.normalized_payload), strict=True)
            if (set(snapshot.normalized_payload) - set(payload_type.model_fields)
                    or payload.schema_version != snapshot.schema_version
                    or payload.resource_id != snapshot.external_resource_id
                    or payload.previous.start_date != snapshot.coverage_start
                    or payload.current.end_date != snapshot.coverage_end):
                raise ValueError("first-party payload binding mismatch")
        coverage = {item.source_type: item for item in parent.data_coverage.sources}
        for row in rows:
            if row.source_type not in coverage or coverage[row.source_type].snapshot_ids != [row.snapshot_id]:
                raise ValueError("parent snapshot coverage mismatch")
            if any(e.snapshot_id != row.snapshot_id for e in parent.evidence_index if e.source_type == row.source_type):
                raise ValueError("parent evidence snapshot mismatch")
        competitor = self.competitor_snapshot.normalized_payload
        if (competitor.market_snapshot_id != self.serp_snapshot.snapshot_id
                or competitor.market_snapshot_checksum != self.serp_snapshot.payload_checksum
                or competitor.job_id != parent.report_version.report_id):
            raise ValueError("competitor market binding mismatch")
        analyze = self.analyze_request
        expected_competitors = {item.competitor_id: item for item in analyze.competitors}
        if {item.competitor.competitor_id for item in competitor.competitors} != set(expected_competitors):
            raise ValueError("competitor selection mismatch")
        for item in competitor.competitors:
            if item.competitor != expected_competitors[item.competitor.competitor_id]:
                raise ValueError("competitor identity mismatch")
        serp = self.serp_snapshot.normalized_payload
        target = analyze.target_market
        if (serp.queries != analyze.queries or serp.device != parent.case_context.search_device
                or serp.language != parent.case_context.search_language or serp.country_code != target.country_code
                or serp.target_point.requested_label != target.display_name
                or any(getattr(target, key) is not None and getattr(target, key) != getattr(serp.target_point, key)
                    for key in ("latitude", "longitude"))):
            raise ValueError("SERP context mismatch")
        if (str(self.site_snapshot.normalized_payload.root_url) != str(parent.identity.business.site_url)
                or self.site_snapshot.normalized_payload.canonical_host != parent.identity.business.normalized_domain
                or self.serp_snapshot.normalized_payload.queries != parent.case_context.queries):
            raise ValueError("public source context mismatch")
        public_id = self.public_gbp_snapshot_id
        if public_id in set(all_ids) | {self.job_id, parent.report_version.report_id}:
            raise ValueError("public GBP identity must be distinct")
        return self

    @property
    def public_gbp_snapshot_id(self) -> UUID:
        return validate_public_gbp(self.parent_report)

    def validate_request(self, *, job_id: UUID, request: VerifiedTaskRequest) -> None:
        if self.job_id != job_id or self.case_id != request.case_id:
            raise ValueError("request identity mismatch")
        sources = {snapshot.source_type: snapshot.snapshot_id for snapshot in self.first_party_snapshots}
        identity = dict(case_id=str(self.case_id), job_id=str(self.job_id),
            parent_report_id=str(self.parent_report.report_version.report_id),
            gsc_snapshot_id=str(sources["gsc"]), ga4_snapshot_id=str(sources["ga4"]),
            public_gbp_snapshot_id=str(self.public_gbp_snapshot_id))
        if (request.parent_report_id != self.parent_report.report_version.report_id
                or request.gsc_snapshot_id != sources["gsc"] or request.ga4_snapshot_id != sources["ga4"]
                or request.public_gbp_snapshot_id != self.public_gbp_snapshot_id
                or verified_request_digest(identity) != request.input_checksum):
            raise ValueError("Verified request binding mismatch")

    @property
    def analyze_request(self) -> AnalyzeRequest:
        from app.api.v2.models import AnalyzeRequest, ConfirmedCompetitor, GenerationLimits

        parent = self.parent_report
        competitors = [ConfirmedCompetitor(competitor_id=item.competitor_id,
            business_name=item.business_name, website_url=item.website_url, public_gbp_url=item.public_gbp_url,
            confirmation_source="user" if parent.competitor_analysis.selection_method == "system_ranked_user_confirmed" else "system")
            for item in parent.competitor_analysis.competitors]
        return AnalyzeRequest(case_id=self.case_id, report_type="prospect", business_identity=parent.identity.business,
            primary_service=parent.case_context.primary_service, target_market=parent.case_context.target_market,
            queries=parent.case_context.queries, competitors=competitors,
            generation_limits=GenerationLimits(competitor_count=len(competitors)))

    @property
    def shared_market(self) -> SharedMarketSnapshot:
        from app.competitors_v22.models import SharedMarketSnapshot
        from app.competitors_v22.selection import analysis_discovery_input_digest

        row = self.serp_snapshot
        return SharedMarketSnapshot(schema_version="competitor_shared_market_v1", snapshot_id=row.snapshot_id,
            source_job_id=row.normalized_payload.job_id, input_digest=analysis_discovery_input_digest(self.analyze_request),
            snapshot_checksum=row.payload_checksum, created_at=row.created_at, expires_at=row.expires_at,
            snapshot=row.normalized_payload)


def validate_public_gbp(report: ReportV22) -> UUID:
    url = report.identity.business.public_gbp_url
    coverage = next((item for item in report.data_coverage.sources if item.source_type == "gbp"), None)
    if url is None or coverage is None or coverage.health_status != "healthy" or coverage.identity_match_status != "matched":
        raise ValueError("healthy matched public GBP coverage required")
    ids = {item.snapshot_id for item in report.evidence_index if item.source_type == "gbp"
        and item.health_status == "healthy" and item.source_locator.url == url
        and item.snapshot_id in coverage.snapshot_ids}
    if len(ids) != 1 or set(coverage.snapshot_ids) != ids:
        raise ValueError("unique public GBP binding required")
    return next(iter(ids))


def validate_exact_gsc_fields(payload: dict) -> None:
    """GSC provider models ignore extras at some levels; reject them before parsing.

    Every structured GSC object is visited. GA4 and public source hierarchies
    already use extra=forbid, checked recursively by the boundary contract tests.
    """
    from app.google_connections_v22.gsc import GscSnapshot, Period, View, MetricRow

    def exact(value, model):
        if not isinstance(value, dict) or set(value) - set(model.model_fields):
            raise ValueError("unexpected GSC object fields")

    exact(payload, GscSnapshot)
    for period_name in ("current", "previous"):
        period = payload.get(period_name)
        exact(period, Period)
        for name, field in Period.model_fields.items():
            if field.annotation is not View:
                continue
            view = period.get(name)
            exact(view, View)
            if not isinstance(view.get("rows"), list):
                raise ValueError("invalid GSC rows")
            for row in view["rows"]:
                exact(row, MetricRow)
