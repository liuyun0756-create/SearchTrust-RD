"""Versioned arithmetic reading aid, not a calibrated effect qualification.

Pure derivative of frozen descriptive values. Never rewrites the saved result,
calls a provider or labels a large count sufficient for causal interpretation.
"""
from typing import Literal

from pydantic import Field, FiniteFloat

from app.report_v22.capability import Checksum, Label
from app.report_v22.comparison import MAX_SAFE, _metrics
from app.report_v22.comparison_base import StrictModel
from app.report_v22.comparison_models import ComparisonResult

RULESET = "v22_metric_resolution_v1"
Note = Literal["ZERO_BASELINE", "ONE_COUNT_DIFFERENCE", "UNCHANGED_COUNT", "ZERO_IMPRESSIONS", "UNCHANGED_CTR"]


class MetricReading(StrictModel):
    metric: Literal["clicks", "impressions", "ctr"]
    before_count: int = Field(ge=0, le=MAX_SAFE)
    after_count: int = Field(ge=0, le=MAX_SAFE)
    before_denominator: int | None
    after_denominator: int | None
    relative_step_pp: FiniteFloat | None
    before_rate_step_pp: FiniteFloat | None
    after_rate_step_pp: FiniteFloat | None
    notes: list[Note]


class MetricReadingGuide(StrictModel):
    schema_version: Literal["v22_metric_reading_v1"]
    ruleset_version: Literal["v22_metric_resolution_v1"]
    comparison_checksum: Checksum
    state: Literal["unavailable", "descriptive_only"]
    blocked_reasons: list[Label]
    metrics: list[MetricReading] = Field(max_length=3)
    inference_status: Literal["not_calibrated"]
    calibrated_threshold: None
    effect_conclusion: None
    causal_attribution: Literal[False]


def metric_reading(result: ComparisonResult) -> MetricReadingGuide:
    result = ComparisonResult.model_validate_json(result.model_dump_json())
    readings = []
    if result.state == "comparable":
        counts = [result.metrics[i].model_dump() for i in (0, 1)]
        values = [v[key] for v in counts for key in ("before", "after")]
        if any(v is None or not float(v).is_integer() or not 0 <= v <= MAX_SAFE for v in values):
            raise ValueError("invalid metric counts")
        cb, ca, ib, ia = map(int, values)
        if cb > ib or ca > ia:
            raise ValueError("clicks exceed impressions")
        if [m.model_dump() for m in result.metrics] != _metrics((cb, ib), (ca, ia)):
            raise ValueError("inconsistent metric arithmetic")
        for name, before, after in (("clicks", cb, ca), ("impressions", ib, ia)):
            notes = []
            if before == 0: notes.append("ZERO_BASELINE")
            if before == after: notes.append("UNCHANGED_COUNT")
            elif abs(after-before) == 1: notes.append("ONE_COUNT_DIFFERENCE")
            readings.append(MetricReading(metric=name,before_count=before,after_count=after,
                before_denominator=None,after_denominator=None,relative_step_pp=100/before if before else None,
                before_rate_step_pp=None,after_rate_step_pp=None,notes=notes))
        # Integer cross-products avoid treating tiny, rounded-away changes as equality.
        notes = ["ZERO_IMPRESSIONS"] if not ib or not ia else ["UNCHANGED_CTR"] if cb*ia == ca*ib else []
        readings.append(MetricReading(metric="ctr",before_count=cb,after_count=ca,
            before_denominator=ib,after_denominator=ia,relative_step_pp=None,
            before_rate_step_pp=100/ib if ib else None,after_rate_step_pp=100/ia if ia else None,notes=notes))
    return MetricReadingGuide(schema_version="v22_metric_reading_v1",ruleset_version=RULESET,
        comparison_checksum=result.checksum,state="descriptive_only" if readings else "unavailable",
        blocked_reasons=result.reasons,metrics=readings,inference_status="not_calibrated",
        calibrated_threshold=None,effect_conclusion=None,causal_attribution=False)
