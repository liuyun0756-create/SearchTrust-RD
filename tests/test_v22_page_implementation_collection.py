from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.collectors.page_implementation import collect_page_implementation, robots_policy
from app.collectors.site_inventory_fetcher import AsyncRateLimiter, BoundedSiteFetcher
from test_v22_page_implementation_checks import prepared


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def simulated_wait(monkeypatch):
    # The limiter has independent real-clock tests; these network tests do not sleep.
    async def wait(self):
        pass
    monkeypatch.setattr(AsyncRateLimiter, "wait", wait)


async def run_check(*, plan=None, robots_status=200, robots=b"User-agent: *\nAllow: /",
                    page_status=200, page=b'<h1>Emergency dental care</h1><a id="contact" href="/contact">Contact</a>',
                    media="text/html", redirect_stage=None, error_stage=None, resolver=None, headers=None):
    calls, targets = [], []
    async def public(host):
        return ["93.184.216.34"]
    def handler(request):
        calls.append(request)
        stage = "robots" if request.url.path == "/robots.txt" else "page"
        if stage == error_stage:
            raise httpx.ReadTimeout("Secret exception MUST NOT appear", request=request)
        if stage == redirect_stage:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        if stage == "robots":
            return httpx.Response(robots_status, content=robots)
        return httpx.Response(page_status, content=page, headers={"content-type": media, **(headers or {})})
    def factory(target):
        targets.append(target)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await collect_page_implementation(plan or prepared(), observation_id=uuid4(),
        resolver=resolver or public, client_factory=factory,
        now=lambda: datetime(2026, 10, 23, 3, tzinfo=timezone.utc))
    return result, calls, targets


@pytest.mark.anyio
async def test_checks_three_conditions_with_only_two_pinned_gets_and_no_raw_content():
    result, calls, targets = await run_check()
    assert result.status == "checked" and [r.state for r in result.results] == ["observed"]*3
    assert [str(r.url) for r in calls] == ["https://clinic.example.invalid/robots.txt", "https://clinic.example.invalid/emergency"]
    assert all(r.method == "GET" and "cookie" not in r.headers and "authorization" not in r.headers for r in calls)
    assert all(t.addresses == ("93.184.216.34",) for t in targets)
    assert result.effect_conclusion is None and result.causal_eligible is False
    dumped = result.model_dump_json()
    assert "<h1>" not in dumped and "<a " not in dumped and "body" not in dumped


@pytest.mark.anyio
@pytest.mark.parametrize("status", [404, 410])
async def test_absent_robots_allows_page(status):
    result, calls, _ = await run_check(robots_status=status)
    assert result.status == "checked" and len(calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503, 204])
async def test_unavailable_robots_stops_before_page(status):
    result, calls, _ = await run_check(robots_status=status)
    assert result.failure == "robots_unavailable" and result.failure_stage == "robots" and len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("body,code", [
    (b"User-agent: *\nDisallow: /", "robots_disallowed"),
    (b"User-agent: SearchTrust-SiteInventory\nDisallow: /emergency", "robots_disallowed"),
    (b"User-agent: *\nCrawl-delay: 30", "crawl_delay_exceeds_budget"),
    (b"User-agent: *\nCrawl-delay: nan", "robots_invalid"),
    (b"User-agent: *\nCrawl-delay: -1", "robots_invalid"),
    (b"User-agent: *\nRequest-rate: nope", "robots_invalid"),
    (b"<html>Login</html>", "robots_invalid"), (b"garbage", "robots_invalid"),
    (b"\xff", "robots_invalid"), (b"Disallow: /", "robots_invalid"),
    (b"x"*262145, "response_too_large"),
], ids=["disallow-all", "disallow-agent", "delay-budget", "nan-delay", "negative-delay", "bad-rate", "html", "garbage", "encoding", "no-agent", "oversize"])
async def test_robots_policy_limits(body, code):
    result, calls, _ = await run_check(robots=body)
    assert result.failure == code and len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["robots", "page"])
async def test_redirect_never_requests_destination(stage):
    result, calls, _ = await run_check(redirect_stage=stage)
    assert result.failure == "redirect_requires_confirmation"
    assert len(calls) == (1 if stage == "robots" else 2)
    assert all(r.url.host == "clinic.example.invalid" for r in calls)


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["robots", "page"])
async def test_timeout_not_content_absence_and_exception_is_not_stored(stage):
    result, _, _ = await run_check(error_stage=stage)
    assert result.failure == "page_timeout"
    assert all(r.state == "unable_to_check" for r in result.results)
    assert "Secret" not in result.model_dump_json()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 500, 503, 204])
async def test_page_non_success_is_not_missing_content(status):
    result, _, _ = await run_check(page_status=status)
    assert result.failure == "http_status" and result.http_status == status
    assert all(r.state == "unable_to_check" for r in result.results)


@pytest.mark.anyio
@pytest.mark.parametrize("body,media,code", [
    (b"PDF", "application/pdf", "content_unsupported"),
    (b"", "text/html", "empty_page"),
    (b"<script>Emergency dental care</script>", "text/html", "empty_page"),
    (b'<input type="password"><h1>Emergency dental care</h1>', "text/html", "access_restricted"),
    (b'<script src="/cdn-cgi/challenge-platform/x"></script>Care', "text/html", "access_restricted"),
    (b"<h1>Verify you are human</h1>", "text/html", "access_restricted"),
    (b'<base href="https://other.invalid/">Content', "text/html", "parse_failed"),
    (b"<p>\x00</p>", "text/html", "parse_failed"),
    (b"<p>"*129+b"Content", "text/html", "parse_failed"),
    (b"<br>"*20001, "text/html", "parse_failed"),
    (b"x"*1048577, "text/html", "response_too_large"),
], ids=["pdf", "empty", "script-only", "password", "challenge-script", "challenge-text", "base", "nul", "depth", "nodes", "oversize"])
async def test_response_and_parse_boundaries(body, media, code):
    result, _, _ = await run_check(page=body, media=media)
    assert result.failure == code and result.content_checksum is None
    assert all(r.state == "unable_to_check" for r in result.results)


@pytest.mark.anyio
async def test_headers_limit_before_read_and_hidden_content_is_not_evidence():
    result, _, _ = await run_check(headers={"content-length": "1048577"})
    assert result.failure == "response_too_large"
    body = b'''<head><title>Emergency dental care</title></head><main>Other service
      <script>Emergency dental care</script><style>Emergency dental care</style>
      <template><a id="contact" href="/contact">Emergency dental care</a></template>
      <div hidden><a id="contact" href="/contact">Emergency dental care</a></div>
      <div aria-hidden="true"><span>Emergency dental care</span></div>
      <noscript>Emergency dental care</noscript><textarea>Emergency dental care</textarea>
      <input value="Emergency dental care"><a href="https://outside.invalid/contact">External</a>
      <a href="/contact?token=PRIVATE">Token</a><a href="mailto:private@example.invalid">Email</a></main>'''
    result, _, _ = await run_check(page=body)
    assert result.status == "checked" and [r.state for r in result.results] == ["not_observed"]*3
    assert "PRIVATE" not in result.model_dump_json() and "private@" not in result.model_dump_json()


@pytest.mark.anyio
async def test_unicode_whitespace_normalization_but_case_sensitive_anchors():
    result, _, _ = await run_check(page='<h1>Ｅｍｅｒｇｅｎｃｙ\n dental   care</h1><a id="Contact" href="/contact">Go</a>'.encode())
    assert [r.state for r in result.results] == ["observed", "observed", "not_observed"]


@pytest.mark.anyio
@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["169.254.169.254"], ["::1"], ["93.184.216.34", "10.0.0.1"], []])
async def test_unsafe_dns_never_connects(addresses):
    async def resolver(host): return addresses
    result, calls, _ = await run_check(resolver=resolver)
    assert result.failure == ("unsafe_target" if addresses else "dns_unavailable") and calls == []


@pytest.mark.anyio
async def test_dns_rebinding_on_page_is_blocked():
    resolutions = []
    async def resolver(host):
        resolutions.append(host)
        return ["93.184.216.34"] if len(resolutions) == 1 else ["127.0.0.1"]
    result, calls, targets = await run_check(resolver=resolver)
    assert result.failure == "unsafe_target" and len(calls) == len(targets) == 1


@pytest.mark.anyio
async def test_invalid_plan_is_revalidated_before_network():
    plan = prepared().model_copy(update={"target_url": "https://127.0.0.1/private"})
    with pytest.raises(ValueError): await run_check(plan=plan)


def test_fractional_delay_and_request_rate_are_honored_conservatively():
    _, delay = robots_policy(b"User-agent: *\nCrawl-delay: 1.5\nRequest-rate: 1/4", "https://clinic.example.invalid/")
    assert delay == 4


@pytest.mark.anyio
async def test_default_fetcher_preserves_pinned_transport_and_disabled_environment_proxy():
    from app.security_v22.urls import resolve_public_url
    from app.security_v22.http import PinnedAsyncHTTPTransport
    async def resolver(host): return ["93.184.216.34"]
    target = await resolve_public_url("https://clinic.example.invalid/", resolver=resolver)
    fetcher = BoundedSiteFetcher(connect_timeout=5, read_timeout=10, total_timeout=30,
        max_redirects=0, concurrency=1, requests_per_second=1)
    async with fetcher._default_client(target) as client:
        assert isinstance(client._transport, PinnedAsyncHTTPTransport)
        assert client.trust_env is False and client.follow_redirects is False


@pytest.mark.anyio
@pytest.mark.parametrize("policy,code", [
    (b"User-agent: *\nAllow: /\nDisallow: /emergency", "robots_disallowed"),
    (b"User-agent: AnotherBot\nDisallow: /emergency", "robots_disallowed"),
    (b"User-agent: *\nDisallow: /emerg*", "robots_invalid"),
    (b"User-agent: *\nAllow: /emergency$", "robots_invalid"),
    (b"User-agent: *\nDisallow: /%65mergency", "robots_disallowed"),
    (b"unexpected: document", "robots_invalid"),
    (b"User-agent: *\nAllow: /\x01", "robots_invalid"),
])
async def test_uncertain_or_overlapping_robots_rules_fail_closed(policy, code):
    result, calls, _ = await run_check(robots=policy)
    assert result.failure == code and len(calls) == 1


@pytest.mark.anyio
async def test_total_budget_includes_policy_and_delay(monkeypatch):
    import asyncio
    from app.collectors import page_implementation as module
    real_timeout = asyncio.timeout
    budgets = []
    def short_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(0.01)
    async def slow_wait(self):
        await asyncio.sleep(0.05)
    monkeypatch.setattr(module.asyncio, "timeout", short_timeout)
    monkeypatch.setattr(AsyncRateLimiter, "wait", slow_wait)
    result, calls, _ = await run_check()
    assert budgets == [30] and result.failure == "page_timeout" and not calls


@pytest.mark.anyio
async def test_cancellation_is_not_retried_or_recorded_as_success():
    import asyncio
    async def cancelled(host):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run_check(resolver=cancelled)


@pytest.mark.anyio
async def test_zero_requests_before_source_time_and_naive_clock():
    def forbidden(target):
        pytest.fail("must not connect")
    for now in (datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2026, 10, 23)):
        with pytest.raises(ValueError):
            await collect_page_implementation(prepared(), observation_id=uuid4(), client_factory=forbidden, now=lambda: now)


@pytest.mark.anyio
async def test_stream_limit_without_content_length():
    class Oversize(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(17):
                yield b"x" * 65536
    calls = []
    async def public(host): return ["93.184.216.34"]
    def handler(request):
        calls.append(request)
        if request.url.path == "/robots.txt": return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Oversize())
    def factory(target): return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await collect_page_implementation(prepared(), observation_id=uuid4(), resolver=public,
        client_factory=factory, now=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert result.failure == "response_too_large" and len(calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("body", [b"<![unexpected]><p>Content</p>", b"<plaintext><a href='/contact'>Content", b"<xmp>Content"])
async def test_unsupported_html_parsing_is_an_explicit_failure(body):
    result, _, _ = await run_check(page=body)
    assert result.failure == "parse_failed"


@pytest.mark.anyio
async def test_title_and_hidden_input_are_not_selected_evidence():
    result, _, _ = await run_check(page=b'<title>Emergency dental care</title><input type="hidden" id="contact"><p>Other content</p>')
    assert result.status == "checked" and all(r.state == "not_observed" for r in result.results)
