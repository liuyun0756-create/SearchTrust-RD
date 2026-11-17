"""V17 inference projection over already gated comparisons; no providers or clock."""
from copy import deepcopy
import logging
from typing import Literal
from pydantic import Field, model_validator
from app.report_v22.models import StrictModel
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest

RULESET = 'v22_capability_public_v17'
DISCLAIMER = 'This difference may be one contributing factor. These observations do not establish ranking causation.'


class PossibleReason(StrictModel):
    metric: Literal['rating', 'review_count']
    classification: Literal['inference'] = 'inference'
    basis_refs: list[str] = Field(min_length=4, max_length=401)
    competitor_indices: list[int] = Field(min_length=2, max_length=3)
    customer_text: str = Field(min_length=1, max_length=2000)


class PossibleReasons(StrictModel):
    schema_version: Literal['possible_reasons_v1'] = 'possible_reasons_v1'
    status: Literal['assessed', 'not_assessed']
    items: list[PossibleReason] = Field(max_length=2)

    @model_validator(mode='after')
    def coherent(self):
        if (self.status == 'assessed') != bool(self.items):
            raise ValueError('no eligible differences means not assessed')
        return self


def build_possible_reasons(gbp, landing):
    from app.report_v22.gbp_comparison import GbpComparison
    from app.report_v22.landing_page_comparison import LandingComparison
    # Revalidate actual rows, directions, timing and evidence gates. A flag alone is insufficient.
    if gbp is None or landing is None:
        return PossibleReasons(status='not_assessed', items=[])
    gbp = GbpComparison.model_validate_json(canonical_json_bytes(gbp))
    LandingComparison.model_validate_json(canonical_json_bytes(landing))
    items = []
    for metric in ('rating', 'review_count'):
        index, difference = next((i, d) for i, d in enumerate(gbp.differences) if d.metric == metric)
        if difference.status != 'observed' or difference.direction != 'higher':
            continue
        rows = [gbp.rows[0], *(gbp.rows[i] for i in difference.competitor_indices)]
        values = [r.metrics[metric] for r in rows]
        if any(not m.source_refs for m in values):
            continue
        nums = [str(int(m.value)) if m.value.is_integer() else str(m.value) for m in values]
        label, own = ('ratings', 'rating') if metric == 'rating' else ('review totals', 'review total')
        text = f"Sample competitor {label}: {', '.join(nums[1:])}; your {own}: {nums[0]}. {DISCLAIMER}"
        items.append(PossibleReason(metric=metric, competitor_indices=difference.competitor_indices,
            basis_refs=[f'/gbp_comparison/differences/{index}', *(ref for m in values for ref in m.source_refs)], customer_text=text))
    # V16 landing comparisons never establish directional gaps (bounded counts/positive lower bounds).
    # Do not turn unobserved content, unequal lower bounds or category sets into reasons.
    return PossibleReasons(status='assessed' if items else 'not_assessed', items=items)


def select_reasons_ruleset(gbp, landing, reasons):
    from app.report_v22.landing_page_comparison import select_comparison_ruleset
    previous = select_comparison_ruleset(gbp, landing)
    if reasons and not (gbp and landing):
        logging.getLogger(__name__).warning('comparison reasons configuration warning: GBP and LANDING are required; keeping existing ruleset',
            extra={'configuration_warning': 'reasons_requires_gbp_and_landing'})
    return RULESET if reasons and gbp and landing else previous


def prepare_reasons_input(manifest, request):
    from app.report_v22.landing_page_comparison import prepare_landing_input
    from app.report_v22.capability import AnalysisInputManifest
    if manifest.ruleset_version == RULESET:
        return manifest, request
    if manifest.ruleset_version != 'v22_capability_public_v16':
        manifest, request = prepare_landing_input(manifest, request)
    raw = deepcopy(manifest.model_dump(mode='json'))
    raw.update(schema_version='v22_analysis_input_v7', ruleset_version=RULESET)
    raw['input_checksum'] = verified_request_digest({k: v for k, v in raw.items() if k != 'input_checksum'})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request
