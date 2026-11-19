"""Evidence-bound action advice and closed-vocabulary copy validation."""
from dataclasses import dataclass
import json
import logging
import re
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID

from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest
from app.report_v22.action_advice_models import ActionAdvice, AdviceExample, AdviceReference
from app.report_v22.local_entity_coverage import RULESET as LOCAL_ENTITY_RULESET, archive_url

RULESET = "v22_capability_public_v20"
MANIFEST_VERSION = "v22_analysis_input_v10"
REPORT_VERSION = "2.2.15"
COPY_KINDS = frozenset({"title_brief", "service_brief"})
EFFORT = {"title_brief": "small", "service_brief": "large", "profile_correction": "small",
          "technical_check": "medium", "measurement_check": "medium"}
CONFIDENCE = {"low": 0, "medium": 1, "high": 2}
TEMPLATES = {"contact": "Get in touch about {service}.",
             "discuss": "Contact us to discuss {service}."}
PROHIBITED = re.compile(
    r"\b(?:cures?|curing|cured|heal(?:s|ed|ing)?|guarantee(?:s|d|ing)?|"
    r"guarant(?:y|ies|ied)|promise(?:s|d)?|assured|risk[ -]free|top[ -]ranked|"
    r"(?:eliminat|revers|reliev)(?:e|es|ed|ing)\s+(?:your\s+)?(?:pain|symptoms|disease)|"
    r"rank(?:s|ed|ing)?\s*(?:#\s*1|first|number\s+one)|"
    r"winn?(?:s|ing)?\s+(?:your|the|every|any)\s+(?:legal\s+)?case|"
    r"(?:will|always)\s+(?:rank|win|cure|heal|deliver\s+results)|100\s*%\s+(?:effective|success))\b"
    r"|包治|治愈|保证|保證|包赢|包贏|必胜|必勝|排名第一",
    re.I,
)
INTERNAL = re.compile(r"v22_|fn_[a-f0-9]|rank_source|https?://|"
                      r"/payload/|\b(?:SERP|CAPABILITY|V22)_[A-Z_]+\b|[-+]?\d{1,3}\.\d{3,}\s*,\s*[-+]?\d{1,3}\.\d{3,}", re.I)
IDENTITY_SLOT = re.compile(r"^[^\W_][\w '&’(),.\-]{0,119}$", re.UNICODE)
SENTENCE_BREAK = re.compile(r"[.!?]\s|[!?]")
WORD = re.compile(r"[^\W_]+", re.UNICODE)
logger = logging.getLogger(__name__)


def select_action_advice_ruleset(previous: str | None, enabled: bool) -> str | None:
    if enabled and previous == LOCAL_ENTITY_RULESET:
        return RULESET
    if enabled:
        logger.warning("V22_ACTION_EXAMPLE_COPY_ENABLED requires GBP, LANDING, REASONS and LOCAL_ENTITY.",
                       extra={"configuration_warning": "action_advice_requires_local_entity"})
    return previous


def prepare_action_advice_input(manifest, request):
    from app.report_v22.capability import AnalysisInputManifest

    if manifest.ruleset_version != LOCAL_ENTITY_RULESET:
        raise ValueError("v20 requires a bound v18 input")
    if manifest.generation_input_checksum != verified_request_digest(request.model_dump(mode="json")):
        raise ValueError("v20 generation input does not match v18")
    raw = manifest.model_dump(mode="json")
    raw.update(schema_version=MANIFEST_VERSION, ruleset_version=RULESET)
    raw["input_checksum"] = verified_request_digest({key: value for key, value in raw.items() if key != "input_checksum"})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(raw)), request


def safe_copy(text: str) -> bool:
    checked = unicodedata.normalize("NFKC", text)
    return (not any(unicodedata.category(character).startswith("C") for character in text)
            and not INTERNAL.search(checked))


def restricted(text: str) -> bool:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[_\-–—]+", " ", text)
    return bool(PROHIBITED.search(text))


def normalized_words(text: str) -> list[str]:
    return WORD.findall(unicodedata.normalize("NFC", text).casefold())


def contains_phrase(text: str, phrase: str) -> bool:
    words, match = normalized_words(text), normalized_words(phrase)
    return bool(match) and any(words[index:index + len(match)] == match for index in range(len(words)))


def _reference(raw, request, action):
    city = request.evidence_input.context.target_market.city
    scope = raw["input_manifest"]["business"]
    service = scope["primary_service"]
    host = urlsplit(scope["site_url"]).hostname
    if not city or not host:
        return None
    site = next((source for source in request.evidence_input.sources if source.kind == "site"), None)
    if site is None:
        return None
    def page_key(url):
        parts = urlsplit(url)
        return ((parts.hostname or "").removeprefix("www."), parts.path.rstrip("/") or "/")
    targets = {page_key(target) for target in action["target"].splitlines()}
    for proof in sorted(
        (entry for metric in raw["landing_page_comparison"]["rows"][0]["metrics"].values()
         for entry in metric["evidence"]),
        key=lambda entry: (entry["url"], entry["path"], entry["quote"]),
    ):
        url, quote, path = proof["url"], proof["quote"], proof["path"]
        parts = urlsplit(url)
        if (parts.username is not None or parts.password is not None
                or proof["location"] != "body" or not path.endswith("/text")
                or page_key(url)[0] != host.removeprefix("www.")
                or page_key(url) in targets or archive_url(url)
                or str(proof["snapshot_id"]) != str(site.binding.snapshot_id)
                or not safe_copy(quote) or restricted(quote)
                or not contains_phrase(quote, service) or not contains_phrase(quote, city)):
            continue
        page_index = int(path.split("/")[3])
        page = site.payload.selected_pages[page_index]
        deep = page.deep_snapshot
        # A whole saved sentence with both scope terms is needed for verbatim reuse.
        if (not deep or not page.deep_analyzed or not 200 <= deep.status_code < 300 or str(deep.url) != url
                or deep.content_type.split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}):
            continue
        if quote not in re.split(r"(?<=[.!?])\s+|[\r\n]+", deep.text):
            continue
        city_evidence = raw["local_entity_coverage"]["evidence"]
        if not any(entry["url"] == url and entry["location"] == "body" for entry in city_evidence):
            continue
        return AdviceReference(text=quote, url=url, path=path, snapshot_id=UUID(str(proof["snapshot_id"])))
    return None


def copy_target_verified(target, site_url, rule_ids):
    # A duplicate-title finding can span unrelated pages. A title-review
    # finding binds one customer page to the confirmed primary service.
    if "v22_capability.service_title_review" not in rule_ids or any(character.isspace() for character in target):
        return False
    candidate = urlsplit(target)
    site = urlsplit(site_url)
    return (candidate.scheme in {"http", "https"}
            and candidate.username is None and candidate.password is None
            and (candidate.hostname or "").removeprefix("www.") == (site.hostname or "").removeprefix("www.")
            and not archive_url(target))


def initial_copy_rejection(eligible, kind, target, site_url):
    if not eligible or kind not in COPY_KINDS:
        return "no_approved_copy"
    if not copy_target_verified(target, site_url, {f["rule_id"] for f in eligible}):
        return "target_scope_unverified"
    return "not_attempted"


def build_action_advice(raw, request):
    findings = {item["id"]: item for item in raw["findings"]}
    system_order = {item["id"]: index for index, item in enumerate(raw["actions"], start=1)}
    advice = []
    for action in raw["action_candidates"]:
        basis = [findings[key] for key in action["finding_ids"]]
        eligible = [finding for finding in basis if finding["status"] == "confirmed" and finding["confidence"] != "low"]
        used = eligible or basis
        evidence = list(dict.fromkeys(key for finding in used for key in finding["evidence_ids"]))[:50]
        kind = action["materials"][0]["kind"] if action["materials"] else None
        rejection = initial_copy_rejection(eligible, kind, action["target"], raw["input_manifest"]["business"]["site_url"])
        reference = _reference(raw, request, action) if rejection == "not_attempted" else None
        advice.append(ActionAdvice(
            action_id=action["id"], problem=list(dict.fromkeys(f["observed_fact"] for f in used)),
            evidence_ids=evidence, why_important=list(dict.fromkeys(f["impact_path"] for f in used)),
            fix=["Reuse the cited passage on the proposed page.", *action["steps"][:11]] if reference else action["steps"],
            where=action["target"], priority=system_order.get(action["id"]), effort=EFFORT.get(kind, "not_assessed"),
            confidence=min((f["confidence"] for f in used), key=CONFIDENCE.__getitem__),
            verification=action["completion"], reference=reference,
            rejection_reason=None if reference else rejection,
        ).model_dump(mode="json"))
    return advice


@dataclass(frozen=True)
class CopyOption:
    template: str
    text: str


def approved_options(service):
    # Use the confirmed primary service. Do not introduce standalone numbers,
    # locations, competitors, names, business claims or performance promises.
    if (not IDENTITY_SLOT.fullmatch(service) or SENTENCE_BREAK.search(service)
            or any(character.isnumeric() for character in service)
            or not safe_copy(service) or restricted(service)):
        return ()
    return tuple(CopyOption(key, template.format(service=service)) for key, template in TEMPLATES.items())


def copy_options(report):
    options = approved_options(report.input_manifest.business.primary_service)
    if not options:
        return {}
    actions = {action.id for action in report.actions}
    return {item.action_id: options for item in report.action_advice
            if item.action_id in actions and item.rejection_reason == "not_attempted"}


def validate_example(candidate, options, evidence_ids):
    if not isinstance(candidate, str) or not safe_copy(candidate):
        return None, "unsupported_copy"
    if restricted(candidate):
        return None, "restricted_claim"
    for option in options:
        if candidate == option.text:
            return AdviceExample(text=candidate, template=option.template, evidence_ids=evidence_ids), None
    # Exact reconstruction covers every word, number, unit, entity and assertion.
    return None, "unsupported_copy"


def apply_copy_result(report, options, response=None, unavailable=None):
    from app.report_v22.capability import CapabilityReport
    if not options:
        return report
    if unavailable is None:
        try:
            def unique_pairs(pairs):
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise ValueError("duplicate response key")
                return result
            data = json.loads(response, object_pairs_hook=unique_pairs)
            if not isinstance(data, dict) or set(data) - options.keys():
                raise ValueError("unexpected action key")
        except (ValueError, TypeError):
            data, unavailable = {}, "invalid_response"
    else:
        data = {}
    raw = report.model_dump(mode="json")
    for advice in raw["action_advice"]:
        key = advice["action_id"]
        if key not in options:
            continue
        example, issue = (None, unavailable) if unavailable else validate_example(data.get(key), options[key], advice["evidence_ids"])
        advice["example"] = example.model_dump(mode="json") if example else None
        advice["rejection_reason"] = issue
    return CapabilityReport.model_validate_json(canonical_json_bytes(raw))
