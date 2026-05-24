"""Tests for `GitHubProvider.list_prs` — search-path routing and shape mapping.

Covers:
  - Default filters route to `/repos/{owner}/{repo}/pulls`.
  - `labels`, `assignee`, and `search` each route to `/search/issues` with
    the expected `q` qualifiers.
  - On the search path, each stub is back-filled via `GET /pulls/{n}`, so
    the returned PullRequest has fully populated head/base/mergeable_state
    (ticket #6 core regression).
  - Status mapping: open/closed/merged driven by the full back-fill payload.
  - Direct `_map_pr` unit test: issue-stub with `pull_request.merged_at` →
    status="merged" (covers the merged-detection fix even though list_prs no
    longer feeds stubs to _map_pr on the search path).

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
from lib_python_projects.providers.github import GitHubProvider, _map_pr


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _pr_payload(number: int, **overrides) -> dict:
    """Build a minimal full-PR payload (as returned by `/pulls` or `/pulls/{n}`)."""
    base: dict = {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "mergeable": None,
        "mergeable_state": "clean",
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


def _search_and_detail_handler(stubs: list[dict], full_payloads: dict[int, dict]) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that serves /search/issues (returning stubs) and
    /repos/{o}/{r}/pulls/{n} (returning the corresponding full payload)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/search/issues":
            return _json({"items": stubs, "total_count": len(stubs)})
        # Detail endpoint: /repos/acme/backend/pulls/{n}
        for number, payload in full_payloads.items():
            if req.url.path == f"/repos/acme/backend/pulls/{number}":
                return _json(payload)
        raise AssertionError(f"Unexpected request path: {req.url.path}")

    return handler


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
    prs, has_more = provider.list_prs(_project(), token="t", filters=PRFilters())
    assert [pr.id for pr in prs] == ["1", "2"]
    assert len(seen) == 1
    assert seen[0].url.path == "/repos/acme/backend/pulls"


def test_labels_filter_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PRFilters(labels=[...])` must route to `/search/issues` with `label:` qualifier."""
    stub = _search_pr_stub(10)
    full = _pr_payload(10)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/search/issues":
            q = req.url.params["q"]
            assert "is:pr" in q
            assert "repo:acme/backend" in q
            assert "label:bug" in q
            return _json({"items": [stub], "total_count": 1})
        if req.url.path == "/repos/acme/backend/pulls/10":
            return _json(full)
        raise AssertionError(f"Unexpected request path: {req.url.path}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(labels=["bug"]))
    assert [pr.id for pr in prs] == ["10"]


def test_assignee_filter_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PRFilters(assignee=...)` must route to `/search/issues` with `assignee:` qualifier."""
    stub = _search_pr_stub(20)
    full = _pr_payload(20)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/search/issues":
            q = req.url.params["q"]
            assert "is:pr" in q
            assert "assignee:bob" in q
            return _json({"items": [stub], "total_count": 1})
        if req.url.path == "/repos/acme/backend/pulls/20":
            return _json(full)
        raise AssertionError(f"Unexpected request path: {req.url.path}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="bob"))
    assert [pr.id for pr in prs] == ["20"]


def test_search_text_filter_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PRFilters(search=...)` must route to `/search/issues` with search text in `q`."""
    stub = _search_pr_stub(30)
    full = _pr_payload(30)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/search/issues":
            q = req.url.params["q"]
            assert "is:pr" in q
            assert "fix memory leak" in q
            return _json({"items": [stub], "total_count": 1})
        if req.url.path == "/repos/acme/backend/pulls/30":
            return _json(full)
        raise AssertionError(f"Unexpected request path: {req.url.path}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(search="fix memory leak"))
    assert [pr.id for pr in prs] == ["30"]


# ---------- ticket #6 core regression: search path must back-fill full shape --


def test_search_path_backfills_head_base_mergeable_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #6: on the search path, returned PR must have populated
    head/base/mergeable_state — not the blank stubs from the search response.

    The handler serves /search/issues with an issue-shaped stub (no head/base),
    then serves /repos/.../pulls/42 with a full payload. The test asserts that
    the back-fill fetch happened and that the returned PullRequest carries the
    full values.
    """
    stub = _search_pr_stub(42)
    full = _pr_payload(
        42,
        mergeable_state="clean",
        head={
            "ref": "feat/my-feature",
            "sha": "deadbeef",
            "repo": {"full_name": "acme/backend"},
        },
        base={
            "ref": "main",
            "sha": "cafebabe",
        },
    )

    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path == "/search/issues":
            return _json({"items": [stub], "total_count": 1})
        if req.url.path == "/repos/acme/backend/pulls/42":
            return _json(full)
        raise AssertionError(f"Unexpected request path: {req.url.path}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(labels=["my-label"]))

    assert len(prs) == 1
    pr = prs[0]

    # Back-fill endpoint must have been called.
    assert "/repos/acme/backend/pulls/42" in seen_paths, (
        f"Detail endpoint was not requested; seen: {seen_paths}"
    )

    # Head must be fully populated (not blank stubs).
    assert pr.head["ref"] == "feat/my-feature", f"head.ref blank: {pr.head}"
    assert pr.head["sha"] == "deadbeef", f"head.sha blank: {pr.head}"
    assert pr.head["repo_full_name"] == "acme/backend", f"head.repo_full_name blank: {pr.head}"

    # Base must be fully populated.
    assert pr.base["ref"] == "main", f"base.ref blank: {pr.base}"
    assert pr.base["sha"] == "cafebabe", f"base.sha blank: {pr.base}"

    # mergeable_state must be populated.
    assert pr.mergeable_state == "clean", f"mergeable_state blank: {pr.mergeable_state!r}"


# ---------- status mapping via full back-fill payload -------------------------


def test_search_path_open_pr_maps_to_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search path with back-fill: full payload state=open → status='open'."""
    stub = _search_pr_stub(40, state="open")
    full = _pr_payload(40, state="open", merged=False, merged_at=None)

    handler = _search_and_detail_handler([stub], {40: full})
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="alice"))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.status == "open"
    assert pr.merged is False
    # Full payload populates head/base.
    assert pr.head["ref"] == "feat/branch"
    assert pr.draft is False
    assert pr.requested_reviewers == []


def test_search_path_closed_not_merged_maps_to_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search path with back-fill: full payload state=closed, merged=False → status='closed'."""
    stub = _search_pr_stub(50, state="closed", merged_at=None)
    full = _pr_payload(50, state="closed", merged=False, merged_at=None)

    handler = _search_and_detail_handler([stub], {50: full})
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="alice"))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.status == "closed"
    assert pr.merged is False


def test_search_path_merged_pr_maps_to_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search path with back-fill: full payload merged=True → status='merged'."""
    stub = _search_pr_stub(60, state="closed", merged_at="2024-03-15T10:00:00Z")
    full = _pr_payload(
        60,
        state="closed",
        merged=True,
        merged_at="2024-03-15T10:00:00Z",
    )

    handler = _search_and_detail_handler([stub], {60: full})
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(assignee="alice"))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.status == "merged", (
        f"Expected 'merged' but got {pr.status!r}"
    )
    assert pr.merged is True


# ---------- _map_pr unit test: merged detection via issue-stub ----------------


def test_map_pr_merged_detection_from_pull_request_stub() -> None:
    """Direct unit test: _map_pr with a search-style issue stub carrying
    pull_request.merged_at must yield status='merged'.

    This covers the nested-merged-detection fix in _map_pr, which stays
    relevant for callers that construct stub-shaped dicts directly even
    though list_prs no longer feeds stubs to _map_pr on the search path.
    """
    stub = {
        "number": 99,
        "title": "Fix thing",
        "body": "desc",
        "state": "closed",
        "user": {"login": "dev"},
        "assignees": [],
        "labels": [],
        "html_url": "https://github.com/acme/backend/pull/99",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "pull_request": {
            "merged_at": "2024-03-10T08:00:00Z",
            "url": "https://api.github.com/repos/acme/backend/pulls/99",
        },
        # deliberately omit top-level `merged` and `merged_at`
    }
    pr = _map_pr(stub)
    assert pr.status == "merged", (
        f"Expected 'merged' from pull_request.merged_at, got {pr.status!r}"
    )
    assert pr.merged is True


# ---------- head filter auto-qualification (Bug #36) --------------------------


def test_head_filter_bare_name_auto_qualified(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRFilters(head='feat/my-branch') must send 'acme:feat/my-branch' to the
    /pulls endpoint — bare branch names are silently unfiltered by GitHub's API
    without the 'owner:branch' qualification.
    """
    captured_params: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/pulls"
        captured_params.update(dict(req.url.params))
        return _json([_pr_payload(1)])

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(head="feat/my-branch"))
    assert [pr.id for pr in prs] == ["1"]
    assert captured_params.get("head") == "acme:feat/my-branch", (
        f"Expected 'acme:feat/my-branch', got {captured_params.get('head')!r}"
    )


def test_head_filter_already_qualified_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRFilters(head='acme:feat/my-branch') — already in 'owner:branch' form —
    must pass through unchanged (no double-prefix).
    """
    captured_params: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/pulls"
        captured_params.update(dict(req.url.params))
        return _json([_pr_payload(2)])

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, _ = provider.list_prs(_project(), token="t", filters=PRFilters(head="acme:feat/my-branch"))
    assert [pr.id for pr in prs] == ["2"]
    assert captured_params.get("head") == "acme:feat/my-branch", (
        f"Expected 'acme:feat/my-branch' (unchanged), got {captured_params.get('head')!r}"
    )


# ---------- has_more boundary regression (ticket #39) -------------------------


def test_list_prs_has_more_true_when_full_page_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #39: non-search path returns has_more=True when API
    returns exactly per_page (limit) items, indicating more pages exist."""
    limit = 3
    payloads = [_pr_payload(i) for i in range(1, limit + 1)]  # exactly `limit` items

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/pulls"
        return _json(payloads)

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, has_more = provider.list_prs(_project(), token="t", filters=PRFilters(limit=limit))
    assert len(prs) == limit
    assert has_more is True, "has_more must be True when API returns exactly per_page items"


def test_list_prs_has_more_false_when_partial_page_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #39: non-search path returns has_more=False when API
    returns fewer than per_page items, indicating no further pages."""
    limit = 10

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/pulls"
        # Return only 2 items when limit is 10 — partial page.
        return _json([_pr_payload(1), _pr_payload(2)])

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, has_more = provider.list_prs(_project(), token="t", filters=PRFilters(limit=limit))
    assert len(prs) == 2
    assert has_more is False, "has_more must be False when API returns fewer than per_page items"


def test_list_prs_search_path_has_more_true_when_full_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #39: search path returns has_more=True when back-filled
    results count equals per_page."""
    limit = 2
    stubs = [_search_pr_stub(i) for i in range(1, limit + 1)]
    full_payloads = {i: _pr_payload(i) for i in range(1, limit + 1)}

    handler = _search_and_detail_handler(stubs, full_payloads)
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    # Use assignee= to force the search path.
    prs, has_more = provider.list_prs(
        _project(), token="t", filters=PRFilters(assignee="alice", limit=limit)
    )
    assert len(prs) == limit
    assert has_more is True, "search path has_more must be True when full page returned"


def test_list_prs_search_path_has_more_false_when_partial_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #39: search path returns has_more=False when fewer
    results than per_page are returned."""
    limit = 10
    stubs = [_search_pr_stub(1)]
    full_payloads = {1: _pr_payload(1)}

    handler = _search_and_detail_handler(stubs, full_payloads)
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    prs, has_more = provider.list_prs(
        _project(), token="t", filters=PRFilters(assignee="alice", limit=limit)
    )
    assert len(prs) == 1
    assert has_more is False, "search path has_more must be False when partial page returned"
