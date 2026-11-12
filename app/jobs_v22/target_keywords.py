"""Confirmed word list bound to one paid discovery; no collection or inference."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from app.report_v22.models import StrictModel
from app.jobs_v22.digest import verified_request_digest


class TargetKeywordBinding(StrictModel):
    schema_version: Literal['v22_target_keyword_binding_v1'] = 'v22_target_keyword_binding_v1'
    queries: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(min_length=3, max_length=5)
    queries_digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    planning_revision: int = Field(ge=1, le=2147483647)

    @model_validator(mode='after')
    def confirmed_queries(self):
        if (any(q != q.strip() or not q.strip() or any(ord(c)<32 for c in q) for q in self.queries)
                or len({q.casefold() for q in self.queries}) != len(self.queries)
                or verified_request_digest(self.queries) != self.queries_digest):
            raise ValueError('TARGET_KEYWORDS_INVALID')
        return self
