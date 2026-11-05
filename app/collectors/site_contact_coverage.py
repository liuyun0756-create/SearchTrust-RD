"""v11-only contact discovery and budget-preserving selection (no paid clients)."""
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from app.collectors.site_inventory_selection import RankedPage
from app.collectors.site_inventory_urls import canonicalize_inventory_url
from app.collectors.site_page_scope import is_admin_page

CONTACT = re.compile(r"^(contact(?:[ -]us)?|get[ -]in[ -]touch)$", re.I)


def contact_path(url):
    segment = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
    return bool(CONTACT.fullmatch(re.sub(r"\.html?$", "", segment, flags=re.I)))


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []; self.href = None; self.text = []; self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'template', 'noscript'}: self.hidden += 1
        if tag == 'a' and not self.hidden:
            self.href = dict(attrs).get('href'); self.text = []
    def handle_data(self, data):
        if self.href and not self.hidden: self.text.append(data)
    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            self.links.append((self.href, ' '.join(' '.join(self.text).split())))
            self.href = None
        if tag in {'script', 'style', 'template', 'noscript'}: self.hidden = max(0, self.hidden - 1)


def discover_contacts(html, scope):
    parser = Links(); parser.feed(html)
    found = {}; all_urls = set()
    for href, label in parser.links:
        url = canonicalize_inventory_url(href, base_url=scope.root_url, scope=scope)
        if not url or is_admin_page(url) or href.startswith(('#', 'mailto:', 'tel:')): continue
        all_urls.add(url)
        if url == scope.root_url: continue
        named, path = bool(CONTACT.fullmatch(label)), contact_path(url)
        if named or path:
            found[url] = min(found.get(url, (9, 9, 99, url)), (not named, not path, urlsplit(url).path.count('/'), url))
    return sorted(found, key=found.get), all_urls


def confirmed_contact(structure):
    return ('ContactPage' in structure.schema_types or
            bool(re.search(r'\b(contact(?: us)?|get in touch)\b', ' '.join(filter(None, (structure.title, structure.h1))), re.I)))


def prioritize_contacts(ranked, pages, urls):
    home = [r for r in ranked if r.page.page_type == 'home']
    if any('ContactPage' in r.page.schema_types for r in home): return ranked
    contacts = [p for p in pages if str(p.url) in urls and p.page_type == 'contact' and (p.status_code or 500) < 400]
    reserved = [RankedPage(p, 9999.0, ('contact_reserved',)) for p in sorted(contacts, key=lambda p: urls.index(str(p.url)))[:2]]
    chosen = {str(r.page.url) for r in home + reserved}
    return home + reserved + [r for r in ranked if str(r.page.url) not in chosen]


def home_contact_complete(html, scope):
    from app.collectors.site_inventory_html import extract_html_structure
    structure = extract_html_structure(html, page_url=scope.root_url, scope=scope)
    return ('ContactPage' in structure.schema_types
            and bool(re.search(r'tel:|mailto:', html, re.I))
            and bool(re.search(r'\b(?:address|hours)\b', structure.visible_text, re.I)))
