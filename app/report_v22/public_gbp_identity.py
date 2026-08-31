"""Compare an independent confirmed reference, never contact text equality."""
from urllib.parse import urlsplit

from app.report_v22.public_gbp_errors import PublicGbpError


def _host(url):
    # Same exact-domain policy as the existing evidence binding boundary.
    return (urlsplit(str(url)).hostname or "").casefold().removeprefix("www.")


def require_request_reference(reference, target):
    if reference.public_gbp_url != target.public_gbp_url or reference.entity_keys != target.entity_keys:
        raise PublicGbpError("REFERENCE_INVALID")


def assess_identity(reference, sample):
    require_request_reference(reference, sample.request_target)
    if sample.collection_status == "failed":
        return "needs_confirmation", ["lookup_failed"]
    expected = {key.kind: key.value for key in reference.entity_keys}
    observed = {key.kind: key.value for key in sample.record.observed_entity_keys}
    common = expected.keys() & observed.keys()
    if any(expected[key] != observed[key] for key in common):
        return "mismatch", ["strong_id_conflict"]
    if not common:
        return "needs_confirmation", ["no_comparable_strong_id"]
    website = sample.record.fields.website_url
    if website.state == "observed" and _host(website.value) != _host(reference.site_url):
        return "mismatch", ["website_domain_conflict"]
    reasons = ["strong_id_matched"]
    if website.state != "observed":
        reasons.append("website_not_observed")
    return "matched", sorted(reasons)
