"""Tests for GitHubProvider.discover_projects (ticket #81).

Mirrors the httpx stubbing pattern used in test_github_token_probe.py:
- httpx.MockTransport wraps a handler callable
- _install_mock patches the module-level _client factory via monkeypatch
- per-test handler functions return the desired httpx.Response
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import GitHubProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Install a mock transport and return the list of captured requests."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


def _json(payload: object, status_code: int = 200, link: str | None = None) -> httpx.Response:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if link is not None:
        headers["link"] = link
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )


def _make_repo(
    full_name: str,
    description: str | None = "A description",
    permissions: dict | None = None,
) -> dict:
    """Build a minimal GitHub repo API payload."""
    repo: dict = {
        "id": 1,
        "name": full_name.split("/")[-1],
        "full_name": full_name,
        "private": False,
        "description": description,
    }
    if permissions is not None:
        repo["permissions"] = permissions
    return repo


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_discover_single_page_no_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """One page, 2 repos, limit=10 -> 2 projects, truncated=False, reason=None."""
    repos = [
        _make_repo("acme/alpha", description="Alpha project", permissions={
            "admin": False, "maintain": False, "push": True,
            "triage": True, "pull": True,
        }),
        _make_repo("acme/beta", description="Beta project", permissions={
            "admin": True, "maintain": True, "push": True,
            "triage": True, "pull": True,
        }),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/user/repos" in req.url.path
        return _json(repos)

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=10)

    assert result.reason is None
    assert result.truncated is False
    assert len(result.projects) == 2

    alpha = result.projects[0]
    assert alpha.provider == "github"
    assert alpha.path == "acme/alpha"
    assert alpha.description == "Alpha project"
    assert alpha.permissions.issues_create is True
    assert alpha.permissions.pulls_merge is False

    beta = result.projects[1]
    assert beta.path == "acme/beta"
    assert beta.permissions.pulls_merge is True
    assert beta.permissions.issues_create is True


def test_discover_multi_page_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two pages (3 then 2 repos), limit=10 -> 5 projects, truncated=False."""
    page1 = [_make_repo(f"acme/repo{i}") for i in range(3)]
    page2 = [_make_repo(f"acme/repo{i}") for i in range(3, 5)]
    next_url = f"{github_provider.API_BASE}/user/repos?page=2"
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if "page=2" in str(req.url):
            return _json(page2)
        return _json(page1, link=f'<{next_url}>; rel="next"')

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=10)

    assert result.reason is None
    assert result.truncated is False
    assert len(result.projects) == 5
    # Verify second request used the next-page URL from the Link header.
    assert len(calls) == 2
    assert "page=2" in calls[1]


def test_discover_truncated_mid_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """One page of 5 repos, limit=3 -> 3 projects, truncated=True."""
    repos = [_make_repo(f"acme/repo{i}") for i in range(5)]

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(repos)

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=3)

    assert result.truncated is True
    assert len(result.projects) == 3
    assert result.reason is None


def test_discover_truncated_at_page_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """First page has 3 repos with a rel=next Link, limit=3 -> truncated=True."""
    repos = [_make_repo(f"acme/repo{i}") for i in range(3)]
    next_url = f"{github_provider.API_BASE}/user/repos?page=2"

    def handler(req: httpx.Request) -> httpx.Response:
        # Only first page is ever requested; second page must NOT be fetched.
        return _json(repos, link=f'<{next_url}>; rel="next"')

    seen = _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=3)

    assert result.truncated is True
    assert len(result.projects) == 3
    assert result.reason is None
    # Should only have made 1 request (not fetched the next page).
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


def test_discover_401_returns_bad_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 401 -> projects == [], reason == 'bad_credentials', no raise."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Bad credentials"}, status_code=401)

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("bad-token", limit=100)

    assert result.projects == []
    assert result.reason == "bad_credentials"
    assert result.truncated is False


def test_discover_network_error_returns_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport-level ConnectError -> projects == [], reason == 'network_error'."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns resolution failed", request=req)

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=100)

    assert result.projects == []
    assert result.reason == "network_error"
    assert result.truncated is False


def test_discover_unexpected_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 500 -> reason == 'http_500', projects == []."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Internal Server Error"}, status_code=500)

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=100)

    assert result.projects == []
    assert result.reason == "http_500"
    assert result.truncated is False


# ---------------------------------------------------------------------------
# Edge-case / mapping tests
# ---------------------------------------------------------------------------


def test_discover_null_description_becomes_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo with description=null in the API response -> project.description == ''."""
    repos = [_make_repo("acme/nulldesc", description=None)]

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(repos)

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=10)

    assert len(result.projects) == 1
    assert result.projects[0].description == ""


def test_discover_missing_permissions_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repo without a 'permissions' key -> all-False TokenCapabilities, no raise."""
    # _make_repo without permissions= kwarg omits the key entirely.
    repos = [_make_repo("acme/noperms")]

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(repos)

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().discover_projects("tok", limit=10)

    assert len(result.projects) == 1
    caps = result.projects[0].permissions
    assert caps.issues_create is False
    assert caps.issues_modify is False
    assert caps.pulls_create is False
    assert caps.pulls_modify is False
    assert caps.pulls_merge is False
    assert caps.reason is None


# ---------------------------------------------------------------------------
# Programmer-error / invalid-argument tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_discover_invalid_limit_raises_value_error_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError immediately, before any HTTP call is made.

    This is a regression test for the missing _validate_limit() guard: previously
    discover_projects(token, limit=0) made a live network call and returned a
    successful-looking ProjectDiscoveryResult(projects=[], truncated=True) instead
    of raising.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        pytest.fail(
            f"discover_projects(limit={bad_limit}) must not make any HTTP request, "
            f"but received a request to {req.url}"
        )

    _install_mock(monkeypatch, handler)

    with pytest.raises(ValueError):
        GitHubProvider().discover_projects("tok", limit=bad_limit)
