from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
from hypothesis import given, settings, strategies as st
from pydantic import ValidationError

from app.integrations.serpapi import execute_serpapi_get
from app.jobs_v22.digest import canonical_json_bytes
from app.report_v22.execution_plan import build_execution_plan
from app.report_v22.findings import build_public_findings
from app.report_v22.models import ReportV22
from execution_plan_helpers import execution_plan_request
from property.strategies import SAFE_TEXT, URLS, evidence_graphs
from version_diff_helpers import parent_report


REPORT_KEYS = tuple(parent_report().model_dump(mode="json"))


@given(st.permutations(REPORT_KEYS))
def test_report_output_is_invariant_to_mapping_insertion_order(order) -> None:
    original = parent_report()
    payload = original.model_dump(mode="json")
    reordered = {key: payload[key] for key in order}
    rebuilt = ReportV22.model_validate_json(json.dumps(reordered))

    assert canonical_json_bytes(rebuilt) == canonical_json_bytes(original)


@given(evidence_graphs())
def test_report_rejects_generated_dangling_evidence_graphs(graph) -> None:
    payload = parent_report().model_dump(mode="json")
    payload["findings"][0]["evidence_ids"] = [graph.evidence_ids[0]]

    with pytest.raises(ValidationError, match="unknown evidence references"):
        ReportV22.model_validate_json(json.dumps(payload))


@given(st.sampled_from(("findings", "execution")))
@settings(max_examples=6)
def test_report_builders_never_mutate_caller_owned_models(builder: str) -> None:
    if builder == "findings":
        request = execution_plan_request().verified_reprioritization_input.public_findings_input
        before = canonical_json_bytes(request)
        build_public_findings(request)
    else:
        request = execution_plan_request()
        before = canonical_json_bytes(request)
        build_execution_plan(request)

    assert canonical_json_bytes(request) == before


@pytest.mark.anyio
@given(
    st.dictionaries(
        st.sampled_from(("engine", "q", "location", "hl")),
        SAFE_TEXT,
        min_size=1,
        max_size=4,
    ),
    st.lists(URLS, min_size=1, max_size=5, unique=True),
)
@settings(max_examples=20)
async def test_provider_boundary_never_mutates_request_or_response(params, urls) -> None:
    response_payload = {
        "search_metadata": {"status": "Success"},
        "organic_results": [
            {"position": index + 1, "link": url} for index, url in enumerate(urls)
        ],
    }
    before_params = deepcopy(params)
    before_response = deepcopy(response_payload)

    class Client:
        async def get(self, url, **kwargs):
            return httpx.Response(200, json=response_payload)

    await execute_serpapi_get(
        Client(),
        params,
        keys=["synthetic-key"],
        base_url="https://serpapi.example.test/search.json",
        monotonic=lambda: 1.0,
    )

    assert params == before_params
    assert response_payload == before_response
