import logging
import re
import ssl
from pathlib import Path

import httpx
import pytest

from app.crtc.client import (
    CrtcClient,
    FetchFailed,
    ResponseTooLarge,
    TooManyRedirects,
    UrlNotAllowed,
    check_url,
)

URL = "https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/18/data"


def redirect(to: str, status: int = 302):
    return httpx.Response(status, headers={"location": to})


# ----------------------------------------------------------------------------- allowlist


@pytest.mark.parametrize(
    "url",
    [
        "http://applications.crtc.gc.ca/x",  # http
        "https://evil.example/x",
        "https://crtc.gc.ca.evil.example/x",  # suffix trick
        "https://evilcrtc.gc.ca/x",  # not a subdomain
        "https://crtc.gc.ca@evil.example/x",  # userinfo trick
        "https://user:pw@crtc.gc.ca/x",
        "https://applications.crtc.gc.ca:8443/x",
        "ftp://crtc.gc.ca/x",
        "javascript:alert(1)",
        "https://127.0.0.1/x",
        "",
    ],
)
async def test_initial_url_not_allowed(make_client, url):
    client = make_client(lambda r: httpx.Response(200, content=b"x"))
    with pytest.raises(UrlNotAllowed):
        await client.fetch(url)
    assert client.seen == []  # nothing was sent


def test_check_url_accepts_apex_and_subdomains():
    assert check_url("https://crtc.gc.ca/a") == "crtc.gc.ca"
    assert check_url("https://www.crtc.gc.ca/a") == "www.crtc.gc.ca"
    assert check_url("https://APPLICATIONS.CRTC.GC.CA/a")


async def test_redirect_to_non_allowlisted_host_is_refused(make_client):
    client = make_client(lambda r: redirect("https://evil.example/steal"))
    with pytest.raises(UrlNotAllowed):
        await client.fetch(URL)
    assert [r.url.host for r in client.seen] == ["applications.crtc.gc.ca"]  # evil never contacted


async def test_redirect_from_https_to_http_is_refused(make_client):
    client = make_client(lambda r: redirect("http://www.crtc.gc.ca/x"))
    with pytest.raises(UrlNotAllowed, match="https only"):
        await client.fetch(URL)
    assert len(client.seen) == 1


async def test_redirect_to_non_allowlisted_host_on_second_hop_is_refused(make_client):
    def handler(r):
        if r.url.host == "www.crtc.gc.ca":
            return redirect("https://evil.example/")
        return redirect("https://www.crtc.gc.ca/hop")

    client = make_client(handler)
    with pytest.raises(UrlNotAllowed):
        await client.fetch(URL)
    assert [r.url.host for r in client.seen] == ["applications.crtc.gc.ca", "www.crtc.gc.ca"]


async def test_redirect_within_allowlist_is_followed_incl_relative(make_client):
    def handler(r):
        if r.url.path == "/a":
            return redirect("/b", 301)
        if r.url.path == "/b":
            return redirect("https://www.crtc.gc.ca/c", 307)
        return httpx.Response(200, content=b"ok")

    client = make_client(handler)
    resp = await client.fetch("https://crtc.gc.ca/a")
    assert resp.content == b"ok"
    assert resp.url == "https://www.crtc.gc.ca/c"


async def test_redirect_cap(make_client):
    client = make_client(lambda r: redirect("https://crtc.gc.ca/loop"), max_redirects=2)
    with pytest.raises(TooManyRedirects):
        await client.fetch(URL)
    assert len(client.seen) == 3  # initial + 2 hops, then refuse


async def test_redirect_without_location_fails(make_client):
    client = make_client(lambda r: httpx.Response(302))
    with pytest.raises(FetchFailed):
        await client.fetch(URL)


# ----------------------------------------------------------------------------- SSRF guard


@pytest.mark.parametrize(
    "addrs",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["172.16.4.4"],
        ["192.168.1.10"],
        ["169.254.169.254"],  # cloud metadata
        ["0.0.0.0"],
        ["100.64.0.1"],  # CGNAT
        ["::1"],
        ["fe80::1"],
        ["fc00::1"],
        ["::ffff:127.0.0.1"],  # IPv4-mapped loopback
        ["52.60.1.1", "10.0.0.5"],  # one good + one bad answer: still refused
    ],
)
async def test_private_loopback_linklocal_resolution_is_refused(make_client, addrs):
    async def resolver(host):
        return addrs

    client = make_client(lambda r: httpx.Response(200, content=b"x"), resolver=resolver)
    with pytest.raises(UrlNotAllowed, match="non-public"):
        await client.fetch(URL)
    assert client.seen == []


async def test_redirect_hop_resolving_to_private_ip_is_refused(make_client):
    async def resolver(host):
        return ["10.1.2.3"] if host == "www.crtc.gc.ca" else ["52.60.1.1"]

    client = make_client(lambda r: redirect("https://www.crtc.gc.ca/x"), resolver=resolver)
    with pytest.raises(UrlNotAllowed, match="non-public"):
        await client.fetch(URL)
    assert len(client.seen) == 1


async def test_dns_failure_is_fetch_failed(make_client):
    async def resolver(host):
        raise OSError("nxdomain")

    client = make_client(lambda r: httpx.Response(200), resolver=resolver)
    with pytest.raises(FetchFailed):
        await client.fetch(URL)


# ----------------------------------------------------------------------------- size cap


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, n_chunks: int, chunk: int = 1000):
        self.n_chunks, self.chunk, self.yielded = n_chunks, chunk, 0

    async def __aiter__(self):
        for _ in range(self.n_chunks):
            self.yielded += 1
            yield b"x" * self.chunk


async def test_oversize_response_is_aborted_while_streaming(make_client):
    stream = CountingStream(n_chunks=10_000)  # 10 MB, and NO Content-Length header
    client = make_client(lambda r: httpx.Response(200, stream=stream), max_response_bytes=5_000)
    with pytest.raises(ResponseTooLarge):
        await client.fetch(URL)
    assert stream.yielded < 20  # stopped right after the cap, did not drain 10_000 chunks


async def test_oversize_content_length_is_refused_before_reading(make_client):
    stream = CountingStream(n_chunks=3)

    def handler(r):
        return httpx.Response(200, headers={"content-length": "999999999"}, stream=stream)

    client = make_client(handler, max_response_bytes=5_000)
    with pytest.raises(ResponseTooLarge):
        await client.fetch(URL)
    assert stream.yielded == 0


async def test_response_exactly_at_cap_is_ok(make_client):
    client = make_client(
        lambda r: httpx.Response(200, content=b"x" * 5000), max_response_bytes=5000
    )
    assert len((await client.fetch(URL)).content) == 5000


# ----------------------------------------------------------------------------- retries / rate limit


async def test_retries_with_backoff_then_succeeds(make_client):
    calls = iter([503, 429, 200])
    client = make_client(lambda r: httpx.Response(next(calls), content=b"ok"))
    assert (await client.fetch(URL)).content == b"ok"
    assert client.slept == [1.0, 2.0]  # exponential backoff
    assert len(client.seen) == 3


async def test_retries_are_bounded(make_client):
    client = make_client(lambda r: httpx.Response(503), max_retries=2)
    with pytest.raises(FetchFailed):
        await client.fetch(URL)
    assert len(client.seen) == 3


async def test_transport_errors_are_retried(make_client):
    n = {"i": 0}

    def handler(r):
        n["i"] += 1
        if n["i"] < 3:
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(200, content=b"ok")

    client = make_client(handler)
    assert (await client.fetch(URL)).content == b"ok"


async def test_4xx_is_not_retried(make_client):
    client = make_client(lambda r: httpx.Response(404))
    with pytest.raises(FetchFailed):
        await client.fetch(URL)
    assert len(client.seen) == 1


async def test_rate_limit_spaces_requests(make_client):
    t = {"now": 100.0}
    client = make_client(
        lambda r: httpx.Response(200, content=b"ok"), min_interval_s=1.0, clock=lambda: t["now"]
    )
    await client.fetch(URL)
    assert client.slept == []  # first request is immediate
    await client.fetch(URL)  # clock did not advance -> must wait the full interval
    assert client.slept == [1.0]


# ----------------------------------------------------------------------------- misc controls


async def test_user_agent_is_descriptive(make_client):
    client = make_client(lambda r: httpx.Response(200, content=b"ok"))
    await client.fetch(URL)
    ua = client.seen[0].headers["user-agent"]
    assert ua.startswith("crtc-copilot/") and "python-httpx" not in ua


def test_tls_verification_is_on_in_default_client():
    http = CrtcClient()._new_http_client()
    ctx = http._transport._pool._ssl_context  # type: ignore[attr-defined]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert http.follow_redirects is False


def test_tls_verification_cannot_be_disabled_in_source():
    src = Path(__file__).parent.parent / "app"
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert not re.search(r"verify\s*=\s*False", text), py
        assert "CERT_NONE" not in text, py


async def test_logs_contain_no_query_strings_cookies_or_bodies(make_client, caplog):
    caplog.set_level(logging.DEBUG)
    client = make_client(
        lambda r: httpx.Response(
            200, content=b"SECRET-BODY-CONTENT", headers={"set-cookie": "sid=SECRETCOOKIE"}
        )
    )
    await client.fetch(URL + "?token=SECRETTOKEN")
    assert "SECRET" not in caplog.text
    assert "fetched https://applications.crtc.gc.ca" in caplog.text
