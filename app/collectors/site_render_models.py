"""Separately frozen rendering observations; static snapshots are referenced, never replaced."""
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, HttpUrl, model_validator

from app.collectors.site_inventory_models import DeepPageSnapshot
from app.report_v22.models import StrictModel

RENDER_FAILURE = 'The additional page check could not be completed. Contact details remain unverified where the saved sources do not provide usable information.'


class RenderedDeepPage(DeepPageSnapshot):
    model_config = ConfigDict(str_strip_whitespace=False)


class RenderedPage(StrictModel):
    url: HttpUrl
    reason: Literal['contact_not_observed', 'limited_visible_text', 'static_not_checked']
    static_text_characters: int = Field(ge=0, le=5_000_000)
    deep_snapshot: RenderedDeepPage | None = None
    limitation: Literal['render_unavailable', 'render_budget_exhausted'] | None = None

    @model_validator(mode='after')
    def consistent(self):
        if (self.deep_snapshot is None) != (self.limitation is not None):
            raise ValueError('render outcome mismatch')
        if self.deep_snapshot is not None and self.deep_snapshot.url != self.url:
            raise ValueError('render URL mismatch')
        return self


class RenderedSiteSnapshot(StrictModel):
    schema_version: Literal['site_rendered_snapshot_v1'] = 'site_rendered_snapshot_v1'
    source_kind: Literal['rendered'] = 'rendered'
    root_url: HttpUrl
    static_payload_checksum: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    started_at: AwareDatetime
    completed_at: AwareDatetime
    pages: list[RenderedPage] = Field(min_length=1, max_length=2)
    limitations: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def consistent(self):
        if self.completed_at < self.started_at or len({p.url for p in self.pages}) != len(self.pages):
            raise ValueError('invalid rendered snapshot')
        for page in self.pages:
            if page.deep_snapshot and not self.started_at <= page.deep_snapshot.collected_at <= self.completed_at:
                raise ValueError('invalid rendered observation time')
        return self

RENDER_NOTE = "Based on a rendered page. This corroborates the stated contact detail only; it does not establish a complete business identity or a mismatch."
