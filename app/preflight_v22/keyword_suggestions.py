"""Suggestions from the current free preflight only, never provider/rank based."""
from html.parser import HTMLParser
import re
from app.competitors_v22.normalization import normalize_domain
from app.preflight_v22.candidates import _phones_match


class _Headings(HTMLParser):
    def __init__(self):
        super().__init__(); self.tag=None; self.parts=[]; self.values=[]
    def handle_starttag(self,tag,attrs):
        if tag in {'title','h1'}: self.tag=tag; self.parts=[]
    def handle_data(self,text):
        if self.tag: self.parts.append(text)
    def handle_endtag(self,tag):
        if self.tag==tag:
            self.values.append((tag,' '.join(' '.join(self.parts).split()))); self.tag=None


def saved_keyword_suggestions(html,signals,gbp,site_url):
    parser=_Headings(); parser.feed(html or '')
    items=[]; seen=set()
    for profile in gbp.candidates:
        if (normalize_domain(profile.website_url)==normalize_domain(site_url)
                or any(_phones_match(p.value,profile.phone) for p in signals.phones)):
            for category in profile.categories:
                if category.strip() and len(category)<=300:
                    items.append(dict(text=category.strip(),source='matched_gbp_category',ref='gbp:category',profile_url=profile.public_gbp_url))
    # A title/H1 is not automatically a service. Require an explicit structured
    # service phrase to actually appear there, and preserve its displayed spelling.
    for tag,text in parser.values:
        for signal in signals.services:
            match=re.search(re.escape(signal.value),text,re.IGNORECASE)
            if match and 2<len(match.group())<=300:
                items.append(dict(text=match.group(),source='site_'+tag,ref='site:'+tag,profile_url=None))
    result=[]
    for item in items:
        key=(item['text'].casefold(),item['source'],item['profile_url'])
        if key not in seen: seen.add(key); result.append(item)
    return dict(schema_version='v22_keyword_suggestions_v1',items=result[:30])
