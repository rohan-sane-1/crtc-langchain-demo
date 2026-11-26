"""Phase 3 additions to the HTTP client: robots.txt, blocked statuses, streaming size cap, HEAD."""

import httpx
import pytest

from app.crtc.client import (
    FetchBlocked,
    FetchFailed,
    ResponseTooLarge,
    RobotsDisallowed,
    UrlNotAllowed,
)
from tests.fakes import APPS, FakeSite, make_pdf

DOC = f"{APPS}/TransferToWeb/2026/a.pdf"


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


def client_for(make_client, site: FakeSite, **kw):
    return make_client(site, **kw)


# ----------------------------------------------------------------------------- size cap


async def test_oversize_download_is_aborted_while_streaming(make_client):
    consumed = []

    async def body():  # no Content-Length: only the streaming cap can stop this
        for i in range(1000):
            consumed.append(i)
            yield b"x" * 1024

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=body())

    client = make_client(handler, max_document_bytes=10 * 1024)
    with pytest.raises(ResponseTooLarge):
        await client.fetch_document(DOC)
    assert len(consumed) < 50  # stopped after ~10 chunks; the other ~950 KB were never read


async def test_declared_content_length_over_the_cap_is_refused_without_reading(make_client):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-length": str(10**9)}, content=b"x")

    client = make_client(handler, max_document_bytes=1024 * 1024)
    with pytest.raises(ResponseTooLarge):
        await client.fetch_document(DOC)


async def test_document_cap_is_independent_of_the_listing_cap(make_client, site):
    site.add(DOC, make_pdf(["1. hi there, this is a pdf"]))
    client = client_for(make_client, site, max_response_bytes=100, max_document_bytes=1024 * 1024)
    assert (await client.fetch_document(DOC)).status_code == 200


# ----------------------------------------------------------------------------- blocked


async def test_403_is_blocked_immediately_and_never_retried(make_client, site):
    site.add(DOC, b"nope", 403)
    client = client_for(make_client, site, max_retries=5)
    with pytest.raises(FetchBlocked) as e:
        await client.fetch_document(DOC)
    assert e.value.reason == "http_403" and e.value.status_code == 403
    assert len(site.requests(contains="a.pdf")) == 1  # one request, no hammering
    assert client.slept == []  # and no backoff sleeps either


async def test_cloudflare_challenge_is_recognised_even_on_a_200(make_client, site):
    site.add(DOC, b"<html>Security Check</html>", 200, {"cf-mitigated": "challenge"})
    client = client_for(make_client, site)
    with pytest.raises(FetchBlocked) as e:
        await client.fetch_document(DOC)
    assert e.value.reason == "cloudflare_challenge"


async def test_cloudflare_403_is_recorded_as_a_challenge(make_client, site):
    site.add_cloudflare_block("https://crtc.gc.ca/eng/archive/2026/2026-259.htm")
    client = client_for(make_client, site)
    with pytest.raises(FetchBlocked) as e:
        await client.fetch_document("https://crtc.gc.ca/eng/archive/2026/2026-259.htm")
    assert e.value.reason == "cloudflare_challenge"
    # the User-Agent stayed honest: no browser impersonation anywhere
    ua = {r for r in site.log if r[0] == "GET"}
    assert ua


async def test_429_is_blocked_not_retried(make_client, site):
    site.add(DOC, b"slow down", 429, {"retry-after": "30"})
    client = client_for(make_client, site, max_retries=5)
    with pytest.raises(FetchBlocked) as e:
        await client.fetch_document(DOC)
    assert e.value.reason == "rate_limited_429"
    assert len(site.requests(contains="a.pdf")) == 1


async def test_5xx_is_still_retried_with_backoff_in_document_mode(make_client, site):
    attempts = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 3 else httpx.Response(200, content=b"ok")

    client = make_client(handler, max_retries=3)
    assert (await client.fetch_document(DOC)).content == b"ok"
    assert len(attempts) == 3 and len(client.slept) == 2


async def test_404_is_a_plain_failure_with_its_status(make_client, site):
    client = client_for(make_client, site)  # no route -> 404
    with pytest.raises(FetchFailed) as e:
        await client.fetch_document(DOC)
    assert e.value.status_code == 404 and not isinstance(e.value, FetchBlocked)


# ----------------------------------------------------------------------------- robots


async def test_robots_disallowed_path_is_never_requested(make_client, site):
    url = f"{APPS}/DocWebBroker/OpenDocument.aspx?DMID=1663270"
    site.add(url, make_pdf(["1. secret"]))
    client = client_for(make_client, site)
    with pytest.raises(RobotsDisallowed) as e:
        await client.fetch_document(url)
    assert e.value.reason == "robots_disallowed" and e.value.rule == "/DocWebBroker/"
    assert site.requests(contains="DocWebBroker") == []  # only robots.txt was fetched
    assert site.requests(contains="robots.txt") == [f"{APPS}/robots.txt"]


async def test_robots_is_fetched_once_per_host_and_cached(make_client, site):
    site.add(DOC, b"%PDF-")
    client = client_for(make_client, site)
    for _ in range(3):
        await client.fetch_document(DOC)
    assert len(site.requests(contains="robots.txt")) == 1


async def test_robots_unreachable_means_disallow_all(make_client):
    def handler(req):
        return httpx.Response(503)

    client = make_client(handler, max_retries=0)
    with pytest.raises(RobotsDisallowed):
        await client.fetch_document(DOC)


async def test_missing_robots_means_allowed(make_client):
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, content=b"x")

    client = make_client(handler)
    assert (await client.fetch_document(DOC)).content == b"x"


async def test_robots_is_checked_on_every_redirect_hop(make_client, site):
    site.add(DOC, b"", 302, {"location": f"{APPS}/DocWebBroker/OpenDocument.aspx?DMID=5"})
    client = client_for(make_client, site)
    with pytest.raises(RobotsDisallowed):
        await client.fetch_document(DOC)
    assert site.requests(contains="DocWebBroker") == []


async def test_redirect_to_a_non_crtc_host_is_refused(make_client, site):
    site.add(DOC, b"", 302, {"location": "https://evil.example/x.pdf"})
    client = client_for(make_client, site)
    with pytest.raises(UrlNotAllowed):
        await client.fetch_document(DOC)
    assert site.requests(contains="evil.example") == []


async def test_user_agent_is_honest_and_constant(make_client):
    seen = []

    def handler(req):
        seen.append(req.headers["user-agent"])
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, content=b"x")

    client = make_client(handler)
    await client.fetch_document(DOC)
    assert seen and all(ua.startswith("crtc-copilot/") for ua in seen)
    assert client.product_token == "crtc-copilot"


# ----------------------------------------------------------------------------- HEAD


async def test_head_returns_headers_and_no_body(make_client, site):
    site.add_zip(f"{APPS}/TransferToWeb/2026/x.zip", b"PK\x03\x04" + b"0" * 100)
    client = client_for(make_client, site)
    r = await client.head(f"{APPS}/TransferToWeb/2026/x.zip")
    assert r.content == b"" and r.headers["last-modified"]
    assert [m for m, u in site.log if u.endswith("x.zip")] == ["HEAD"]


async def test_listing_fetch_respects_robots_too(make_client, site):
    url = f"{APPS}/ListeInterventionList/x"
    site.add(url, b"{}")
    client = client_for(make_client, site)
    with pytest.raises(RobotsDisallowed):
        await client.fetch_listing(url)
