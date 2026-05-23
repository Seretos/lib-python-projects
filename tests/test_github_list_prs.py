"""Tests for `GitHubProvider.list_prs` — search-path routing and shape mapping.

Covers:
  - Default filters route to `/repos/{owner}/{repo}/pulls`.
  - `labels`, `assignee`, and `search` each route to `/search/issues` with
    the expected `q` qualifiers.
  - `/search/issues` items that omit `head`/`base`/`draft`/`requested_reviewers`
    are mapped to safe defaults (regression: merged detection via
    `pull_request.merged_at`).

Pattern mirrors `tests/test_github_list_filters.py`:
  httpx.MockTransport + monkeypatch on `github_provider._client`.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.base import PRFilters
from lib_python_projects.providers.github import GitHubProvider


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _pr_payload(number: int, **overrides) -> dict:
    """Build a minimal full-PR payload (as returned by `/pulls`)."""
    base: dict = {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "mergeable": None,
        "mergeable_state": None,
        "merge_commit_sha": None,
        "auto_merge": None,
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "requested_reviewers": [],
        "head": {
            "ref": "feat/branch",
            "sha": "abc123",
            "repo": {"full_name": "acme/backend"},
        },
        "base": {
            "ref": "main",
            "sha": "def456",
        },
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _search_pr_stub(number: int, state: str = "open", merged_at: str | None = None, **overrides) -> dict:
    """Build a minimal `/search/issues` PR stub (omits head/base/draft/requested_reviewers)."""
    base: dict = {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "state": state,
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        # `pull_request` sub-object is present on search results for PRs
        "pull_request": {
            "merged_at": merged_at,
            "url": f"https://api.github.com/repos/acme/backend/pulls/{number}",
        },
        # NOTE: no top-level `head`, `base`, `draft`, or `requested_reviewers`
    }
    base.update(overrides)
    return base


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
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


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


# ---------- routing tests ----------------------------------------------------


def test_default_filters_route_to_pulls_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default `PRFilters()` (no labels/assignee/search) must use `/pulls`."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/pulls", (
            f"expected /pulls endpoint, got {req.url}"
        )
        params = dict(req.url.params)
        assert params["sort"] == "created"
        assert params["direction"] == "desc"
        # Default status is "open" → maps to state=open on /pulls
        assert params["state"] == "open"
        return _json([_pr_payload(1), _pr_payload(2)])

    seen = _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs = provider.list_prs(_project(), token="t", filters=PRFilters())
    assert [pr.id for pr in prs] == ["1", "2"]
    assert len(seen) == 1
    assert seen[0].url.path == "/repos/acme/backend/pulls"


def test_labels_filter_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PRFilters(labels=[...])` must route to `/search/issues` with `label:` qualifier."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues", (
            f"expected /search/issues, got {req.url}"
        )
        q = req.url.params["q"]
        assert "is:pr" in q
        assert "repo:acme/backend" in q
        assert "label:bug" in q
        return _json({"items": [_search_pr_stub(10)], "total_count": 1})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs = provider.list_prs(_project(), token="t", filters=PRFilters(labels=["bug"]))
    assert [pr.id for pr in prs] == ["10"]


def test_assignee_filter_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PRFilters(assignee=...)` must route to `/search/issues` with `assignee:` qualifier."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues", (
            f"expected /search/issues, got {req.url}"
        )
        q = req.url.params["q"]
        assert "is:pr" in q
        assert "assignee:bob" in q
        return _json({"items": [_search_pr_stub(20)], "total_count": 1})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="bob"))
    assert [pr.id for pr in prs] == ["20"]


def test_search_text_filter_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PRFilters(search=...)` must route to `/search/issues` with search text in `q`."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues", (
            f"expected /search/issues, got {req.url}"
        )
        q = req.url.params["q"]
        assert "is:pr" in q
        assert "fix memory leak" in q
        return _json({"items": [_search_pr_stub(30)], "total_count": 1})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs = provider.list_prs(_project(), token="t", filters=PRFilters(search="fix memory leak"))
    assert [pr.id for pr in prs] == ["30"]


# ---------- search-shape mapping tests ---------------------------------------


def test_search_stub_open_pr_maps_to_open_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search stub with state=open produces status='open' and safe defaults."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"items": [_search_pr_stub(40, state="open")], "total_count": 1})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="alice"))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.status == "open"
    assert pr.merged is False
    # Safe defaults for absent head/base
    assert pr.head == {"ref": "", "sha": "", "repo_full_name": ""}
    assert pr.base == {"ref": "", "sha": ""}
    # Safe defaults for absent draft and requested_reviewers
    assert pr.draft is False
    assert pr.requested_reviewers == []


def test_search_stub_closed_not_merged_maps_to_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search stub: closed with no merged_at anywhere → status='closed'."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"items": [_search_pr_stub(50, state="closed", merged_at=None)], "total_count": 1})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="alice"))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.status == "closed"
    assert pr.merged is False


def test_search_stub_merged_pr_maps_to_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: search stub with pull_request.merged_at set → status='merged'.

    Before the fix, _map_pr only checked top-level `merged`/`merged_at`;
    search stubs only carry the timestamp in `pull_request.merged_at`, so
    merged PRs were incorrectly reported as 'closed'.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({
            "items": [
                _search_pr_stub(
                    60,
                    state="closed",
                    merged_at="2024-03-15T10:00:00Z",
                )
            ],
            "total_count": 1,
        })

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="alice"))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.status == "merged", (
        f"Expected 'merged' but got {pr.status!r}; "
        "pull_request.merged_at was not checked"
    )
    assert pr.merged is True
