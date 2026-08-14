"""Bounded model fallback for incomplete target-page address candidates.

The deterministic address pipeline remains authoritative.  This module only
asks an OpenAI-compatible model to join nearby source lines after that pipeline
found a street-shaped candidate but could not confirm a complete address.  A
model answer is accepted only when every returned token is grounded in the
provided source window and the normal deterministic parser accepts the result.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.report_v21.address_candidates import AddressCandidate
from app.report_v21.address_facts import build_address_facts


logger = logging.getLogger(__name__)

_RECOVERABLE_REASONS = frozenset({"missing_city", "missing_state"})
_MAX_WINDOWS = 5
_CONTEXT_BEFORE = 2
_CONTEXT_AFTER = 3


async def confirm_incomplete_address_candidates(
    content: str,
    page_facts: dict[str, Any],
    *,
    source_url: str = "",
) -> list[AddressCandidate]:
    """Return source-grounded, parser-valid address candidates from the model."""
    if not _configured() or page_facts.get("addresses"):
        return []

    rejected = (
        page_facts.get("rejected_observations", {}).get("addresses", [])
        if isinstance(page_facts.get("rejected_observations"), dict)
        else []
    )
    windows = _candidate_windows(content, rejected)
    if not windows:
        return []

    try:
        payload = await _request_confirmation(windows)
    except Exception as exc:  # noqa: BLE001 - this fallback must never block a report
        logger.warning(
            "[AddressAI] confirmation unavailable type=%s; keeping deterministic result",
            type(exc).__name__,
        )
        return []

    confirmed = _validated_candidates(payload, windows, source_url=source_url)
    logger.info(
        "[AddressAI] evaluated_windows=%d confirmed_addresses=%d",
        len(windows),
        len(confirmed),
    )
    return confirmed


def _configured() -> bool:
    return bool(
        settings.ADDRESS_AI_ENABLED
        and settings.ADDRESS_AI_API_KEY.strip()
        and settings.ADDRESS_AI_BASE_URL.strip()
        and settings.ADDRESS_AI_MODEL.strip()
    )


def _candidate_windows(
    content: str,
    rejected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lines = [_clean_line(line) for line in str(content or "").splitlines()]
    lines = [line for line in lines if line]
    windows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for observation in rejected:
        if not isinstance(observation, dict):
            continue
        if observation.get("rejection_reason") not in _RECOVERABLE_REASONS:
            continue
        raw = _clean_line(observation.get("raw_value"))
        if not raw or not any(character.isdigit() for character in raw):
            continue

        matched = False
        raw_key = _source_key(raw)
        for index, line in enumerate(lines):
            if raw_key not in _source_key(line):
                continue
            context_lines = lines[
                max(0, index - _CONTEXT_BEFORE):min(len(lines), index + _CONTEXT_AFTER + 1)
            ]
            context = "\n".join(context_lines)
            key = (raw_key, _source_key(context))
            if key in seen:
                continue
            seen.add(key)
            windows.append({
                "candidate_id": len(windows) + 1,
                "partial_address": raw,
                "context_lines": context_lines,
            })
            matched = True
            if len(windows) >= _MAX_WINDOWS:
                return windows

        if not matched:
            excerpt = _clean_line(observation.get("excerpt"))
            if excerpt:
                key = (raw_key, _source_key(excerpt))
                if key not in seen:
                    seen.add(key)
                    windows.append({
                        "candidate_id": len(windows) + 1,
                        "partial_address": raw,
                        "context_lines": [excerpt],
                    })
                    if len(windows) >= _MAX_WINDOWS:
                        return windows
    return windows


async def _request_confirmation(windows: list[dict[str, Any]]) -> dict[str, Any]:
    system_prompt = (
        "You verify whether adjacent webpage lines form complete postal addresses. "
        "Use only text present in the supplied context. Never infer or add a location. "
        "Preserve the source wording and return only valid JSON."
    )
    user_prompt = {
        "task": "Confirm and join incomplete US address candidates.",
        "rules": [
            "Return one result for each candidate_id.",
            "Set is_address false unless the context explicitly supplies street, city, and state.",
            "full_address and every component must use only words and numbers present in context_lines.",
            "evidence_lines must quote the exact context lines used.",
        ],
        "output_schema": {
            "addresses": [{
                "candidate_id": 1,
                "is_address": True,
                "full_address": "",
                "components": {
                    "street": "",
                    "city": "",
                    "state": "",
                    "postal_code": "",
                },
                "evidence_lines": [],
            }],
        },
        "candidates": windows,
    }
    request_payload = {
        "model": settings.ADDRESS_AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    url = f"{settings.ADDRESS_AI_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.ADDRESS_AI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(float(settings.ADDRESS_AI_TIMEOUT)),
        follow_redirects=True,
    ) as client:
        response = await client.post(url, headers=headers, json=request_payload)
        response.raise_for_status()
        data = response.json()

    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("Address AI response did not contain choices")
    message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Address AI response did not contain message content")
    return _parse_json_object(content)


def _parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Address AI content was not a JSON object")
    return parsed


def _validated_candidates(
    payload: dict[str, Any],
    windows: list[dict[str, Any]],
    *,
    source_url: str,
) -> list[AddressCandidate]:
    by_id = {int(item["candidate_id"]): item for item in windows}
    confirmed: list[AddressCandidate] = []
    seen: set[str] = set()
    for result in payload.get("addresses") or []:
        if not isinstance(result, dict) or result.get("is_address") is not True:
            continue
        try:
            candidate_id = int(result.get("candidate_id"))
        except (TypeError, ValueError):
            continue
        window = by_id.get(candidate_id)
        full_address = _clean_line(result.get("full_address"))
        if not window or not full_address:
            continue
        if not _grounded_result(result, window, full_address):
            continue

        candidate = AddressCandidate(
            raw_value=full_address,
            source_type="page.ai.confirmed_address",
            source_url=source_url,
            locator=f"AI-confirmed candidate window {candidate_id}",
            excerpt=" | ".join(str(line) for line in result.get("evidence_lines") or []),
            visibility="visible",
        )
        if not build_address_facts([candidate]).get("addresses"):
            continue
        key = _source_key(full_address)
        if key and key not in seen:
            seen.add(key)
            confirmed.append(candidate)
    return confirmed


def _grounded_result(
    result: dict[str, Any],
    window: dict[str, Any],
    full_address: str,
) -> bool:
    context_lines = [str(line) for line in window.get("context_lines") or []]
    context_key = _source_key(" ".join(context_lines))
    address_key = _source_key(full_address)
    partial_key = _source_key(window.get("partial_address"))
    if not context_key or not address_key or not partial_key:
        return False
    if not _tokens(address_key).issubset(_tokens(context_key)):
        return False
    if not _tokens(partial_key).issubset(_tokens(address_key)):
        return False

    evidence = result.get("evidence_lines") or []
    if not isinstance(evidence, list) or not evidence:
        return False
    context_line_keys = {_source_key(line) for line in context_lines}
    if any(_source_key(line) not in context_line_keys for line in evidence):
        return False

    components = result.get("components") or {}
    if not isinstance(components, dict):
        return False
    for key in ("street", "city", "state"):
        value = _source_key(components.get(key))
        if not value or not _tokens(value).issubset(_tokens(context_key)):
            return False
    postal_code = _source_key(components.get("postal_code"))
    if postal_code and not _tokens(postal_code).issubset(_tokens(context_key)):
        return False
    return True


def _clean_line(value: Any) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", str(value or ""))
    text = re.sub(r"[*_`#]+", " ", text)
    return " ".join(text.split()).strip(" -|:")


def _source_key(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _tokens(value: Any) -> set[str]:
    return set(_source_key(value).split())
