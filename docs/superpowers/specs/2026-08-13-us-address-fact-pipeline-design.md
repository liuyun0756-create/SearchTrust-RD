# US Address Fact Pipeline Design

Date: 2026-08-13
Status: Approved design, pending implementation plan

## Context

The current page fact extractor identifies addresses primarily with broad text
regular expressions and then selects candidates by source priority. This has
two recurring failure modes:

1. Ordinary prose can resemble an address. For example, `20 minutes ... the
   cheapest way` can be interpreted as a house number followed by a street
   ending in `Way`.
2. A real address split across DOM nodes can be reduced to a partial street
   line. For example, `5855 E Clinton Ave.` and `Fresno, CA 93727` are one
   address but may be extracted as separate text lines.

Adding phrase-specific exclusions does not address the underlying problem. The
system needs a source-aware candidate pipeline and component-level validation.

## Goals

- Build one authoritative set of target-page address facts for both L2 and L3.
- Prefer explicit page structure over unbounded text matching.
- Parse US address candidates into components before accepting them.
- Preserve the page's exact displayed value for L3 comparison and evidence.
- Retain provenance and deterministic rejection reasons for every candidate.
- Support multiple legitimate addresses on one page without using GBP to pick
  a preferred value.
- Handle non-US addresses conservatively.

## Non-goals

- Geocoding or verifying that a physical location exists.
- Calling Google Maps, another geocoder, or an LLM to decide address validity.
- Normalizing page address text into the GBP spelling or punctuation.
- Relaxing the existing L3 strict comparison behavior.
- Selecting a page address by measuring its similarity to GBP.

## Confirmed Product Rules

1. The product primarily audits US businesses and US addresses.
2. Non-US free text is not inferred. A non-US address is accepted only from an
   explicit structured or labeled source.
3. An address present only inside a map URL is not a page address. The map URL
   may associate visible nodes, but hidden URL parameters never become the
   displayed address value.
4. If a page exposes multiple valid addresses, all are retained.
5. L2 asks only whether at least one sufficiently complete address exists.
6. L3 receives the same page facts as L2 and compares their raw values strictly
   with GBP. It is not changed by this work.

## Architecture

Address extraction is split into three focused modules:

### `address_candidates.py`

Produces provenance-preserving candidates from the target page. It does not
decide whether a candidate is a valid US address.

Candidate sources, in evidence-order preference:

1. JSON-LD `PostalAddress` and equivalent microdata components.
2. Semantic `<address>` elements.
3. A visible DOM container labeled `Address`, `Location`, or `Located at`.
4. Visible text grouped by a shared map destination or shared map container.
5. Bounded visible-text candidates as a last-resort discovery source.

DOM grouping is based on container relationships, labels, and shared link
targets. It does not concatenate arbitrary neighboring text across sections.

### `us_address_parser.py`

Provides a small internal parsing interface and hides the concrete parser
dependency. The initial implementation uses `usaddress` to label US address
components. `usaddress` is a parser, not a validator, so its output never
becomes a fact without deterministic validation.

Expected interface:

```python
parse_us_address(raw_value: str) -> ParsedAddress
```

The parsed representation includes:

- `house_number`
- `street_predirectional`
- `street_name`
- `street_suffix`
- `unit`
- `city`
- `state`
- `postal_code`
- parser status and ambiguity information

`libpostal` is not selected for this phase because its compiled C runtime and
large model footprint are disproportionate for the current Python slim Worker
and US-first scope.

### `address_facts.py`

Validates parsed candidates, groups equivalent physical-address components,
retains raw variants, and produces accepted and rejected observations.

This module is the only authority that may mark an address observation as
eligible for L2 and L3.

## Data Model

Each candidate and accepted observation uses an auditable record:

```json
{
  "raw_value": "5855 E Clinton Ave., Fresno, CA 93727",
  "normalized_value": "5855 e clinton ave., fresno, ca 93727",
  "components": {
    "house_number": "5855",
    "street": "E Clinton Ave.",
    "unit": null,
    "city": "Fresno",
    "state": "CA",
    "postal_code": "93727"
  },
  "country_code": "US",
  "source_type": "page.dom.labeled_address_block",
  "source_url": "https://www.artdouglasplumbing.com/drain-cleaning",
  "locator": "footer > address-column",
  "excerpt": "Address: | 5855 E Clinton Ave. | Fresno, CA 93727",
  "visibility": "visible",
  "validation": "valid",
  "completeness": "full",
  "eligible_for_l2": true,
  "eligible_for_l3": true,
  "rejection_reason": null
}
```

`page_facts.addresses` remains an array of raw page values for compatibility.
Accepted structured observations remain in
`page_facts.observations.addresses`. All candidates and rejected observations
remain available in their existing audit ledgers.

## US Validation Rules

Candidate discovery is permissive; fact acceptance is strict.

A US address is L2-complete when it contains:

- a house number;
- a street name and recognized street type;
- a city; and
- a two-letter US state or District of Columbia code.

A ZIP Code improves completeness and is retained when present, but it is not
required for L2 presence. A page without a ZIP may still expose a clear
physical address. L3 compares the available address components semantically,
so an omitted ZIP is compatible when the shared location components agree.

Additional rules:

- Parser output must be a street address, not an intersection, recipient,
  landmark, time expression, measurement, or ordinary sentence.
- Parsed state values must be valid US postal abbreviations.
- A visible-text fallback candidate must pass the same component requirements
  as a structured candidate.
- Structured sources do not bypass component validation; they only provide
  stronger provenance and clearer component boundaries.
- Parser ambiguity fails closed and is recorded rather than guessed.

## Non-US Handling

Non-US candidates are accepted only when all of the following are true:

- the source is JSON-LD/microdata, `<address>`, or an explicitly labeled visible
  address container;
- a country is explicit or the structured shape is unambiguously postal;
- the value is not derived solely from a map URL;
- the original value and source provenance are preserved.

Ordinary non-US prose is retained only as a rejected or unassessed candidate.

## Candidate Grouping and Selection

The current global "highest source priority wins" behavior is replaced for the
address field with address-specific selection.

1. Validate every candidate independently.
2. Group candidates representing the same parsed physical components.
3. Retain all distinct raw variants inside the group.
4. Retain all valid groups when the page exposes multiple locations.
5. Use source strength only to order evidence, not to erase another valid fact.

If the same physical address appears visibly as both `Ave.` and `Ave`, both raw
variants remain auditable. The extractor does not choose the variant closest to
GBP.

## L2 and L3 Behavior

### L2 Entity Presence

Rule 22 reads the accepted observations from the shared `page_facts` object.
It does not rescan page text and does not receive an L2-only fact source.

- At least one L2-complete address: Rule 22 does not trigger.
- No L2-complete address: Rule 22 triggers.
- Partial candidates remain evidence but cannot satisfy presence.

### L3 Entity Consistency

Rules 26-29 continue to receive the same `page_facts` object. The L3 address
comparison implementation is outside the scope of this change and remains
strict.

Examples:

- Page `5855 E Clinton Ave., Fresno, CA 93727`
- GBP `5855 E Clinton Ave, Fresno, CA 93727`
- Result: mismatch because the raw punctuation differs.

Component parsing validates the field type; it does not relax equality.

## Failure Handling

Rejected observations use bounded reason codes, including:

- `not_street_address`
- `missing_house_number`
- `missing_street`
- `missing_city`
- `missing_state`
- `invalid_state`
- `ambiguous_parse`
- `hidden_map_url_only`
- `unsupported_non_us_free_text`
- `cross_container_join_rejected`

If the US parser dependency is unavailable at runtime:

- complete structured components may still pass deterministic validation;
- ordinary visible-text candidates fail closed;
- the report records a parser diagnostic;
- the system must not silently return to the broad legacy regex as an address
  authority.

## Evidence Presentation

- The page observation displays one complete raw address per row.
- Fragment candidates are not concatenated by the UI.
- The source label identifies JSON-LD, semantic address, labeled address block,
  or map-associated visible block.
- Rejected prose never appears as an accepted L2/L3 address.
- Diagnostics remain available in the report audit payload without cluttering
  the client-facing summary.

## Testing Strategy

Unit and integration fixtures cover:

1. Spot On Plumbing: street and locality nodes share a map target and form one
   complete address.
2. Art Douglas Plumbing: `Address:` plus street and locality lines form one
   complete address.
3. `20 minutes ... cheapest way`: rejected as ordinary prose.
4. Complete single-line US addresses.
5. Suite, unit, directional, ordinal street, and ZIP+4 formats.
6. Multiple legitimate location addresses on one page.
7. Same physical address with different visible punctuation variants.
8. Map URL with `Directions` text but no visible address.
9. JSON-LD and visible values that agree, differ, or are partial.
10. Non-US structured addresses and rejected non-US free text.
11. Parser ambiguity and parser-unavailable behavior.
12. L2 presence using accepted components.
13. L3 exact, punctuation/abbreviation-compatible, and materially conflicting
    addresses using the same facts.

L3 reports only fields with comparable values in both sources as assessed.
Zero comparable fields produce `not_checked`; compatible omissions such as a
Suite/Unit appearing in only one source remain visible as a non-scoring
`partial` advisory.

The entire existing report suite must pass. Regression assertions protect the
L3 evaluator's component-aware semantic matching from incidental changes.

## Rollout

1. Add the parser abstraction and dependency in an isolated change.
2. Add candidate extractors and address-specific validation behind the existing
   `build_page_facts` interface.
3. Replace the current address candidate implementation rather than layering
   more regex patches on top of it.
4. Run the real-page fixtures and complete backend suite.
5. Deploy to the test branch and rerun affected audit URLs.
6. Compare accepted/rejected address diagnostics before promoting further.

No database schema migration is required because the existing page fact arrays
and observation ledgers can carry the new structured metadata.

## Success Criteria

- L2 and L3 consume exactly the same accepted page address facts.
- Art Douglas produces `5855 E Clinton Ave., Fresno, CA 93727` and no prose
  address candidate.
- Spot On produces its complete split-node address.
- Hidden map URL parameters never become page address facts.
- Multiple real locations remain present without GBP-guided selection.
- Every rejected candidate has a deterministic reason.
- L3 strict comparison remains unchanged and covered by regression tests.

## References

- [DataMade usaddress](https://github.com/datamade/usaddress)
- [OpenVenues libpostal](https://github.com/openvenues/libpostal)
