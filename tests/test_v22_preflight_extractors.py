from pathlib import Path

from app.preflight_v22.extractors import extract_site_signals


FIXTURES = Path(__file__).parent / "fixtures" / "v22_preflight"


def test_extract_site_signals_prioritizes_structured_business_facts() -> None:
    signals = extract_site_signals((FIXTURES / "homepage_complete.html").read_text())

    assert signals.names[0].value == "Acme Plumbing"
    assert signals.names[0].source == "json_ld"
    assert signals.names[0].confidence == "high"
    assert signals.phones[0].value == "+1-512-555-0100"
    assert signals.services[0].value == "Residential Plumbing"
    assert signals.markets[0].country_code == "US"
    assert signals.markets[0].city == "Austin"
    assert signals.markets[0].region == "TX"
    assert signals.operating_models[0].value == "hybrid"
    assert signals.gbp_url and signals.gbp_url.startswith("https://www.google.com/maps/")


def test_extract_site_signals_does_not_invent_market_or_operating_model() -> None:
    signals = extract_site_signals((FIXTURES / "homepage_ambiguous.html").read_text())

    assert signals.markets == ()
    assert signals.operating_models == ()
    assert all(signal.confidence != "high" for signal in signals.names)


def test_extract_site_signals_deduplicates_same_name_from_multiple_sources() -> None:
    signals = extract_site_signals((FIXTURES / "homepage_complete.html").read_text())

    assert [signal.value for signal in signals.names].count("Acme Plumbing") == 1


def test_extract_site_signals_ignores_contract_oversized_structured_fields() -> None:
    document = f'''<script type="application/ld+json">{{
      "@type": "LocalBusiness",
      "name": "{'x' * 241}",
      "address": {{
        "addressLocality": "{'y' * 121}",
        "addressCountry": "US"
      }}
    }}</script>'''

    signals = extract_site_signals(document)

    assert signals.names == ()
    assert signals.markets == ()


def test_extract_site_signals_retains_visible_identity_without_structured_data() -> None:
    document = '''
      <meta property="og:site_name" content="Acme Plumbing">
      <a href="tel:+15125550100">Call +1 (512) 555-0100</a>
    '''

    signals = extract_site_signals(document)

    assert signals.names[0].value == "Acme Plumbing"
    assert signals.names[0].confidence == "medium"
    assert signals.phones[0].source == "visible_phone"
