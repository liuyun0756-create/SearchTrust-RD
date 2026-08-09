# GBP Discovery and Report Reliability Design

## Goal

SearchTrust may continue without a Google Business Profile, but it must never
use an unverified profile to drive L3 or GBP-to-page conclusions. Automatic
discovery therefore optimizes for precision rather than connection rate.

The same release also prevents a transient browser connection failure from
turning an active backend analysis into a failed report.

## Invariants

1. A missing or ambiguous GBP never blocks report generation.
2. An automatically discovered GBP is usable only after deterministic entity
   verification.
3. A user-supplied exact GBP is the requested comparison target. Page/GBP
   differences are audit evidence and must not cause that target to be replaced.
4. Search recall and candidate acceptance are separate stages.
5. Weak signals such as category, city, page title, domain, or logo text cannot
   independently identify a business.
6. Multi-location brands require a branch-specific location or phone anchor.
7. Only backend terminal states may mark a report complete or failed.

## Identity evidence

Page extraction produces a collection of name, phone, and locality signals
rather than choosing one name and one city immediately. Every signal records:

- field and raw value;
- source: JSON-LD, visible page content, metadata, image alt, link, page URL, or
  supporting page;
- scope: target page, same-site support, brand-wide, or branch-specific;
- quality: strong, supporting, or weak.

Page URL domain, extracted street address, and exact Maps identifiers remain
separate verification anchors. Candidate evaluation records their normalized
matches and conflicts alongside the signal evidence.

Generic theme/image labels, sentence fragments, service headings, third-party
credits, and shared brand-wide values remain available for diagnostics but are
not accepted as identity evidence.

## Discovery flow

1. Resolve an explicitly supplied exact Google identifier, if present.
2. Resolve an exact Google Maps link published by the checked site.
3. Build independent search queries from reliable domain, name, phone, and
   address evidence.
4. Gather and de-duplicate all returned candidates.
5. Evaluate every candidate against the same evidence set.
6. Accept one candidate only when it passes the confidence matrix and has a
   safe margin over every alternative.

No search query is itself evidence that a returned candidate is correct.

## Acceptance matrix

An automatically discovered candidate is accepted when either:

- one unique anchor (exact phone, full street address, or exact Maps ID) matches
  and at least one independent supporting signal matches; or
- at least three independent high-quality supporting signals match, no strong
  contradiction exists, and no competing candidate is similarly supported.

The following are never sufficient alone: similar name, same category, same
city, same root domain, logo alt, or first search position.

A domain mismatch is not a hard contradiction because a GBP may use an older or
alternate brand domain. Exact conflicting phone/address evidence and a wrong
branch locality are hard contradictions. Shared domains are never branch
anchors.

## GBP states and report behavior

- `checked` + source `system_discovered`: automatically discovered and verified.
- `checked` + source `user_provided`: requested exact profile successfully fetched.
- `not_found`: search completed with no viable candidates.
- `ambiguous`: candidates exist but no unique safe selection is possible.
- `error`: provider, network, or parsing failure.
- `not_checked`: no lookup ran.

Only `checked` may produce GBP comparison evidence, and its source records
whether the target was provided by the user or discovered by the system. Other
states produce a page-only report; GBP comparison rules are not assessed rather
than failed. Dify receives no GBP facts in those states.

## Frontend task recovery

The browser treats SSE as a notification channel, not as the authority for task
failure. On disconnect it keeps the report pending and polls the backend task.
A single request failure, 404, or empty result cannot close the task. The client
stops only on backend `done`, backend `failed`, or confirmed expiry.

Completed results are persisted idempotently. Reconnects and duplicate completion
events cannot save twice or deduct two credits. A GBP lookup state never becomes
a report-generation failure.

## Diagnostics

Logs record evidence sources, query plan, candidate match dimensions, rejection
reasons, ambiguity, and final decision. Raw provider keys are never logged.
Report-facing copy exposes only the state and a concise reason.

## Verification

The regression corpus covers single-store, alternate-domain, multi-location,
generic-logo, wrong-city, multiple-candidate, no-GBP, provider-error, long-running
task, SSE disconnect, repeated completion, and explicit backend failure cases.

Release acceptance prioritizes zero known false-positive GBP connections. A
lower automatic connection rate is acceptable.
