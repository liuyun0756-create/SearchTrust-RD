"""V11-only, bounded weekly hours observations. No IO or inferred closing days."""
import json
import re
import unicodedata
from types import SimpleNamespace
from collections import Counter

from app.report_v22.site_business_html import ParseBudget, parse_html
from app.report_v22.site_business_text import text_visible, HIDDEN_STYLE
from app.report_v22.site_business_jsonld import jsonld_candidates, records, business
from app.report_v22.site_business_models import SiteBusinessLimits
from app.report_v22.site_business_bindings import in_scope
from app.collectors.site_inventory_urls import SiteScope
from app.report_v22.site_business_errors import SiteBusinessError
from app.report_v22.evidence_bindings import eligibility

DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
DAY = r'(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)'
DAY_RE = re.compile(r'\b('+DAY+r')(?:\s*-\s*('+DAY+r'))?\b', re.I)
TIME = r'\d{1,2}(?::\d{2})?\s*(?:[ap]\s*\.?\s*m\.?)?'
INTERVAL = re.compile(r'(?<!\d)('+TIME+r')\s*(?:-|to)\s*('+TIME+r')(?!\d)', re.I)
LABEL = re.compile(r'\b(?:opening hours|business hours|office hours|hours|open daily|open every day)\b', re.I)
CATEGORY_ORDER = ('parsed','parse_ambiguous','tour_schedule','emergency','appointment_only','other')
HOURS_NOTE = 'The published hours on the two channels differ. Please check both channels; this does not establish actual operating hours.'


def normalized(text):
    text = unicodedata.normalize('NFKC', text).replace('\xa0',' ')
    return re.sub(r'[–—−]', '-', text).strip()


def day_number(text):
    return next(i for i, d in enumerate(DAYS) if d[:3] == text.lower()[:3])


def clock_minutes(text):
    m = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*([ap]\s*\.?\s*m\.?)?', text.strip(), re.I)
    if not m: raise ValueError('unsupported_time')
    h, minute = int(m[1]), int(m[2] or 0)
    if minute > 59: raise ValueError('invalid_time')
    if m[3]:
        if not 1 <= h <= 12: raise ValueError('invalid_time')
        return (h % 12 + (12 if m[3].lower().startswith('p') else 0))*60+minute
    if m[2] is None: raise ValueError('meridiem_missing')
    if h > 24 or (h == 24 and minute): raise ValueError('invalid_time')
    return h*60+minute


def parse_week(text, *, structured=False):
    """Return explicit days only; caller retains the complete original text."""
    text = normalized(text)
    if len(text) > 4000:
        return dict(classification='parse_ambiguous', purpose='unknown', days={}, reason='resource_limit')
    lo = text.casefold()
    if re.search(r'\b(?:tours?|hikes?|departure|departures|start times?|group tour|private hike|duration|meeting time)\b', lo):
        return dict(classification='tour_schedule', purpose='tour_schedule', days={}, reason='not_business_hours')
    if re.search(r'\b(?:emergency|emergencies|hotline)\b', lo):
        return dict(classification='emergency', purpose='emergency', days={}, reason='not_regular_hours')
    if re.search(r'\b(?:appointment|by request)\b', lo):
        return dict(classification='appointment_only', purpose='appointment_only', days={}, reason='appointment_condition')
    if re.search(r'\b(?:holiday|seasonal|temporary|christmas|thanksgiving)\b|\b\d{4}-\d{2}-\d{2}\b', lo):
        return dict(classification='other', purpose='dated_special', days={}, reason='dated_hours')
    purpose = 'office' if 'office' in lo else 'regular_business'
    result = dict(classification='parse_ambiguous', purpose=purpose, days={}, reason='unsupported_hours_format')
    # Never mix explicitly different zones or infer a timezone from a market point.
    if re.search(r'\b(?:utc|gmt|pst|pdt|est|edt|cst|cdt|hst|bst)\b|[A-Za-z]+/[A-Za-z_]+', text, re.I):
        return dict(result, reason='explicit_timezone_requires_review')
    matches = list(DAY_RE.finditer(text))
    daily = bool(re.search(r'\b(?:every day|daily|seven days|7 days)\b|24\s*/\s*7', text, re.I))
    chunks = []
    if matches:
        for i, m in enumerate(matches):
            start, end = day_number(m[1]), day_number(m[2] or m[1])
            days = [(start+j)%7 for j in range((end-start)%7+1)]
            chunks.append((days, text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)]))
        # "Mon, Wed and Fri 9am-5pm": empty connectors share the following explicit interval.
        for i in range(len(chunks)-2,-1,-1):
            if re.fullmatch(r'[\s,:;&]*(?:and)?[\s,:;&]*', chunks[i][1], re.I):
                chunks[i] = (chunks[i][0], chunks[i+1][1])
    elif daily:
        chunks = [(list(range(7)), text)]
    else:
        return dict(result, reason='days_not_explicit')
    weekly, transforms = {}, []
    try:
        for days, body in chunks:
            closed = bool(re.search(r'\b(?:closed|not open)\b', body, re.I))
            all_day = bool(re.search(r'\b(?:open\s*)?24\s*(?:hours?|/\s*7)\b', body, re.I))
            spans = list(INTERVAL.finditer(body))
            if closed and (all_day or spans): raise ValueError('conflicting_day_states')
            if closed: ranges = []
            elif all_day: ranges = [[0,1440]]
            else:
                if not spans: raise ValueError('time_not_explicit')
                ranges = []
                for span in spans:
                    a,b = clock_minutes(span[1]), clock_minutes(span[2])
                    if a == b or a == 1440: raise ValueError('ambiguous_equal_endpoints')
                    if structured and a == 0 and b == 1439:
                        b = 1440; transforms.append('structured_full_day_2359')
                    ranges.append([a,b])
            residue = INTERVAL.sub('', body)
            residue = re.sub(r'24\s*(?:hours?|/\s*7)|7\s*days|24\s*/\s*7', '', residue, flags=re.I)
            residue = re.sub(r'\b(?:opening|business|office|hours|open|closed|not|daily|every|day|seven|days|a|week|and|to)\b', '', residue, flags=re.I)
            if re.search(r'[A-Za-z0-9]', residue): raise ValueError('unparsed_hours_condition')
            if len(ranges)>8: raise ValueError('resource_limit')
            for day in days:
                key = str(day)
                if key in weekly and weekly[key] != ranges: raise ValueError('conflicting_day_states')
                weekly[key] = ranges
        expanded = {}
        for day, ranges in weekly.items():
            own = []
            for a,b in ranges:
                if b<a:
                    own.append([a,1440])
                    next_day = str((int(day)+1)%7)
                    if next_day in weekly and not any((c == 0 and d >= b) or d < c for c,d in weekly[next_day]): raise ValueError('conflicting_overnight')
                    expanded.setdefault(next_day,[]).append([0,b])
                else: own.append([a,b])
            expanded.setdefault(day,[]).extend(own)
        for day, ranges in expanded.items():
            merged=[]
            for a,b in sorted(ranges):
                if merged and a<=merged[-1][1]: merged[-1][1]=max(b,merged[-1][1])
                else: merged.append([a,b])
            expanded[day]=merged
        return dict(classification='parsed',purpose=purpose,days=expanded,reason=None,transformations=sorted(set(transforms)))
    except ValueError as exc:
        return dict(result, reason=str(exc))


def parse_spec(value):
    specs = value if isinstance(value,list) else [value]
    by_day={}
    for spec in specs:
        if not isinstance(spec,dict): return dict(classification='parse_ambiguous',purpose='unknown',days={},reason='invalid_structured_hours')
        if spec.get('validFrom') or spec.get('validThrough'):
            return dict(classification='other',purpose='dated_special',days={},reason='dated_hours')
        days=spec.get('dayOfWeek',[]);days=days if isinstance(days,list) else [days]
        names=[]
        for day in days:
            name=str(day).rsplit('/',1)[-1].lower()
            if name not in DAYS: return dict(classification='parse_ambiguous',purpose='unknown',days={},reason='invalid_weekday')
            names.append(name)
        if not names: return dict(classification='parse_ambiguous',purpose='unknown',days={},reason='days_not_explicit')
        for name in names:
            by_day.setdefault(name, []).append(f"{spec.get('opens','')} - {spec.get('closes','')}")
    return parse_week('\n'.join(f"{day}: {', '.join(parts)}" for day,parts in by_day.items()),structured=True)


def extract_page(html, *, url, path, identity, budget):
    """Source-position candidates. Schema ownership uses the existing extractor."""
    candidates=[]
    try:
        tree=parse_html(html,path,budget)
        drafts, diagnostics, documents=jsonld_candidates(tree,url,url,SiteScope.from_root(str(identity.site_url)))
        # Ownership checks in the old extractor are retained, not relaxed by names/hours.
        owners={(o.start,o.end,o.record_pointer):d.ownership_status for d in drafts for o in d.origins if d.source_kind=='jsonld_property'}
        for block in tree.json_blocks:
            doc=documents.get((block.content_start,block.content_end))
            if doc is None: continue
            for pointer, record in records(doc):
                if not business(record):continue
                owner=owners.get((block.content_start,block.content_end,pointer))=='declared_entity'
                for prop in ('openingHours','openingHoursSpecification','specialOpeningHoursSpecification'):
                    if prop not in record:continue
                    raw=json.dumps(record[prop],ensure_ascii=False,sort_keys=True)
                    parsed=parse_spec(record[prop]) if prop.endswith('Specification') else parse_week(' '.join(record[prop]) if isinstance(record[prop],list) else str(record[prop]))
                    if prop=='specialOpeningHoursSpecification':parsed=dict(classification='other',purpose='dated_special',days={},reason='dated_hours')
                    if not owner:parsed=dict(classification='other',purpose='unknown',days={},reason='identity_unresolved')
                    candidates.append(dict(raw=raw,url=str(url),path=path,start=block.content_start,end=block.content_end,
                        json_pointer=pointer+'/'+prop,basis='structured',**parsed))
        selected={}
        for node in tree.nodes:
            if node.tag not in {'p','div','li','td','dd','h1','h2','h3','h4','span'}:continue
            ancestor=node
            hidden=False
            while ancestor is not None:
                if ancestor.hidden or HIDDEN_STYLE.search(ancestor.attrs.get('style') or ''):
                    hidden=True;break
                ancestor=ancestor.parent
            if hidden:continue
            text=' '.join(text_visible(node).split())
            if not text or len(text)>1000:continue
            time_context = (DAY_RE.search(text) or LABEL.search(text) or re.search(r'\b(?:every day|daily)\b',text,re.I)
                or re.search(r'\b(?:emergency|tour|departure|appointment only|by appointment|by request)\b', text, re.I))
            if not (INTERVAL.search(normalized(text)) or re.search(r'24\s*(?:hours?|/\s*7)|appointment|by request|\bclosed\b(?!-)|\b\d{1,2}\s*[ap]m\b',text,re.I)):continue
            # Prefer the smallest complete time-containing block; collect label context locally.
            if any(INTERVAL.search(normalized(text_visible(c))) or re.search(r'24\s*(?:hours?|/\s*7)|appointment',text_visible(c),re.I) for c in node.elements):continue
            chosen=node;labelled=bool(LABEL.search(text))
            for _ in range(6):
                parent=chosen.parent
                if parent is None:break
                context=' '.join(text_visible(parent).split())
                if len(context)>1000:break
                position = context.find(text)
                labels = list(LABEL.finditer(context[:position] if position >= 0 else context))
                label = labels[-1] if labels else LABEL.search(text)
                if label:
                    # Keep the local label's purpose, without borrowing later Tour Areas text.
                    prefix = context[max(0,label.start()-40):label.start()] if labels else ''
                    qualifier = re.search(r'\b(tour|departure|emergency|office)[\w\s-]{0,24}$', prefix, re.I)
                    text=(qualifier.group(1)+' ' if qualifier else '')+label.group()+' '+text
                    chosen=parent;labelled=True;break
                chosen=parent
            if not time_context and not labelled:continue
            parsed=parse_week(text)
            if parsed['classification'] in {'parsed','parse_ambiguous'} and not labelled:
                continue
            key=(chosen.start if labelled else node.start,text)
            selected[key]=dict(raw=text,url=str(url),path=path,start=chosen.start if labelled else node.start,
                end=chosen.end if labelled else node.end,json_pointer=None,basis='text_pattern',**parsed)
        candidates.extend(selected.values())
        if len(candidates)>100:raise ValueError('candidate_limit')
    except (SiteBusinessError, ValueError, RecursionError):
        return [dict(raw='',url=str(url),path=path,basis='text_pattern',classification='other',purpose='unknown',days={},reason='resource_or_parse_limit')]
    unique = {}
    for row in candidates:
        unique.setdefault((row['url'], row['basis'], row['raw']), row)
    return list(unique.values())


def site_candidates(source, identity):
    if source is None or identity is None:return []
    limits=SiteBusinessLimits().model_dump();limits.update(max_html_bytes_per_page=4_000_000,max_html_bytes_total=10_000_000)
    budget=ParseBudget(SimpleNamespace(**limits));rows=[]
    for index, selected in enumerate(source.payload.selected_pages):
        deep=selected.deep_snapshot
        if deep is None or selected.page_type not in {'home','contact'}:continue
        path=f'/payload/selected_pages/{index}/deep_snapshot/html'
        if not in_scope(str(deep.final_url),SiteScope.from_root(str(identity.site_url))) or not 200<=deep.status_code<300:continue
        rows.extend(extract_page(deep.html,url=deep.final_url,path=path,identity=identity,budget=budget))
    return rows


def gbp_candidates(source):
    rows=[]
    if source is None:return rows
    for field in ('hours','operating_hours'):
        wrapper=getattr(source.payload.record.fields,field,None)
        if wrapper is None or wrapper.state!='observed':continue
        value=wrapper.value;parts=value if isinstance(value,list) else [value]
        text='\n'.join(f'{day}: {hours}' for part in parts for day,hours in part.items())
        rows.append(dict(raw=text,url=str(source.payload.record.observed_public_gbp_url or ''),
            path=f'/payload/record/fields/{field}/value',basis='structured',alias=field,**parse_week(text)))
    return rows


def compare_candidates(site, gbp, *, eligible=True):
    base=dict(state='not_checked',reason='field_not_observed',compared_days=[],omitted_days=list(DAYS))
    if not eligible:return dict(base,reason='source_ineligible')
    # Appointment conditions and ambiguous candidate schedules may not be silently dropped.
    if any(c['classification'] in {'parse_ambiguous','appointment_only'} or (c['classification']=='other' and c['reason'] not in {'hours_label_missing'}) for c in site):
        return dict(base,reason='hours_not_comparable')
    usable=[c for c in site if c['classification']=='parsed']
    if not usable or not gbp:return base
    if any(c['classification']!='parsed' for c in gbp):return dict(base,reason='hours_not_comparable')
    def union(rows):
        result={}
        for row in rows:
            for day,periods in row['days'].items():
                if day in result and result[day]!=periods:raise ValueError()
                result[day]=periods
        return result
    try:
        left,right=union(usable),union(gbp)
        if len({r['purpose'] for r in usable})>1:return dict(base,reason='hours_not_comparable')
    except ValueError:return dict(base,reason='conflicting_hours_sources')
    common=sorted(set(left)&set(right),key=int)
    if not common:return dict(base,reason='hours_not_comparable')
    mismatch=any(left[d]!=right[d] for d in common)
    return dict(state='mismatch' if mismatch else 'exact_match' if len(common)==7 else 'partial_match',
        reason='value_mismatch' if mismatch else 'exact_match' if len(common)==7 else 'partial_match',
        compared_days=[DAYS[int(d)] for d in common],omitted_days=[d for i,d in enumerate(DAYS) if str(i) not in common])


def inspect_hours(request):
    sources={s.kind:s for s in request.evidence_input.sources}
    site,gbp=sources.get('site'),sources.get('public_gbp')
    left,right=site_candidates(site,request.business_identity),gbp_candidates(gbp)
    eligible=bool(request.business_identity and site and gbp and all(eligibility(s,request.evidence_input.context.evaluated_at) is None for s in (site,gbp)))
    compared=compare_candidates(left,right,eligible=eligible)
    return dict(**compared,site_candidates=left,gbp_candidates=right,
        counts={k:sum(r['classification']==k for r in left+right) for k in CATEGORY_ORDER})


def observations(source, rows):
    from app.report_v22.evidence_adapters.common import observe
    from app.report_v22.models import SourceLocator
    for row in rows:
        if not row['raw']:continue
        key=['opening_hours_v1',row['url'],row['path'],str(row.get('start')) if row.get('start') is not None else None,row.get('json_pointer'),row['raw']]
        yield observe(source,'site_field' if source.kind=='site' else 'public_gbp_field',key,
            'opening_hours',row['raw'],row['path'],locator=SourceLocator(url=row['url'] or None),
            limitations=['Website structured data.' ] if source.kind=='site' and row['basis']=='structured' else [])


def outcome(view, audit):
    from app.report_v22.public_rule_catalog import HOURS
    from app.report_v22.findings_common import decision
    from app.report_v22.findings_models import RuleTarget
    site=view.sources.get('site')
    rows=audit['site_candidates']; usable=[r for r in rows if r['classification']=='parsed']
    urls=sorted({r['url'] for r in (usable or rows)})
    raw={r['raw'] for r in usable+audit['gbp_candidates']}
    ids=sorted(k for k,t in view.traces.items() if t.selector.record_context and t.selector.record_context[0]=='opening_hours_v1' and view.items[k].original_value in raw)
    audit['evidence_ids']=ids
    audit['basis']='structured' if usable and all(r['basis']=='structured' for r in usable) else 'text_pattern'
    target=RuleTarget(kind='customer_gbp',snapshot_id=site.binding.snapshot_id if site else None,urls=urls)
    state='triggered' if audit['state']=='mismatch' else 'not_checked' if audit['state']=='not_checked' else 'not_triggered'
    origin="website's structured data" if audit['basis']=='structured' else 'website'
    if usable and all(r['purpose']=='office' for r in usable): origin = "website's office-hours statement"
    statement=f"The {origin} and the public business profile publish different opening hours for {', '.join(audit['compared_days'])}. Please check both channels."
    return decision(view,HOURS,target,state,audit['reason'],evidence=ids,statement=statement,notes=[HOURS_NOTE])


def contact_check(audit):
    return dict(field='opening_hours',result={'mismatch':'mismatch','exact_match':'match','partial_match':'partial_match'}.get(audit['state'],'none'),
        status='not_checked' if audit['state']=='not_checked' else 'confirmed',basis=audit.get('basis','text_pattern'),
        compared_components=audit['compared_days'],omitted_components=audit['omitted_days'],evidence_ids=audit.get('evidence_ids',[]))
