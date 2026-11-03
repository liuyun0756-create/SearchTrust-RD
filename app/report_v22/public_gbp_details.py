"""Bounded decoding of optional place details; absence is never a zero value."""
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.report_v22.public_gbp_models import PublicGbpFieldsV2

DETAIL_KEYS = ('type', 'types', 'rating', 'reviews', 'hours', 'operating_hours', 'photos_count', 'photo_count')
_ADAPTERS = {name: TypeAdapter(field.annotation) for name, field in PublicGbpFieldsV2.model_fields.items()}


def _empty(value):
    return value is None or isinstance(value, str) and not value.strip() or isinstance(value, (list, dict)) and not value


def _detail(raw: dict[str, Any], keys: tuple[str, ...], field: str, *, multiple=False):
    absent = [] if multiple else None
    values = [raw[key] for key in keys if key in raw]
    if not values:
        return {'state': 'not_returned', 'value': absent}
    values = [v for v in values if not _empty(v)]
    if not values:
        return {'state': 'returned_empty', 'value': absent}
    try:
        if multiple:
            combined = []
            for value in values:
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list) or len(value) > 20 or any(type(item) is not str or not item.strip() for item in value):
                    raise ValueError()
                for item in value:
                    if item not in combined:
                        combined.append(item)
            value = combined
        else:
            if any(type(v) != type(values[0]) or v != values[0] for v in values[1:]):
                raise ValueError()
            value = values[0]
            if field in {'rating', 'review_count', 'photo_count'} and type(value) not in {int, float}:
                raise ValueError()
            if field in {'review_count', 'photo_count'} and type(value) is not int:
                raise ValueError()
        result = _ADAPTERS[field].validate_python({'state':'observed', 'value':value}, strict=True)
        if field in {'hours', 'operating_hours'}:
            days = result.value if isinstance(result.value, list) else [result.value]
            if any(not text.strip() for day in days for text in day.values()):
                raise ValueError()
        return result.model_dump(mode='python')
    except (ValueError, TypeError, ValidationError):
        # Invalid/unknown shapes do not erase the whole otherwise verified profile
        # and do not masquerade as not_returned, returned_empty, or a count of zero.
        return {'state':'unsupported', 'value':absent}


def detail_fields(raw):
    return {
        'categories': _detail(raw, ('type','types'), 'categories', multiple=True),
        'rating': _detail(raw, ('rating',), 'rating'),
        'review_count': _detail(raw, ('reviews',), 'review_count'),
        'hours': _detail(raw, ('hours',), 'hours'),
        'operating_hours': _detail(raw, ('operating_hours',), 'operating_hours'),
        'photo_count': _detail(raw, ('photos_count','photo_count'), 'photo_count'),
    }
