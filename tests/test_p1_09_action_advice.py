"""Offline regression for evidence-bound advice and frozen examples."""
from copy import deepcopy
from hashlib import sha256
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from app.jobs_v22.action_example_provider import ActionExampleProvider, fill_action_examples
from app.jobs_v22.capability_public_stage import CheckpointedCapabilityPublicStage
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.action_advice import (
    RULESET, _reference, apply_copy_result, approved_options, copy_options,
    prepare_action_advice_input, select_action_advice_ruleset,
)
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.local_entity_coverage import prepare_local_entity_input


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def pr_input():
    return example_gold_input()


def example_gold_input(site="all-star-plumbing-fresno"):
    root = Path(__file__).parent / "fixtures/gold" / site
    descriptor = json.loads((root / "input.json").read_text())["generation"]["input"]
    raw = (root / descriptor["path"]).read_bytes()
    assert sha256(raw).hexdigest() == descriptor["sha256"]
    data = json.loads(gzip.decompress(raw))
    manifest = AnalysisInputManifest.model_validate_json(canonical_json_bytes(data["manifest"]))
    request = PublicFindingsInput.model_validate_json(canonical_json_bytes(data["request"]))
    return prepare_action_advice_input(*prepare_local_entity_input(manifest, request))


def select_first_example(report):
    choices = copy_options(report)
    assert choices
    return apply_copy_result(report, choices, json.dumps({key: options[0].text for key, options in choices.items()}))


@pytest.fixture(scope="module")
def base_report(pr_input):
    return build_public_capability_report(*pr_input)


@pytest.mark.parametrize("text,reason", [
    ("Get in touch about plumbing. Rates start at $25 per hour.", "unsupported_copy"),
    ("Contact Smith Plumbing to discuss plumbing.", "unsupported_copy"),
    ("Contact us to discuss solar installation.", "unsupported_copy"),
    ("Guaranteed results with our plumbing.", "restricted_claim"),
    ("We can cure your condition.", "restricted_claim"),
    ("We will win your legal case.", "restricted_claim"),
    ("Rank #1 for plumbing with us.", "restricted_claim"),
    ("Get in touch about plumbing. Book immediately.", "unsupported_copy"),
    ("Our award-winning plumbing is top-quality.", "unsupported_copy"),
    ("Get in touch about plumbing. Save 10% by combining both offers.", "unsupported_copy"),
    ("Get in touch about plumbing\u200b.", "unsupported_copy"),
])
def test_rejects_unbound_numbers_entities_claims_and_extra_words(base_report, text, reason):
    options = copy_options(base_report)
    action_id = next(iter(options))
    report = apply_copy_result(base_report, options, json.dumps({action_id: text}))
    item = next(a for a in report.action_advice if a.action_id == action_id)
    assert item.example is None and item.rejection_reason == reason
    assert item.problem and item.evidence_ids and item.fix and item.verification
    assert report.findings == base_report.findings


def test_duplicate_titles_across_pages_do_not_publish_generic_service_copy():
    report = build_public_capability_report(*example_gold_input("pr-plumbing-fresno"))
    assert not copy_options(report)
    assert any(item.rejection_reason == "target_scope_unverified" for item in report.action_advice)


def test_structured_advice_must_match_the_existing_action_and_finding(base_report):
    checked = select_first_example(base_report)
    assert checked.schema_version == "2.2.15" and checked.input_manifest.ruleset_version == RULESET
    assert checked.input_manifest.schema_version == "v22_analysis_input_v10"
    item = next(item for item in checked.action_advice if item.example)
    assert item.example.evidence_ids == item.evidence_ids
    assert item.where and item.priority and item.effort == "small"
    raw = checked.model_dump(mode="json")
    for mutation in ("why_important", "example"):
        tampered = deepcopy(raw)
        entry = next(a for a in tampered["action_advice"] if a["example"])
        if mutation == "example":
            entry["example"]["text"] += " Guaranteed results."
        else:
            entry[mutation] = ["New unsupported causal statement"]
        with pytest.raises(ValueError):
            CapabilityReport.model_validate_json(canonical_json_bytes(tampered))


@pytest.mark.parametrize("url,body,expected", [
    ("https://example.test/proof", "We provide plumbing in Fresno.", True),
    ("https://example.test/service", "We provide plumbing in Fresno.", False),
    ("https://other.test/proof", "We provide plumbing in Fresno.", False),
    ("https://example.test/tag/proof", "We provide plumbing in Fresno.", False),
    ("https://example.test/proof", "Old text. We provide plumbing in Fresno", False),
    ("https://example.test/proof", "We provide plumbing in Oakland.", False),
    ("https://example.test/proof", "We guarantee plumbing in Fresno.", False),
])
def test_reuses_only_a_whole_quote_from_the_correct_bound_service_page(url, body, expected):
    snapshot = "00000000-0000-0000-0000-000000000111"
    proof = {"url": url, "location": "body", "snapshot_id": snapshot, "quote": body,
             "path": "/payload/selected_pages/0/deep_snapshot/text"}
    deep = SimpleNamespace(text="We provide plumbing in Fresno. Another sentence.", status_code=200,
                           content_type="text/html", url=url)
    page = SimpleNamespace(deep_analyzed=True, deep_snapshot=deep)
    site = SimpleNamespace(kind="site", binding=SimpleNamespace(snapshot_id=snapshot),
                           payload=SimpleNamespace(selected_pages=[page]))
    context = SimpleNamespace(target_market=SimpleNamespace(city="Fresno"))
    request = SimpleNamespace(evidence_input=SimpleNamespace(context=context, sources=[site]))
    raw = {"input_manifest": {"business": {"site_url": "https://example.test/", "primary_service": "plumbing"}},
           "landing_page_comparison": {"rows": [{"metrics": {"case_study": {"evidence": [proof]}}}]},
           "local_entity_coverage": {"evidence": [proof]}}
    result = _reference(raw, request, {"target": "https://example.test/service"})
    assert bool(result) is expected
    if result:
        assert result.text == body and result.url == url
    if expected:
        # Report evidence must also match the text at the declared page index.
        page.deep_snapshot.url = "https://example.test/elsewhere"
        assert _reference(raw, request, {"target": "https://example.test/service"}) is None


@pytest.mark.anyio
async def test_provider_timeout_keeps_structured_advice_and_is_not_retried(pr_input):
    provider = SimpleNamespace(choose=AsyncMock(side_effect=TimeoutError))
    async with FakeRedis() as redis:
        checks = JobCheckpoints(redis, prefix="p109-timeout", ttl_seconds=60)
        stage = CheckpointedCapabilityPublicStage(provider)
        manifest, request = pr_input
        first = await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checks)
        replay = await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checks)
    assert provider.choose.await_count == 1
    assert canonical_json_bytes(first) == canonical_json_bytes(replay)
    assert any(item.rejection_reason == "timeout" and item.fix for item in first.action_advice)


@pytest.mark.anyio
async def test_copy_result_is_frozen_and_replayed_without_another_call(pr_input):
    async def choose(menu):
        return {key: texts[-1] for key, texts in menu.items()}

    provider = SimpleNamespace(choose=AsyncMock(side_effect=choose))
    async with FakeRedis() as redis:
        checks = JobCheckpoints(redis, prefix="p109-success", ttl_seconds=60)
        stage = CheckpointedCapabilityPublicStage(provider)
        manifest, request = pr_input
        first = await stage.build(job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checks)
        replay = await CheckpointedCapabilityPublicStage().build(
            job_id=manifest.job_id, manifest=manifest, request=request, checkpoints=checks)
    assert provider.choose.await_count == 1
    assert canonical_json_bytes(first) == canonical_json_bytes(replay)
    assert any(item.example for item in first.action_advice)
    menu = provider.choose.call_args.args[0]
    assert all(key.isdecimal() for key in menu)
    assert not any("/" in option for texts in menu.values() for option in texts)


@pytest.mark.anyio
async def test_prior_attempt_cannot_replace_report(base_report):
    provider = SimpleNamespace(choose=AsyncMock())
    blocked = await fill_action_examples(
        base_report, provider, claim_attempt=AsyncMock(return_value=False), job_id=base_report.input_manifest.job_id,
    )
    provider.choose.assert_not_called()
    assert blocked == base_report


@pytest.mark.anyio
async def test_json_fenced_provider_copy_passes_the_same_evidence_check(base_report, monkeypatch):
    async def send(client, request, **kwargs):
        menu = json.loads(json.loads(request.content)["messages"][-1]["content"])
        content = json.dumps({key: options[0] for key, options in menu.items()})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"\n```json\n{content}\n```\n"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    provider = ActionExampleProvider("example-secret", api_url="https://provider.example/v1", model="mock-model")
    report = await fill_action_examples(
        base_report, provider, claim_attempt=AsyncMock(return_value=True), job_id=base_report.input_manifest.job_id,
    )
    assert report == select_first_example(base_report)


@pytest.mark.anyio
@pytest.mark.parametrize("status,body,stage,code,reason", [
    (401, {"error": {"code": "invalid_api_key", "message": "private-response"}}, "http",
     "invalid_api_key", "provider_unavailable"),
    (402, {"error": {"code": "secret-test-key"}}, "http", None, "provider_unavailable"),
    (429, "private-response", "http", None, "provider_unavailable"),
    (None, None, "network", None, "provider_unavailable"),
    (200, "private-response", "json", None, "invalid_response"),
    (200, {}, "schema", None, "invalid_response"),
    (200, {"choices": [{"message": {"content": "```json\nprivate-response\n```"}}]}, "json", None,
     "invalid_response"),
    (200, {"choices": [{"message": {"content": '{"0":"private-response"}'}}]}, "schema", None,
     "invalid_response"),
    (200, {"choices": [{"message": {"content": '{"1":"x","1":"y"}'}}]}, "schema", None, "invalid_response"),
])
async def test_provider_failures_keep_advice_and_warn_without_payload(
    base_report, monkeypatch, caplog, status, body, stage, code, reason,
):
    async def send(client, request, **kwargs):
        assert request.headers["Authorization"] == "Bearer secret-test-key"
        if status is None:
            raise httpx.ConnectError("private-response secret-test-key", request=request)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    provider = ActionExampleProvider("secret-test-key", api_url="https://provider.example/v1", model="mock-model")
    job_id = base_report.input_manifest.job_id
    report = await fill_action_examples(
        base_report, provider, claim_attempt=AsyncMock(return_value=True), job_id=job_id,
    )
    assert report.actions == base_report.actions
    assert report.findings == base_report.findings
    assert any(item.rejection_reason == reason and item.fix for item in report.action_advice)
    assert all(item.example is None for item in report.action_advice)
    warnings = [record for record in caplog.records if record.msg.startswith("action_example_provider_failed ")]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"
    logged_stage, http_status, provider_code, job_id_suffix, elapsed_ms = warnings[0].args
    assert (logged_stage, http_status, provider_code, job_id_suffix) == (stage, status, code, str(job_id)[-8:])
    assert isinstance(elapsed_ms, int) and elapsed_ms >= 0
    for sensitive in ("secret-test-key", "private-response", "provider.example", "Choose one supplied", str(job_id)):
        assert sensitive not in caplog.text


def test_v19_is_reserved_without_selecting_or_loosening_its_contract(base_report, caplog):
    assert not approved_options("24 hour plumbing")
    assert select_action_advice_ruleset("v22_capability_public_v18", False) == "v22_capability_public_v18"
    assert select_action_advice_ruleset("v22_capability_public_v18", True) == RULESET
    assert select_action_advice_ruleset("v22_capability_public_v17", True) == "v22_capability_public_v17"
    assert "requires GBP" in caplog.text
    raw = base_report.model_dump(mode="json")
    raw["input_manifest"].update(ruleset_version="v22_capability_public_v19", schema_version="v22_analysis_input_v9")
    raw["schema_version"] = "2.2.14"
    with pytest.raises(ValueError):
        CapabilityReport.model_validate_json(canonical_json_bytes(raw))
