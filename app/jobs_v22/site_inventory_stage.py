"""Checkpointed worker-stage adapter for v2.2 site inventory collection."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, ValidationError

from app.api.v2.models import AnalyzeRequest
from app.collectors.site_inventory import (
    FirecrawlMapper,
    SiteInventoryCollectionError,
    SiteInventoryCollector,
)
from app.collectors.site_inventory_fetcher import (
    BoundedSiteFetcher,
    SiteFetchResponse,
)
from app.collectors.site_inventory_firecrawl import (
    FirecrawlMapAdapter,
    FirecrawlMapResult,
)
from app.collectors.site_inventory_models import GscPagePriority, SiteInventorySnapshot
from app.collectors.site_inventory_selection import gsc_priorities_from_snapshots
from app.collectors.site_inventory_urls import (
    SiteScope,
    canonicalize_site_resource_url,
    stable_url_digest,
)
from app.core.config import Settings
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.models import StrictModel


_CHECKPOINT_VERSION = "site_inventory_v1"


class Fetcher(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        scope: SiteScope,
        max_bytes: int,
        accepted_media_types: set[str] | None = None,
    ) -> SiteFetchResponse: ...

    def apply_crawl_delay(self, seconds: float | None) -> None: ...


class SiteInventoryCheckpointError(DeterministicJobError):
    def __init__(self) -> None:
        super().__init__(
            "V22_SITE_INVENTORY_CHECKPOINT_INVALID",
            "A saved site inventory checkpoint could not be validated.",
        )


class _FetchCheckpoint(StrictModel):
    schema_version: str = Field(pattern=r"^site_inventory_fetch_v1$")
    requested_url: str = Field(min_length=1, max_length=4096)
    final_url: str = Field(min_length=1, max_length=4096)
    status_code: int = Field(ge=100, le=599)
    content_type: str = Field(max_length=200)
    body_base64: str = Field(max_length=7_000_000)
    body_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class _FirecrawlCheckpoint(StrictModel):
    schema_version: str = Field(pattern=r"^site_inventory_firecrawl_v1$")
    urls: list[str] = Field(default_factory=list, max_length=500)
    limitation: str | None = Field(default=None, max_length=100)


class _InventoryManifestCheckpoint(StrictModel):
    schema_version: str = Field(pattern=r"^site_inventory_manifest_v1$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    page_digests: list[str] = Field(min_length=1, max_length=500)
    discovered_url_count: int = Field(ge=1, le=500)
    structurally_checked_count: int = Field(ge=1, le=500)


class _InventorySelectionCheckpoint(StrictModel):
    schema_version: str = Field(pattern=r"^site_inventory_selection_v1$")
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    selected_page_digests: list[str] = Field(default_factory=list, max_length=50)
    deep_analyzed_count: int = Field(ge=0, le=50)


@dataclass(frozen=True)
class SiteInventoryStageConfig:
    structural_max_bytes: int
    deep_max_bytes: int
    sitemap_max_bytes: int
    sitemap_decompressed_max_bytes: int
    sitemap_max_files: int
    sitemap_max_depth: int
    batch_size: int


def _validate_model(model_type: Any, value: Any) -> Any:
    try:
        return model_type.model_validate_json(canonical_json_bytes(value))
    except (TypeError, ValidationError, ValueError) as exc:
        raise SiteInventoryCheckpointError() from exc


class CheckpointedSiteFetcher:
    def __init__(
        self,
        *,
        delegate: Fetcher,
        checkpoints: JobCheckpoints,
        job_id: UUID,
    ) -> None:
        self.delegate = delegate
        self.checkpoints = checkpoints
        self.job_id = job_id

    def checkpoint_key(
        self,
        url: str,
        *,
        max_bytes: int,
        accepted_media_types: set[str] | None,
    ) -> str:
        media_digest = request_digest(sorted(accepted_media_types or []))[7:23]
        return (
            f"{_CHECKPOINT_VERSION}:fetch:{stable_url_digest(url)[7:]}:"
            f"{max_bytes}:{media_digest}"
        )

    async def fetch(
        self,
        url: str,
        *,
        scope: SiteScope,
        max_bytes: int,
        accepted_media_types: set[str] | None = None,
    ) -> SiteFetchResponse:
        key = self.checkpoint_key(
            url,
            max_bytes=max_bytes,
            accepted_media_types=accepted_media_types,
        )

        async def operation() -> dict[str, Any]:
            response = await self.delegate.fetch(
                url,
                scope=scope,
                max_bytes=max_bytes,
                accepted_media_types=accepted_media_types,
            )
            checkpoint = _FetchCheckpoint(
                schema_version="site_inventory_fetch_v1",
                requested_url=response.requested_url,
                final_url=response.final_url,
                status_code=response.status_code,
                content_type=response.content_type[:200],
                body_base64=base64.b64encode(response.body).decode("ascii"),
                body_checksum=f"sha256:{hashlib.sha256(response.body).hexdigest()}",
            )
            return checkpoint.model_dump(mode="json")

        raw = await self.checkpoints.run_once(self.job_id, key, operation)
        checkpoint = _validate_model(_FetchCheckpoint, raw)
        if checkpoint.requested_url != url:
            raise SiteInventoryCheckpointError()
        try:
            body = base64.b64decode(checkpoint.body_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SiteInventoryCheckpointError() from exc
        checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
        if checksum != checkpoint.body_checksum or len(body) > max_bytes:
            raise SiteInventoryCheckpointError()
        parsed_final = urlsplit(checkpoint.final_url)
        if (
            parsed_final.scheme not in {"http", "https"}
            or not parsed_final.hostname
            or not scope.contains_host(parsed_final.hostname)
            or canonicalize_site_resource_url(checkpoint.final_url, scope=scope) is None
        ):
            raise SiteInventoryCheckpointError()
        media_type = checkpoint.content_type.split(";", 1)[0].strip().casefold()
        if accepted_media_types is not None and media_type not in accepted_media_types:
            raise SiteInventoryCheckpointError()
        return SiteFetchResponse(
            requested_url=checkpoint.requested_url,
            final_url=checkpoint.final_url,
            status_code=checkpoint.status_code,
            content_type=checkpoint.content_type,
            body=body,
        )

    def apply_crawl_delay(self, seconds: float | None) -> None:
        self.delegate.apply_crawl_delay(seconds)


class _CheckpointedFirecrawl:
    def __init__(
        self,
        *,
        delegate: FirecrawlMapper,
        checkpoints: JobCheckpoints,
        job_id: UUID,
    ) -> None:
        self.delegate = delegate
        self.checkpoints = checkpoints
        self.job_id = job_id

    async def map(self, root_url: str, *, limit: int) -> FirecrawlMapResult:
        key = (
            f"{_CHECKPOINT_VERSION}:firecrawl-map:"
            f"{stable_url_digest(root_url)[7:]}:{limit}"
        )

        async def operation() -> dict[str, Any]:
            result = await self.delegate.map(root_url, limit=limit)
            return _FirecrawlCheckpoint(
                schema_version="site_inventory_firecrawl_v1",
                urls=list(result.urls),
                limitation=result.limitation,
            ).model_dump(mode="json")

        raw = await self.checkpoints.run_once(self.job_id, key, operation)
        checkpoint = _validate_model(_FirecrawlCheckpoint, raw)
        if len(checkpoint.urls) > limit:
            raise SiteInventoryCheckpointError()
        return FirecrawlMapResult(tuple(checkpoint.urls), checkpoint.limitation)


class CheckpointedSiteInventoryStage:
    def __init__(
        self,
        *,
        fetcher: Fetcher,
        firecrawl: FirecrawlMapper | None,
        config: SiteInventoryStageConfig,
        clock: Any | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.firecrawl = firecrawl
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(
        self,
        *,
        job_id: UUID,
        request: AnalyzeRequest,
        checkpoints: JobCheckpoints,
    ) -> SiteInventorySnapshot:
        identity = {
            "site_url": str(request.business_identity.site_url),
            "primary_service": request.primary_service,
            "target_market": request.target_market.model_dump(mode="json"),
            "max_site_urls": request.generation_limits.max_site_urls,
            "max_deep_pages": request.generation_limits.max_deep_pages,
            "gsc_snapshots": [
                snapshot.payload_checksum
                for snapshot in request.first_party_snapshots
                if snapshot.source_type == "gsc"
            ],
        }
        input_digest = request_digest(identity)
        final_key = f"{_CHECKPOINT_VERSION}:snapshot:{input_digest[7:]}"

        async def operation() -> dict[str, Any]:
            checkpointed_fetcher = CheckpointedSiteFetcher(
                delegate=self.fetcher,
                checkpoints=checkpoints,
                job_id=job_id,
            )
            checkpointed_firecrawl = (
                _CheckpointedFirecrawl(
                    delegate=self.firecrawl,
                    checkpoints=checkpoints,
                    job_id=job_id,
                )
                if self.firecrawl is not None
                else None
            )
            collector = SiteInventoryCollector(
                fetcher=checkpointed_fetcher,
                firecrawl=checkpointed_firecrawl,
                structural_max_bytes=self.config.structural_max_bytes,
                deep_max_bytes=self.config.deep_max_bytes,
                sitemap_max_bytes=self.config.sitemap_max_bytes,
                sitemap_decompressed_max_bytes=self.config.sitemap_decompressed_max_bytes,
                sitemap_max_files=self.config.sitemap_max_files,
                sitemap_max_depth=self.config.sitemap_max_depth,
                batch_size=self.config.batch_size,
                clock=self.clock,
            )
            scope = SiteScope.from_root(str(request.business_identity.site_url))
            gsc_priorities = gsc_priorities_from_snapshots(
                request.first_party_snapshots,
                scope=scope,
                now=self.clock(),
            )
            market_text = " ".join(
                value
                for value in (
                    request.target_market.display_name,
                    request.target_market.city,
                    request.target_market.region,
                    request.target_market.country_code,
                )
                if value
            )
            try:
                snapshot = await collector.collect(
                    site_url=str(request.business_identity.site_url),
                    discovery_limit=request.generation_limits.max_site_urls,
                    deep_analysis_limit=request.generation_limits.max_deep_pages,
                    primary_service=request.primary_service,
                    target_market=market_text,
                    gsc_priorities=gsc_priorities,
                )
            except SiteInventoryCollectionError as exc:
                raise DeterministicJobError(
                    f"V22_SITE_INVENTORY_{exc.code.upper()}",
                    exc.user_message,
                ) from exc

            manifest = _InventoryManifestCheckpoint(
                schema_version="site_inventory_manifest_v1",
                request_digest=input_digest,
                page_digests=[stable_url_digest(str(page.url)) for page in snapshot.pages],
                discovered_url_count=snapshot.discovered_url_count,
                structurally_checked_count=snapshot.structurally_checked_count,
            )
            selection = _InventorySelectionCheckpoint(
                schema_version="site_inventory_selection_v1",
                request_digest=input_digest,
                selected_page_digests=[
                    stable_url_digest(str(page.url)) for page in snapshot.selected_pages
                ],
                deep_analyzed_count=snapshot.deep_analyzed_count,
            )
            manifest_raw = await checkpoints.run_once(
                job_id,
                f"{_CHECKPOINT_VERSION}:manifest:{input_digest[7:]}",
                lambda: _return_value(manifest.model_dump(mode="json")),
            )
            selection_raw = await checkpoints.run_once(
                job_id,
                f"{_CHECKPOINT_VERSION}:selection:{input_digest[7:]}",
                lambda: _return_value(selection.model_dump(mode="json")),
            )
            persisted_manifest = _validate_model(_InventoryManifestCheckpoint, manifest_raw)
            persisted_selection = _validate_model(_InventorySelectionCheckpoint, selection_raw)
            if persisted_manifest != manifest or persisted_selection != selection:
                raise SiteInventoryCheckpointError()
            return snapshot.model_dump(mode="json")

        raw = await checkpoints.run_once(job_id, final_key, operation)
        return _validate_model(SiteInventorySnapshot, raw)

    async def collect_inventory_for_site(
        self,
        *,
        job_id: UUID,
        site_url: str,
        primary_service: str,
        target_market: str,
        discovery_limit: int,
        deep_analysis_limit: int,
        gsc_priorities: list[GscPagePriority],
        checkpoints: JobCheckpoints,
        checkpoint_namespace: str,
    ) -> SiteInventorySnapshot:
        """Collect one bounded site with explicit limits and first-party priorities."""

        identity = {
            "site_url": site_url,
            "primary_service": primary_service,
            "target_market": target_market,
            "discovery_limit": discovery_limit,
            "deep_analysis_limit": deep_analysis_limit,
            "gsc_priorities": [item.model_dump(mode="json") for item in gsc_priorities],
            "checkpoint_namespace": checkpoint_namespace,
        }
        input_digest = request_digest(identity)
        final_key = f"{_CHECKPOINT_VERSION}:{checkpoint_namespace}:snapshot:{input_digest[7:]}"

        async def operation() -> dict[str, Any]:
            checkpointed_fetcher = CheckpointedSiteFetcher(
                delegate=self.fetcher,
                checkpoints=checkpoints,
                job_id=job_id,
            )
            checkpointed_firecrawl = (
                _CheckpointedFirecrawl(
                    delegate=self.firecrawl,
                    checkpoints=checkpoints,
                    job_id=job_id,
                )
                if self.firecrawl is not None
                else None
            )
            collector = SiteInventoryCollector(
                fetcher=checkpointed_fetcher,
                firecrawl=checkpointed_firecrawl,
                structural_max_bytes=self.config.structural_max_bytes,
                deep_max_bytes=self.config.deep_max_bytes,
                sitemap_max_bytes=self.config.sitemap_max_bytes,
                sitemap_decompressed_max_bytes=self.config.sitemap_decompressed_max_bytes,
                sitemap_max_files=self.config.sitemap_max_files,
                sitemap_max_depth=self.config.sitemap_max_depth,
                batch_size=self.config.batch_size,
                clock=self.clock,
            )
            try:
                snapshot = await collector.collect(
                    site_url=site_url,
                    discovery_limit=discovery_limit,
                    deep_analysis_limit=deep_analysis_limit,
                    primary_service=primary_service,
                    target_market=target_market,
                    gsc_priorities=gsc_priorities,
                )
            except SiteInventoryCollectionError as exc:
                raise DeterministicJobError(
                    f"V22_SITE_INVENTORY_{exc.code.upper()}",
                    exc.user_message,
                ) from exc
            return snapshot.model_dump(mode="json")

        raw = await checkpoints.run_once(job_id, final_key, operation)
        return _validate_model(SiteInventorySnapshot, raw)


async def _return_value(value: dict[str, Any]) -> dict[str, Any]:
    return value


def build_site_inventory_stage(settings: Settings) -> CheckpointedSiteInventoryStage:
    fetcher = BoundedSiteFetcher(
        connect_timeout=settings.V22_SITE_INVENTORY_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.V22_SITE_INVENTORY_READ_TIMEOUT_SECONDS,
        total_timeout=settings.V22_SITE_INVENTORY_TOTAL_TIMEOUT_SECONDS,
        max_redirects=settings.V22_SITE_INVENTORY_MAX_REDIRECTS,
        concurrency=settings.V22_SITE_INVENTORY_CONCURRENCY,
        requests_per_second=settings.V22_SITE_INVENTORY_REQUESTS_PER_SECOND,
    )
    firecrawl = FirecrawlMapAdapter(
        api_key=settings.FIRECRAWL_API_KEY,
        api_url=settings.FIRECRAWL_API_URL,
        enabled=settings.V22_SITE_INVENTORY_FIRECRAWL_ENABLED,
        timeout_seconds=settings.V22_SITE_INVENTORY_READ_TIMEOUT_SECONDS,
    )
    return CheckpointedSiteInventoryStage(
        fetcher=fetcher,
        firecrawl=firecrawl,
        config=SiteInventoryStageConfig(
            structural_max_bytes=settings.V22_SITE_INVENTORY_STRUCTURAL_BYTES,
            deep_max_bytes=settings.V22_SITE_INVENTORY_DEEP_BYTES,
            sitemap_max_bytes=settings.V22_SITE_INVENTORY_SITEMAP_BYTES,
            sitemap_decompressed_max_bytes=(
                settings.V22_SITE_INVENTORY_SITEMAP_DECOMPRESSED_BYTES
            ),
            sitemap_max_files=settings.V22_SITE_INVENTORY_SITEMAP_FILES,
            sitemap_max_depth=settings.V22_SITE_INVENTORY_SITEMAP_INDEX_DEPTH,
            batch_size=settings.V22_SITE_INVENTORY_BATCH_SIZE,
        ),
    )
