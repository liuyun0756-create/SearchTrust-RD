"""V16 bounded, deterministic observations of saved landing-page content only."""
from copy import deepcopy
from datetime import datetime, timedelta
import re
import logging
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.models import StrictModel

RULESET = 'v22_capability_public_v16'
EXTRACTION = 'landing_content_rules_v1'
METRICS = ('local_entities', 'case_study', 'portfolio', 'credentials', 'service_price', 'discount', 'project_budget')
CITY_LIMIT = 'Only the confirmed target city is checked; other cities, neighbourhoods and landmarks are not assessed.'


def select_comparison_ruleset(gbp_enabled, landing_enabled):
    if landing_enabled and not gbp_enabled:
        logging.getLogger(__name__).warning('comparison configuration warning: V22_LANDING_PAGE_COMPARISON_ENABLED=true requires V22_GBP_COMPARISON_ENABLED=true; keeping v14',
            extra={'configuration_warning': 'landing_requires_gbp', 'gbp_enabled': False, 'landing_enabled': True})
    return RULESET if gbp_enabled and landing_enabled else 'v22_capability_public_v15' if gbp_enabled else 'v22_capability_public_v14'


def normalized(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def claim_key(key, evidence):
    if key == 'credentials':
        number = re.search(r'\b(?:licen[cs]e|CSLB|registration)\s*(?:number|no\.?|#|:|\s)*([A-Z-]*\d{4,})', evidence.quote, re.I)
        if number:
            return 'license:' + number.group(1).casefold()
    if key == 'case_study':
        return re.sub(r'\W+', '', re.split(r'estimated budget', evidence.quote, flags=re.I)[0].casefold())
    if key == 'discount':
        value = evidence.quote[evidence.quote.find(re.search(r'[$£]', evidence.quote).group()):]
        return re.sub(r'[^a-z0-9$£]', '', value.casefold())
    return evidence.quote.casefold()


class LandingEvidence(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    snapshot_id: UUID
    path: str = Field(pattern=r'^/payload/(?:competitors/\d+/site_inventory/)?selected_pages/\d+/deep_snapshot/(?:text|title|h1)$')
    url: str = Field(min_length=1, max_length=2083)
    quote: str = Field(min_length=1, max_length=300)
    location: Literal['title', 'h1', 'body', 'footer']
    sampled_at: str = Field(max_length=64)


class LandingMetric(StrictModel):
    status: Literal['observed', 'not_checked']
    count: int | None = Field(default=None, ge=1, le=100)
    lower_bound: bool = False
    reason: Literal['saved_positive', 'not_observed_in_saved_sample', 'no_usable_pages', 'source_unavailable', 'identity_unresolved', 'target_city_unavailable']
    evidence: list[LandingEvidence] = Field(max_length=100)

    @model_validator(mode='after')
    def coherent(self):
        if (self.status == 'observed') != (self.count is not None):
            raise ValueError('unchecked landing values must not become zero')
        if self.status == 'observed' and (self.count != len(self.evidence) or not self.lower_bound or self.reason != 'saved_positive'):
            raise ValueError('positive observations require bounded source evidence')
        if self.status == 'not_checked' and (self.evidence or self.lower_bound or self.reason == 'saved_positive'):
            raise ValueError('unchecked evidence must remain unknown')
        if any(datetime.fromisoformat(e.sampled_at.replace("Z", "+00:00")).utcoffset() is None for e in self.evidence):
            raise ValueError('original sampling times require timezone')
        return self


class LandingRow(StrictModel):
    subject: Literal['customer', 'competitor']
    name: str = Field(min_length=1, max_length=240)
    snapshot_id: UUID | None
    inventory_path: str
    eligible_html_count: int = Field(ge=0, le=500)
    deep_page_count: int = Field(ge=0, le=50)
    page_type_counts: dict[str, int]
    coverage: Literal['bounded_sample'] = 'bounded_sample'
    metrics: dict[str, LandingMetric]

    @model_validator(mode='after')
    def keys(self):
        from app.collectors.site_inventory_models import SITE_PAGE_TYPE_ORDER
        if set(self.metrics) != set(METRICS) or set(self.page_type_counts) != set(SITE_PAGE_TYPE_ORDER):
            raise ValueError('landing metric/type keys required')
        if any(type(x) is not int or x < 0 for x in self.page_type_counts.values()) or sum(self.page_type_counts.values()) != self.eligible_html_count:
            raise ValueError('sample page counts mismatch')
        return self


class LandingInference(StrictModel):
    classification: Literal['inference'] = 'inference'
    metric: Literal['page_type_counts', *METRICS]
    status: Literal['not_checked'] = 'not_checked'
    reason: Literal['bounded_counts', 'customer_unchecked', 'insufficient_comparators', 'comparison_time_gap', 'no_supported_difference']
    competitor_indices: list[int] = Field(max_length=3)
    evidence: list[LandingEvidence] = Field(max_length=400)


def compare_rows(rows):
    out = [LandingInference(metric='page_type_counts', reason='bounded_counts', competitor_indices=[], evidence=[])]
    for key in METRICS:
        customer = rows[0].metrics[key]
        eligible = [(i, r.metrics[key]) for i, r in enumerate(rows[1:], 1) if r.metrics[key].status == 'observed']
        reason, indices, evidence = 'customer_unchecked', [], []
        if customer.status == 'observed':
            reason = 'insufficient_comparators'
            indices = [i for i, _ in eligible]
            evidence = customer.evidence + [e for _, m in eligible for e in m.evidence]
            if len(eligible) >= 2:
                times = [datetime.fromisoformat(e.sampled_at.replace("Z", "+00:00")) for e in evidence]
                reason = 'comparison_time_gap' if max(times) - min(times) > timedelta(hours=24) else 'no_supported_difference'
        out.append(LandingInference(metric=key, reason=reason, competitor_indices=indices, evidence=evidence))
    return out


class LandingComparison(StrictModel):
    schema_version: Literal['landing_page_comparison_v1'] = 'landing_page_comparison_v1'
    extraction_version: Literal['landing_content_rules_v1'] = EXTRACTION
    target_city: str | None = Field(min_length=1, max_length=200)
    city_limit: Literal['Only the confirmed target city is checked; other cities, neighbourhoods and landmarks are not assessed.'] = CITY_LIMIT
    rows: list[LandingRow] = Field(min_length=1, max_length=4)
    comparisons: list[LandingInference] = Field(min_length=8, max_length=8)

    @model_validator(mode='after')
    def coherent(self):
        if self.rows[0].subject != 'customer' or any(r.subject != 'competitor' for r in self.rows[1:]):
            raise ValueError('customer must precede competitors')
        if self.comparisons != compare_rows(self.rows):
            raise ValueError('landing comparison gates mismatch')
        if any((r.metrics['local_entities'].count or 0) > 1 for r in self.rows):
            raise ValueError('only one confirmed target city is assessed')
        return self


def prepare_landing_input(manifest, request):
    from app.report_v22.gbp_comparison import prepare_comparison_input
    from app.report_v22.capability import AnalysisInputManifest
    if manifest.ruleset_version == RULESET:
        return manifest, request
    if manifest.ruleset_version != 'v22_capability_public_v15':
        manifest, request = prepare_comparison_input(manifest, request)
    raw = deepcopy(manifest.model_dump(mode='json'))
    raw.update(schema_version='v22_analysis_input_v6', ruleset_version=RULESET)
    raw['input_checksum'] = verified_request_digest({k: v for k, v in raw.items() if k != 'input_checksum'})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request


def extract_content(deep, *, city, business_name, snapshot_id, path):
    """Positive observations only. No provider, file, clock, or mutable input."""
    found = {key: [] for key in METRICS}
    if not deep.html or not deep.text or len(deep.html) > 5_000_000:
        return found
    try:
        from app.report_v22.site_business_html import parse_html, ParseBudget
        from app.report_v22.site_business_models import SiteBusinessLimits
        from app.report_v22.site_business_errors import SiteBusinessError
        tree = parse_html(deep.html, path, ParseBudget(SiteBusinessLimits()))
    except (ValueError, TypeError, SiteBusinessError):
        return found
    def ancestors(node):
        while node.parent is not None:
            node = node.parent
            yield node
    def ignored(node):
        return node.hidden or node.bad or not node.closed or any(n.tag in {'nav', 'header', 'form'} for n in [node, *ancestors(node)])
    def visible(node):
        if ignored(node):
            return ''
        return ' '.join(child if isinstance(child, str) else visible(child) for child in node.children)
    def descendants(node):
        for child in node.elements:
            if not ignored(child):
                yield child
                yield from descendants(child)
    text = normalized(deep.text)
    city_re = re.compile(r'(?<!\w)' + re.escape(city) + r'(?!\w)', re.I) if city else None
    seen = {key: set() for key in METRICS}

    def add(key, quote, location='body', field='text', identity=None):
        quote = normalized(quote)[:300]
        if not quote or quote.casefold() not in normalized(getattr(deep, field)).casefold():
            return
        identity = identity or quote.casefold()
        if identity in seen[key] or len(found[key]) >= 100:
            return
        seen[key].add(identity)
        found[key].append(LandingEvidence(snapshot_id=snapshot_id, path=path + '/' + field, url=str(deep.url),
            quote=quote, location=location, sampled_at=deep.collected_at.isoformat()))

    def city_context(value):
        # A city occurring only inside the saved business name is not local content.
        if city_re is None:
            return None
        if set(re.findall(r'\w+', value.casefold())) <= set(re.findall(r'\w+', business_name.casefold())):
            return None
        names = [business_name]
        words = business_name.split()
        if city_re.search(' '.join(words[:2])):
            names.extend(' '.join(words[:n]) for n in range(2, len(words)))
        spans = [(m.start(), m.end()) for name in names for m in re.finditer(re.escape(name), value, re.I)]
        return next((m for m in city_re.finditer(value) if not any(a <= m.start() < b for a, b in spans)), None)

    for field in ('title', 'h1'):
        value = getattr(deep, field) or ''
        if city_context(value):
            add('local_entities', value, field, field, city.casefold())
    for node in tree.nodes:
        if node.tag not in {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'address', 'footer', 'figcaption'} or ignored(node):
            continue
        value = normalized(visible(node))
        if not value or len(value) > 4000:
            continue
        location = 'footer' if node.tag == 'footer' or any(n.tag == 'footer' for n in ancestors(node)) else 'body'
        match = city_context(value)
        if match:
            original = match
            add('local_entities', value[max(0, original.start()-40):original.end()+100], location, identity=city.casefold())
        if len(value) < 15:
            continue
        # Explicit numbers or a self-attributed assertion, never job requirements/advice.
        license_match = re.search(r'\b(?:licen[cs]e|CSLB|registration)\s*(?:number|no\.?|#|:|\s)*[A-Z-]*\d{4,}', value, re.I)
        self_claim = re.search(r"\bwe (?:are|have|employ) (?:\w+ ){0,3}(?:licensed|certified|insured|bonded)\b|\bour (?:licensed|certified|insured|bonded) (?:team|staff|technicians|installers|plumbers|engineers)\b|\bour (?:team|staff|technicians|installers|company|plumbers|engineers) (?:is|are|of|consists of) (?:\w+ ){0,3}(?:licensed|certified|insured|bonded)\b", value, re.I)
        excluded = re.search(r'\b(?:must|should|required|driver|applicant|recommend|not|never|unlicensed)\b', value, re.I)
        if not excluded and (license_match or (len(value) >= 30 and self_claim)):
            m = license_match or self_claim
            add('credentials', value[max(0, m.start()-30):m.end()+100], location,
                identity=normalized(m.group()).casefold())
        if location == 'footer' or node.tag in ('h1', 'h2', 'h3', 'h4'):
            continue
        money = re.search(r'[$£]\s?\d[\d,.]*', value)
        if money and not any(re.search(r'review|testimonial', n.attrs.get('class', ''), re.I) for n in [node, *ancestors(node)]):
            near = value[max(0, money.start()-90):money.end()+90]
            budget = bool(len(value) >= 40 and re.search(r'\bproject\b', value, re.I) and re.search(r'\b(?:estimated\s+)?budget\s*:', near, re.I))
            discount = bool(re.search(r'\b(?:off|save|discount|coupon)\b', near, re.I))
            if budget:
                add('project_budget', near)
            elif discount:
                add('discount', near)
            elif re.search(r'\b(?:from|starts? at|starting at|price|pricing|service fee|call[- ]?out)\s*[:\-]?\s*[$£]\s?\d|[$£]\s?\d[\d,.]*\s*(?:per|/)\s*(?:hour|visit|service)', near, re.I):
                add('service_price', near)
        if len(value) >= 160 and re.search(r'\b(?:project|case study)\b', value, re.I) and re.search(r'\b(?:completed|installed|repaired|replaced|built)\b', value, re.I):
            details = sum(bool(re.search(pattern, value, re.I)) for pattern in (r'\bbudget\b', r'\b(?:client|customer)\b', r'\blocation\b', r'\bbefore\b', r'\bafter\b'))
            if details >= 2:
                add('case_study', value)
    for heading in tree.nodes:
        if heading.tag not in {'h1', 'h2', 'h3', 'h4'} or ignored(heading):
            continue
        title = normalized(visible(heading))
        if (re.search(r'project management|project delivery|\bFAQ\b|\bquestions?\b', title, re.I)
                or not re.search(r'^(?:projects\b|portfolio\b|(?:photo |image |project image )?gallery\b|our works?\b)|\b(?:our (?:latest )?|recent |latest |completed |additional |featured )[^.!?]{0,50}\bprojects\b', title, re.I)):
            continue
        container = heading.parent
        for _ in range(4):
            if container is None or container.tag in ('html', 'body'):
                break
            value = normalized(visible(container))
            children = list(descendants(container))
            links = [a for a in children if a.tag == 'a' and re.search(r'project|portfolio|case-study', a.attrs.get('href', ''), re.I)]
            if 40 <= len(value) <= 12000 and (sum(c.tag == 'img' and bool(c.attrs.get('src') or c.attrs.get('data-src')) for c in children) >= 2 or links):
                add('portfolio', value if len(value) <= 300 else title)
                # If nested markup splits title/description, retain the title itself as locator evidence.
                if not found['portfolio']:
                    add('portfolio', title)
                break
            container = container.parent
    if found['local_entities']:
        found['local_entities'] = found['local_entities'][:1]
    return found


def build_landing_comparison(request):
    from app.report_v22.evidence_adapters.site_counts import eligible_html
    from app.collectors.site_inventory_models import SITE_PAGE_TYPE_ORDER
    from app.report_v22.evidence_bindings import host
    sources = {s.kind: s for s in request.evidence_input.sources}
    city = request.evidence_input.context.target_market.city
    rows = []

    def row(subject, name, source, inventory, path, eligible=True):
        counts = {key: 0 for key in SITE_PAGE_TYPE_ORDER}
        metrics = {key: [] for key in METRICS}
        deep_count = 0
        if inventory is not None and eligible:
            for page in inventory.pages:
                if eligible_html(page):
                    counts[page.page_type] += 1
            for index, page in enumerate(inventory.selected_pages):
                deep = page.deep_snapshot
                if (not page.deep_analyzed or deep is None or not (200 <= deep.status_code < 300)
                        or deep.content_type.split(';', 1)[0].lower() not in ('text/html', 'application/xhtml+xml')):
                    continue
                deep_count += 1
                found = extract_content(deep, city=city, business_name=name, snapshot_id=source.binding.snapshot_id,
                    path=path + f'/selected_pages/{index}/deep_snapshot')
                for key in METRICS:
                    for evidence in found[key]:
                        if key == 'local_entities' and metrics[key]:
                            continue
                        if all(claim_key(key, e) != claim_key(key, evidence) for e in metrics[key]) and len(metrics[key]) < 100:
                            metrics[key].append(evidence)
        values = {key: LandingMetric(status='observed', count=len(es), lower_bound=True, reason='saved_positive', evidence=es)
            if es else LandingMetric(status='not_checked', reason='identity_unresolved' if not eligible else 'source_unavailable' if inventory is None
                else 'target_city_unavailable' if key == 'local_entities' and not city
                else 'no_usable_pages' if not deep_count else 'not_observed_in_saved_sample', evidence=[]) for key, es in metrics.items()}
        return LandingRow(subject=subject, name=name, snapshot_id=source.binding.snapshot_id if source else None, inventory_path=path,
            eligible_html_count=sum(counts.values()), deep_page_count=deep_count, page_type_counts=counts, metrics=values)

    customer = sources.get('site')
    rows.append(row('customer', request.business_identity.business_name, customer, customer.payload if customer else None, '/payload'))
    competitor = sources.get('competitor')
    if competitor:
        for index, item in enumerate(competitor.payload.competitors):
            eligible = competitor.binding.identity_match_status == 'matched' and item.site_status != 'unavailable' and item.site_inventory is not None
            if eligible:
                eligible = host(item.competitor.website_url) == host(item.site_inventory.root_url)
            rows.append(row('competitor', item.competitor.business_name, competitor, item.site_inventory, f'/payload/competitors/{index}/site_inventory', eligible))
    return LandingComparison(target_city=city, rows=rows, comparisons=compare_rows(rows))
