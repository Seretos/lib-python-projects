"""In-memory ETag / Last-Modified conditional-request cache (ticket #100).

A module-level store that wraps any httpx transport and transparently:
  1. Injects ``If-None-Match`` / ``If-Modified-Since`` on repeated GET requests.
  2. Replays a cached 200 when the server returns 304 Not Modified.
  3. Updates the cache entry whenever the server sends a fresh 2xx with
     ``ETag`` or ``Last-Modified`` headers.

Only GET requests are intercepted; POST, PATCH, PUT, DELETE pass through
unmodified.

Note: ADO WIQL (``POST /_apis/wit/wiql``) and Work-Items-Batch
(``POST /_apis/wit/workitemsbatch``) are POST requests and are therefore
never intercepted by this transport — this is correct/expected.
"""
from __future__ import annotations

import dataclasses
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx


# ---------- cache data structure ---------------------------------------------


@dataclasses.dataclass
class _ETagEntry:
    """One cached response entry."""

    etag: str | None
    last_modified: str | None
    body: bytes
    headers: httpx.Headers


# ---------- module-level thread-safe store -----------------------------------

_etag_store: dict[str, _ETagEntry] = {}
_etag_lock = threading.Lock()

# Headers that encode the transfer representation (not the content itself) and
# must be stripped before caching so a 304-replay does not double-decode.
_STRIP_HEADERS: frozenset[str] = frozenset({"content-encoding", "transfer-encoding"})


def _cache_key(request: httpx.Request) -> str:
    """Return a stable string key for a GET request URL.

    Query parameters are sorted so ``?b=2&a=1`` and ``?a=1&b=2`` map to
    the same entry.
    """
    parsed = urlparse(str(request.url))
    sorted_query = urlencode(sorted(parse_qsl(parsed.query)))
    normalised = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        sorted_query,
        "",          # drop fragment — never relevant for REST
    ))
    return normalised


def clear_etag_cache() -> None:
    """Test hook: drain the in-memory ETag store."""
    with _etag_lock:
        _etag_store.clear()


# ---------- transport ---------------------------------------------------------


class ETagTransport(httpx.BaseTransport):
    """httpx transport wrapper that adds conditional-request caching.

    Wraps a ``wrapped`` transport (typically ``httpx.HTTPTransport()``) and
    intercepts GET requests to implement transparent ETag / Last-Modified
    caching with 304-replay.

    Non-GET methods pass straight through to ``wrapped``.
    """

    def __init__(self, wrapped: httpx.BaseTransport) -> None:
        self._wrapped = wrapped

    # ------------------------------------------------------------------
    # httpx.BaseTransport contract
    # ------------------------------------------------------------------

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            response = self._wrapped.handle_request(request)
            response.read()
            return response

        key = _cache_key(request)

        # Inject conditional headers if we have a cached entry.
        with _etag_lock:
            entry = _etag_store.get(key)

        if entry is not None:
            # Build a new request with the conditional headers injected.
            # We cannot mutate the original request, so we rebuild it.
            headers = dict(request.headers)
            if entry.etag is not None:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified is not None:
                headers["If-Modified-Since"] = entry.last_modified
            request = httpx.Request(
                method=request.method,
                url=request.url,
                headers=headers,
                content=b"",
            )

        response = self._wrapped.handle_request(request)

        # httpx requires us to call read() to materialise the body when
        # coming out of a real transport.
        response.read()

        if response.status_code == 304 and entry is not None:
            # Reconstruct a synthetic 200 from the cached body + headers.
            return httpx.Response(
                status_code=200,
                headers=entry.headers,
                content=entry.body,
            )

        if response.is_success:
            # Update (or insert) the cache entry.
            etag = response.headers.get("ETag") or response.headers.get("etag")
            last_modified = (
                response.headers.get("Last-Modified")
                or response.headers.get("last-modified")
            )
            if etag or last_modified:
                filtered_headers = httpx.Headers({
                    k: v
                    for k, v in response.headers.multi_items()
                    if k.lower() not in _STRIP_HEADERS
                })
                new_entry = _ETagEntry(
                    etag=etag,
                    last_modified=last_modified,
                    body=response.content,
                    headers=filtered_headers,
                )
                with _etag_lock:
                    _etag_store[key] = new_entry

        return response

    def close(self) -> None:
        self._wrapped.close()


# ---------- factory ----------------------------------------------------------


def make_cached_transport() -> ETagTransport:
    """Return an ``ETagTransport`` wrapping a fresh ``httpx.HTTPTransport``."""
    return ETagTransport(httpx.HTTPTransport())
