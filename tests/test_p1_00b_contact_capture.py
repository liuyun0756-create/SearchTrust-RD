"""Offline capture bookkeeping: no network and no real business hours fixtures."""
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.collectors.site_inventory_fetcher import SiteFetchResponse
from scripts import p1_00b_contact_capture as capture_script


@pytest.fixture
def anyio_backend():
    return "asyncio"


def setup_capture(monkeypatch, tmp_path, *, robots_status=200, disallow=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(capture_script, "ROOT", repo)
    calls = []

    class Fetcher:
        def __init__(self, **kwargs):
            self.client_factory = lambda target: SimpleNamespace(event_hooks={})

        def apply_crawl_delay(self, delay):
            pass

        async def fetch(self, url, **kwargs):
            client = self.client_factory(None)
            request = httpx.Request("GET", url)
            for hook in client.event_hooks["request"]:
                await hook(request)
            calls.append(url)
            if url.endswith("/robots.txt"):
                body = b"User-agent: *\nDisallow: /\n" if disallow else b"User-agent: *\nAllow: /\n"
                status, content_type = robots_status, "text/plain"
            else:
                body = (f'<a href="{capture_script.CONTACT}">Contact</a>' if url == capture_script.HOME
                        else '<h1>Contact</h1><p>Synthetic test content, not actual business hours.</p>').encode()
                status, content_type = 200, "text/html"
            response = httpx.Response(status, headers={"content-type":content_type}, request=request)
            for hook in client.event_hooks["response"]:
                await hook(response)
            return SiteFetchResponse(url, url, status, content_type, body)

    monkeypatch.setattr(capture_script, "BoundedSiteFetcher", Fetcher)
    return calls, Path("../private/batch"), Path("fixtures/batch")


@pytest.mark.anyio
async def test_relative_archive_paths_save_once_and_reject_accidental_rerun(monkeypatch, tmp_path):
    calls, private, summary = setup_capture(monkeypatch, tmp_path)
    assert await capture_script.capture(private, summary) == 0
    audit = json.loads((summary / "capture.json").read_text())
    assert audit["logical_fetches"] == len(audit["http_requests"]) == len(calls) == 3
    assert audit["responses"][0]["private_file"] == "private/batch/robots.body"
    assert gzip.decompress((summary / "contact.html.gz").read_bytes()) == (private / "contact.body").read_bytes()
    before = list(calls)
    with pytest.raises(FileExistsError):
        await capture_script.capture(private, summary)
    assert calls == before


@pytest.mark.anyio
@pytest.mark.parametrize("status,disallow,reason", [(503, False, "unexpected_http_status"), (200, True, "robots_disallowed")])
async def test_robots_failure_or_denial_stops_without_retry(monkeypatch, tmp_path, status, disallow, reason):
    calls, private, summary = setup_capture(monkeypatch, tmp_path, robots_status=status, disallow=disallow)
    assert await capture_script.capture(private, summary) == 1
    audit = json.loads((summary / "capture.json").read_text())
    assert audit["status"] == "stopped" and audit["reason"] == reason
    assert len(calls) == audit["logical_fetches"] == 1
    assert (private / "robots.body").is_file()
    assert not (private / "contact.body").exists()


@pytest.mark.anyio
async def test_approved_resume_checks_hash_and_keeps_cumulative_budget(monkeypatch, tmp_path):
    import hashlib
    calls, private, summary = setup_capture(monkeypatch, tmp_path)
    private.mkdir(parents=True); summary.mkdir(parents=True)
    robots = b'User-agent: *\nAllow: /\n'
    (private / 'robots.body').write_bytes(robots)
    original = dict(status='stopped', current_step='robots', reason='ValueError', logical_fetches=1,
                    http_requests=[dict(url=capture_script.HOME+'robots.txt',status_code=200)], responses=[], api_cost_usd=0)
    before = json.dumps(original)
    (private / 'capture.json').write_text(before)
    with pytest.raises(ValueError, match='robots_checksum_mismatch'):
        await capture_script.capture(private, summary, resume_robots_sha256='0'*64)
    assert calls == []
    assert await capture_script.capture(private, summary, resume_robots_sha256=hashlib.sha256(robots).hexdigest()) == 0
    resumed = json.loads((summary / 'capture-resumed.json').read_text())
    assert resumed['logical_fetches'] == len(resumed['http_requests']) == 3
    assert calls == [capture_script.HOME, capture_script.CONTACT]
    assert (private / 'capture.json').read_text() == before
