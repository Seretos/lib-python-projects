"""Tests for the ETag / conditional-request transport cache (ticket #100).

Uses httpx.MockTransport to drive ETagTransport without making real
network calls.  Each test calls clear_etag_cache() via the autouse
fixture so the module-level store starts empty.
"""
from __future__ import annotations

import gzip
import threading
from typing import Callable

import httpx
import pytest

from lib_python_projects.providers._http_cache import (
    ETagTransport,
    clear_etag_cache,
)


# ---------- fixtures ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """Drain the module-level ETag store before every test."""
    clear_etag_cache()


def _make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ETagTransport:
    """Wrap a handler function in an ETagTransport via MockTransport."""
    return ETagTransport(httpx.MockTransport(handler))


def _get(transport: ETagTransport, url: str, **kwargs) -> httpx.Response:
    """Send a GET request through the transport directly."""
    request = httpx.Request("GET", url, **kwargs)
    return transport.handle_request(request)


def _post(transport: ETagTransport, url: str, **kwargs) -> httpx.Response:
    """Send a POST request through the transport directly."""
    request = httpx.Request("POST", url, **kwargs)
    return transport.handle_request(request)


def _patch(transport: ETagTransport, url: str, **kwargs) -> httpx.Response:
    """Send a PATCH request through the transport directly."""
    request = httpx.Request("PATCH", url, **kwargs)
    return transport.handle_request(request)


# ---------- core 304-replay regression (the primary bug this ticket fixes) ---


def test_304_replay_returns_synthetic_200() -> None:
    """REGRESSION: when the wrapped transport returns 304, ETagTransport must
    return a synthetic 200 with the original cached body (core fix for #100)."""
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First request: return real 200 with ETag.
            return httpx.Response(
                200,
                content=b'{"hello": "world"}',
                headers={"ETag": '"abc123"', "Content-Type": "application/json"},
            )
        # Second request: server says nothing changed.
        return httpx.Response(304, content=b"")

    transport = _make_transport(handler)
    r1 = _get(transport, "https://api.example.com/items")
    r2 = _get(transport, "https://api.example.com/items")

    assert r1.status_code == 200
    # The 304 must be transparently replayed as 200:
    assert r2.status_code == 200, "304 must be replayed as 200"
    assert r2.content == b'{"hello": "world"}'


def test_304_replayed_body_is_byte_identical() -> None:
    """Replayed body from a 304 must be byte-for-byte equal to the first response."""
    body = b"\x00\x01binary\xff\xfe"
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                content=body,
                headers={"ETag": '"v1"'},
            )
        return httpx.Response(304, content=b"")

    transport = _make_transport(handler)
    _get(transport, "https://api.example.com/data")
    r2 = _get(transport, "https://api.example.com/data")

    assert r2.content == body, "replayed body must be byte-identical to first response"


# ---------- first-request pass-through ----------------------------------------


def test_first_request_with_empty_cache_passes_through() -> None:
    """A first GET with an empty cache must return the real response unchanged."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"first", headers={"ETag": '"e1"'})

    transport = _make_transport(handler)
    r = _get(transport, "https://api.example.com/resource")
    assert r.status_code == 200
    assert r.content == b"first"


def test_second_request_sends_if_none_match() -> None:
    """After caching an ETag, the second GET must send If-None-Match."""
    seen_headers: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(req.headers))
        return httpx.Response(
            200,
            content=b"data",
            headers={"ETag": '"etag-value"'},
        )

    transport = _make_transport(handler)
    _get(transport, "https://api.example.com/res")
    _get(transport, "https://api.example.com/res")

    assert len(seen_headers) == 2
    first_headers = {k.lower(): v for k, v in seen_headers[0].items()}
    second_headers = {k.lower(): v for k, v in seen_headers[1].items()}
    assert "if-none-match" not in first_headers, "first request must not have If-None-Match"
    assert "if-none-match" in second_headers, "second request must carry If-None-Match"
    assert second_headers["if-none-match"] == '"etag-value"'


# ---------- Last-Modified (no ETag) -------------------------------------------


def test_last_modified_only_sends_if_modified_since() -> None:
    """When only Last-Modified is present (no ETag), subsequent GET must send
    If-Modified-Since instead of If-None-Match."""
    seen_headers: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(req.headers))
        return httpx.Response(
            200,
            content=b"content",
            headers={"Last-Modified": "Tue, 01 Jan 2025 00:00:00 GMT"},
        )

    transport = _make_transport(handler)
    _get(transport, "https://api.example.com/doc")
    _get(transport, "https://api.example.com/doc")

    assert len(seen_headers) == 2
    second_headers = {k.lower(): v for k, v in seen_headers[1].items()}
    assert "if-modified-since" in second_headers
    assert second_headers["if-modified-since"] == "Tue, 01 Jan 2025 00:00:00 GMT"
    assert "if-none-match" not in second_headers


# ---------- non-GET bypass ----------------------------------------------------


def test_post_bypasses_cache_no_if_none_match_injected() -> None:
    """POST requests must bypass the cache — no If-None-Match injected."""
    seen_requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_requests.append(req)
        return httpx.Response(201, content=b"{}", headers={"ETag": '"x"'})

    transport = _make_transport(handler)
    # First seed a GET so there is something in the cache:
    _get(transport, "https://api.example.com/items")
    # Clear seen to isolate the POST:
    seen_requests.clear()

    req = httpx.Request("POST", "https://api.example.com/items", content=b'{"a":1}')
    transport.handle_request(req)

    assert len(seen_requests) == 1
    post_headers = {k.lower(): v for k, v in seen_requests[0].headers.items()}
    assert "if-none-match" not in post_headers


def test_patch_bypasses_cache() -> None:
    """PATCH must not inject conditional headers."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"{}")

    transport = _make_transport(handler)
    req = httpx.Request("PATCH", "https://api.example.com/item/1", content=b'{}')
    transport.handle_request(req)

    patch_headers = {k.lower(): v for k, v in seen[0].headers.items()}
    assert "if-none-match" not in patch_headers
    assert "if-modified-since" not in patch_headers


def test_delete_bypasses_cache() -> None:
    """DELETE must not inject conditional headers."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(204, content=b"")

    transport = _make_transport(handler)
    req = httpx.Request("DELETE", "https://api.example.com/item/1")
    transport.handle_request(req)

    del_headers = {k.lower(): v for k, v in seen[0].headers.items()}
    assert "if-none-match" not in del_headers


# ---------- cache key stability -----------------------------------------------


def test_cache_key_stable_across_different_query_param_order() -> None:
    """GETs to the same URL with differently-ordered query params must share a cache entry."""
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, content=b"stable", headers={"ETag": '"q1"'})
        # Second call: assert If-None-Match was injected (cache hit).
        return httpx.Response(304, content=b"")

    transport = _make_transport(handler)
    r1 = _get(transport, "https://api.example.com/search?z=3&a=1&b=2")
    r2 = _get(transport, "https://api.example.com/search?b=2&z=3&a=1")

    assert r1.status_code == 200
    assert r2.status_code == 200, "reordered query params must hit the same cache entry"
    assert call_count == 2


# ---------- clear_etag_cache test hook ----------------------------------------


def test_clear_etag_cache_drains_store() -> None:
    """clear_etag_cache() must drain the store so the next request has no
    conditional headers."""
    call_count = 0
    seen_if_none_match: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        seen_if_none_match.append(req.headers.get("If-None-Match"))
        return httpx.Response(200, content=b"data", headers={"ETag": '"e"'})

    transport = _make_transport(handler)
    _get(transport, "https://api.example.com/x")
    # Drain the cache.
    clear_etag_cache()
    # Next request should have no conditional header.
    _get(transport, "https://api.example.com/x")

    assert seen_if_none_match[0] is None, "first request must not have If-None-Match"
    assert seen_if_none_match[1] is None, "after clear, request must not have If-None-Match"


# ---------- ETag update on fresh 200 -----------------------------------------


def test_new_etag_on_200_updates_stored_entry() -> None:
    """When the server returns a fresh 200 with a new ETag, the store must
    update so the next request sends the newer ETag."""
    etags_seen: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        etags_seen.append(req.headers.get("If-None-Match"))
        call_n = len(etags_seen)
        etag = f'"v{call_n}"'
        return httpx.Response(200, content=b"data", headers={"ETag": etag})

    transport = _make_transport(handler)
    _get(transport, "https://api.example.com/y")  # call 1: stores "v1"
    _get(transport, "https://api.example.com/y")  # call 2: sends "v1", stores "v2"
    _get(transport, "https://api.example.com/y")  # call 3: must send "v2"

    assert etags_seen[0] is None
    assert etags_seen[1] == '"v1"'
    assert etags_seen[2] == '"v2"', "third request must use the updated ETag"


# ---------- concurrent 304 requests don't corrupt the store -------------------


def test_concurrent_304_requests_do_not_corrupt_store() -> None:
    """Concurrent requests that all get 304 replies must all receive a valid
    synthetic 200 response without corrupting the shared cache store."""
    THREADS = 8
    body = b"shared body"
    lock = threading.Lock()
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        with lock:
            call_count += 1
            first = (call_count == 1)
        if first:
            return httpx.Response(200, content=body, headers={"ETag": '"shared"'})
        return httpx.Response(304, content=b"")

    transport = _make_transport(handler)
    # Seed the cache with one GET.
    _get(transport, "https://api.example.com/concurrent")

    results: list[httpx.Response] = [None] * THREADS  # type: ignore[list-item]

    def worker(idx: int) -> None:
        results[idx] = _get(transport, "https://api.example.com/concurrent")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for idx, resp in enumerate(results):
        assert resp.status_code == 200, f"thread {idx} got status {resp.status_code}"
        assert resp.content == body, f"thread {idx} got corrupted body"


# ---------- PATCH gzip regression (ticket #108) --------------------------------


def test_patch_gzip_response_decoded_without_error() -> None:
    """REGRESSION (#108): PATCH responses with Content-Encoding: gzip must be
    decoded exactly once and returned without error.

    Before the fix, handle_request returned the raw (unread) response for
    non-GET methods.  With a real streaming transport (WSGITransport), the
    body is NOT materialised until response.read() is called, so accessing
    response.content on the unread response raises httpx.ResponseNotRead.
    The fix calls response.read() before returning, which also triggers
    httpx's Content-Encoding decode path — giving us the plaintext body.

    This test uses WSGITransport (not MockTransport) so that the failure is
    genuine: MockTransport pre-materialises _content and masks the bug.
    """
    raw_body = b'{"id": 42, "title": "updated"}'
    compressed_body = gzip.compress(raw_body)

    def wsgi_app(environ, start_response):
        start_response("200 OK", [
            ("Content-Type", "application/json"),
            ("Content-Encoding", "gzip"),
            ("Content-Length", str(len(compressed_body))),
        ])
        return [compressed_body]

    transport = ETagTransport(httpx.WSGITransport(wsgi_app))
    response = _patch(transport, "http://testserver/item/42", content=b'{"title": "updated"}')

    assert response.status_code == 200
    # The body must equal the original pre-compression bytes — decoded exactly once.
    assert response.content == raw_body, (
        "PATCH response body must be gzip-decoded exactly once; "
        f"got {response.content!r}"
    )
