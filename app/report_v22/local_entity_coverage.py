"""Target-city observations from bound website samples."""
from dataclasses import dataclass
import logging
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.landing_page_comparison import CITY_LIMIT, LandingEvidence, extract_content
from app.report_v22.models import StrictModel
from app.report_v22.site_business_html import HTML_TOLERANCE_RULESET as RULESET


@dataclass
class CityPositionScan:
    complete: bool = False


def archive_url(url: str) -> bool:
    return any(part.casefold() in {'tag', 'tags', 'category', 'categories'}
               for part in urlsplit(url).path.split('/'))


def observation_key(evidence: LandingEvidence):
    return (evidence.url, evidence.location, evidence.path)


class LocalEntityCoverage(StrictModel):
    schema_version: Literal['local_entity_coverage_v1'] = 'local_entity_coverage_v1'
    target_city: str | None = Field(min_length=1, max_length=200)
    city_limit: Literal['Only the confirmed target city is checked; other cities, neighbourhoods and landmarks are not assessed.'] = CITY_LIMIT
    status: Literal['present', 'weak_position', 'not_detected', 'not_checked']
    reason: Literal['saved_positive', 'not_observed_in_saved_sample', 'target_city_unavailable', 'incomplete_sample']
    complete: bool
    archive_headings_only: bool
    evidence: list[LandingEvidence] = Field(max_length=200)

    @model_validator(mode='after')
    def coherent(self):
        headings = [e for e in self.evidence if e.location in {'title', 'h1'}]
        expected = ('present' if headings else 'weak_position' if self.evidence
                    else 'not_detected' if self.complete else 'not_checked')
        reason = ('saved_positive' if self.evidence else 'not_observed_in_saved_sample' if self.complete
                  else 'target_city_unavailable' if not self.target_city else 'incomplete_sample')
        keys = [observation_key(e) for e in self.evidence]
        if (self.status != expected or self.reason != reason or keys != sorted(set(keys))
                or self.archive_headings_only != (bool(headings) and all(archive_url(e.url) for e in headings))
                or (not self.target_city and (self.evidence or self.complete))
                or sum(e.location == 'footer' for e in self.evidence) > 1):
            raise ValueError('invalid local entity coverage')
        return self


def build_local_entity_coverage(request):
    city = request.evidence_input.context.target_market.city
    source = next((s for s in request.evidence_input.sources if s.kind == 'site'), None)
    entries = []
    complete = False
    if city and source:
        pages = source.payload.selected_pages
        complete = bool(pages)
        for index, page in enumerate(pages):
            deep = page.deep_snapshot
            if (not page.deep_analyzed or deep is None or not 200 <= deep.status_code < 300
                    or deep.content_type.split(';', 1)[0].strip().lower() not in {'text/html', 'application/xhtml+xml'}):
                complete = False
                continue
            scan = CityPositionScan()
            found = extract_content(deep, city=city, business_name=request.business_identity.business_name,
                                    snapshot_id=source.binding.snapshot_id,
                                    path=f'/payload/selected_pages/{index}/deep_snapshot', city_positions=scan)
            entries.extend(found['local_entities'])
            complete &= scan.complete
    evidence = []
    seen = set()
    for entry in sorted(entries, key=observation_key):
        # One target city in the shared footer remains one observation.
        key = 'footer' if entry.location == 'footer' else observation_key(entry)
        if key not in seen:
            evidence.append(entry)
            seen.add(key)
    headings = [e for e in evidence if e.location in {'title', 'h1'}]
    status = ('present' if headings else 'weak_position' if evidence
              else 'not_detected' if complete else 'not_checked')
    reason = ('saved_positive' if evidence else 'not_observed_in_saved_sample' if complete
              else 'target_city_unavailable' if not city else 'incomplete_sample')
    return LocalEntityCoverage(target_city=city, status=status, reason=reason, complete=complete,
                               archive_headings_only=bool(headings) and all(archive_url(e.url) for e in headings),
                               evidence=evidence)


def select_local_entity_ruleset(gbp, landing, reasons, enabled):
    from app.report_v22.comparison_reasons import select_reasons_ruleset, RULESET as REASONS_RULESET

    previous = select_reasons_ruleset(gbp, landing, reasons)
    if not enabled:
        return previous
    if previous == REASONS_RULESET:
        return RULESET
    logging.getLogger(__name__).warning(
        'V22_LOCAL_ENTITY_COVERAGE_ENABLED requires GBP, LANDING and REASONS. Keeping existing ruleset.',
        extra={'configuration_warning': 'local_entity_requires_comparisons'})
    return previous


def prepare_local_entity_input(manifest, request):
    from app.report_v22.capability import AnalysisInputManifest
    from app.report_v22.comparison_reasons import prepare_reasons_input

    if manifest.ruleset_version == RULESET:
        return manifest, request
    manifest, request = prepare_reasons_input(manifest, request)
    raw = manifest.model_dump(mode='json')
    raw.update(schema_version='v22_analysis_input_v8', ruleset_version=RULESET)
    raw['input_checksum'] = verified_request_digest({k: v for k, v in raw.items() if k != 'input_checksum'})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request
