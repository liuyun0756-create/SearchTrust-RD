"""Strict JSON-native dates; keep the existing capability contracts untouched."""
from pydantic import ConfigDict
from app.report_v22.models import StrictModel as LegacyStrictModel


class StrictModel(LegacyStrictModel):
    # No wildcard before-validator: Pydantic 2.10 loses strict JSON date parsing
    # across that boundary. Dates/UUIDs retain their ordinary strict JSON types.
    model_config = ConfigDict(str_strip_whitespace=False, revalidate_instances="always")
