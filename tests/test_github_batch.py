"""Tests for the batch read aggregation interface (ticket #101).

Covers:
1.  Import smoke — fetch_open_board and BatchProjectResult importable from providers.
2.  Two repos, one GraphQL POST — correct mapping, exactly 1 HTTP request.
3.  projects=[] → [] and zero HTTP requests.
4.  Single repo, no open items → one result, empty lists, error=None.
5.  Partial failure — one alias null → error set, other result normal.
6.  429 + Retry-After → RateLimitError(429) with correct retry_after.
7.  403 + x-ratelimit-remaining: 0 → RateLimitError(403).
8.  403 without rate-limit headers (fine-grained PAT) → GitHubError(403), NOT RateLimitError.
9.  Label sort order matches _map_issue (alphabetical).
10. PullRequest.reviewers == [] and mergeable is None.
11. Timestamps pass through normalize_timestamp (fractional-second createdAt → second precision).
12. Parity — Ticket fields from _map_graphql_issue identical to ones from _map_issue.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects.models import ProjectConfig
from lib_python_projects.providers import BatchProjectResult, fetch_open_board
from lib_python_projects.providers import github_batch as batch_mod
from lib_python_projects.providers.base import RateLimitError
from lib_python_projects.providers.github import GitHubError, _map_issue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project(path: str = "acme/backend", pid: str = "acme") -> ProjectConfig:
    return ProjectConfig(id=pid, provider="github", path=path)


def _json_resp(
    payload: object,
    status_code: int = 200,
    headers: dict | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Monkeypatch _graphql_client to use a MockTransport wrapping *handler*.

    Returns a list that accumulates every request seen.
    """
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_graphql_client(token: str) -> httpx.Client:
        return httpx.Client(transport=transport, timeout=30.0)

    monkeypatch.setattr(batch_mod, "_graphql_client", fake_graphql_client)
    return seen


def _make_issue_node(
    number: int = 1,
    title: str = "Test issue",
    body: str = "body",
    url: str = "https://github.com/acme/backend/issues/1",
    state: str = "OPEN",
    author: str = "alice",
    assignees: list[str] | None = None,
    labels: list[str] | None = None,
    created_at: str = "2025-01-01T10:00:00Z",
    updated_at: str = "2025-01-02T12:00:00Z",
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": url,
        "state": state,
        "author": {"login": author},
        "assignees": {"nodes": [{"login": a} for a in (assignees or [])]},
        "labels": {"nodes": [{"name": lb} for lb in (labels or [])]},
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def _make_pr_node(
    number: int = 10,
    title: str = "Test PR",
    body: str = "pr body",
    url: str = "https://github.com/acme/backend/pull/10",
    state: str = "OPEN",
    is_draft: bool = False,
    author: str = "bob",
    assignees: list[str] | None = None,
    labels: list[str] | None = None,
    head_ref: str = "feature/x",
    head_sha: str = "abc123",
    head_repo: str = "acme/backend",
    base_ref: str = "main",
    base_sha: str = "def456",
    merged: bool = False,
    merged_at: str | None = None,
    requested_reviewers: list[str] | None = None,
    created_at: str = "2025-01-03T08:00:00Z",
    updated_at: str = "2025-01-04T09:00:00Z",
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": url,
        "state": state,
        "isDraft": is_draft,
        "author": {"login": author},
        "assignees": {"nodes": [{"login": a} for a in (assignees or [])]},
        "labels": {"nodes": [{"name": lb} for lb in (labels or [])]},
        "headRefName": head_ref,
        "headRefOid": head_sha,
        "headRepository": {"nameWithOwner": head_repo},
        "baseRefName": base_ref,
        "baseRefOid": base_sha,
        "merged": merged,
        "mergedAt": merged_at,
        "reviewRequests": {"nodes": [{"requestedReviewer": {"login": r}} for r in (requested_reviewers or [])]},
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def _make_graphql_body(aliases: dict) -> dict:
    """Build a typical successful GraphQL response body.

    ``aliases`` maps alias name (e.g. ``"r0"``) to the repository data dict
    (or ``None`` to simulate a null alias).
    """
    return {"data": aliases}


# ---------------------------------------------------------------------------
# Test 1: import smoke
# ---------------------------------------------------------------------------


def test_import_smoke() -> None:
    """BatchProjectResult and fetch_open_board must be importable from providers."""
    from lib_python_projects.providers import BatchProjectResult, fetch_open_board  # noqa: F401

    assert callable(fetch_open_board)
    assert BatchProjectResult is not None


# ---------------------------------------------------------------------------
# Test 2: Two repos, ONE GraphQL POST, correct mapping
# ---------------------------------------------------------------------------


def test_two_repos_single_post_correct_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two repos → exactly one POST; each BatchProjectResult has 1 Ticket + 1 PR."""
    project_a = _project("owner1/repo1", "proj-a")
    project_b = _project("owner2/repo2", "proj-b")

    issue_a = _make_issue_node(number=1, title="Issue A", author="alice")
    pr_a = _make_pr_node(number=10, title="PR A", author="alice")

    issue_b = _make_issue_node(
        number=2, title="Issue B", author="bob",
        url="https://github.com/owner2/repo2/issues/2",
    )
    pr_b = _make_pr_node(
        number=20, title="PR B", author="bob",
        url="https://github.com/owner2/repo2/pull/20",
        head_repo="owner2/repo2",
    )

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": [issue_a]},
            "pullRequests": {"nodes": [pr_a]},
        },
        "r1": {
            "issues": {"nodes": [issue_b]},
            "pullRequests": {"nodes": [pr_b]},
        },
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    seen = _install_mock(monkeypatch, handler)
    results = fetch_open_board([project_a, project_b], token="tok")

    # Exactly one POST request
    assert len(seen) == 1
    assert seen[0].method == "POST"

    # Two results
    assert len(results) == 2

    # Result for project_a
    ra = results[0]
    assert ra.project is project_a
    assert ra.error is None
    assert len(ra.tickets) == 1
    assert len(ra.pull_requests) == 1
    assert ra.tickets[0].id == "1"
    assert ra.tickets[0].title == "Issue A"
    assert ra.tickets[0].author == "alice"
    assert ra.tickets[0].status == "open"
    assert ra.pull_requests[0].id == "10"
    assert ra.pull_requests[0].title == "PR A"
    assert ra.pull_requests[0].author == "alice"
    assert ra.pull_requests[0].status == "open"

    # Result for project_b
    rb = results[1]
    assert rb.project is project_b
    assert rb.error is None
    assert len(rb.tickets) == 1
    assert len(rb.pull_requests) == 1
    assert rb.tickets[0].id == "2"
    assert rb.pull_requests[0].id == "20"


# ---------------------------------------------------------------------------
# Test 3: projects=[] → [] and zero HTTP requests
# ---------------------------------------------------------------------------


def test_empty_projects_returns_empty_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_open_board([]) must return [] without any HTTP request."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json_resp({})

    _install_mock(monkeypatch, handler)
    result = fetch_open_board([], token="tok")

    assert result == []
    assert seen == []


# ---------------------------------------------------------------------------
# Test 4: Single repo, no open items
# ---------------------------------------------------------------------------


def test_single_repo_no_open_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo with no open issues/PRs → one BatchProjectResult with empty lists, error=None."""
    project = _project("org/empty-repo")

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": []},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    assert len(results) == 1
    assert results[0].project is project
    assert results[0].tickets == []
    assert results[0].pull_requests == []
    assert results[0].error is None


# ---------------------------------------------------------------------------
# Test 5: Partial failure — one alias null
# ---------------------------------------------------------------------------


def test_partial_failure_null_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """When one alias is null in the GraphQL response, that project gets error set,
    while the other project's data is still mapped normally."""
    project_good = _project("good/repo", "good")
    project_bad = _project("bad/repo", "bad")

    issue_good = _make_issue_node(number=5, title="Good issue")

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": [issue_good]},
            "pullRequests": {"nodes": []},
        },
        "r1": None,  # alias is null → partial failure
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project_good, project_bad], token="tok")

    assert len(results) == 2

    good_result = results[0]
    assert good_result.error is None
    assert len(good_result.tickets) == 1
    assert good_result.tickets[0].id == "5"

    bad_result = results[1]
    assert bad_result.error is not None
    assert "bad/repo" in bad_result.error
    assert bad_result.tickets == []
    assert bad_result.pull_requests == []


# ---------------------------------------------------------------------------
# Test 6: 429 + Retry-After → RateLimitError(429)
# ---------------------------------------------------------------------------


def test_429_raises_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 429 + Retry-After → RateLimitError(429) with correct retry_after."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            content=b'{"message":"Too Many Requests"}',
            headers={
                "Content-Type": "application/json",
                "Retry-After": "60",
            },
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(RateLimitError) as exc:
        fetch_open_board([_project()], token="tok")

    assert exc.value.status == 429
    assert exc.value.retry_after == 60


# ---------------------------------------------------------------------------
# Test 7: 403 + x-ratelimit-remaining: 0 → RateLimitError(403)
# ---------------------------------------------------------------------------


def test_403_rate_limit_raises_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 + x-ratelimit-remaining=0 → RateLimitError(403)."""
    import time
    reset_ts = int(time.time()) + 120

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            content=b'{"message":"API rate limit exceeded"}',
            headers={
                "Content-Type": "application/json",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(reset_ts),
            },
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(RateLimitError) as exc:
        fetch_open_board([_project()], token="tok")

    assert exc.value.status == 403
    assert exc.value.retry_after is not None
    assert abs(exc.value.retry_after - 120) <= 5


# ---------------------------------------------------------------------------
# Test 8: 403 without rate-limit headers → GitHubError(403), NOT RateLimitError
# ---------------------------------------------------------------------------


def test_403_no_rate_limit_headers_raises_github_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 WITHOUT x-ratelimit-remaining header → GitHubError, not RateLimitError.

    This is the fine-grained PAT scope rejection path.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            content=b'{"message":"Resource not accessible by integration"}',
            headers={"Content-Type": "application/json"},
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        fetch_open_board([_project()], token="tok")

    assert exc.value.status == 403
    # Must NOT be a RateLimitError
    assert not isinstance(exc.value, RateLimitError)


# ---------------------------------------------------------------------------
# Test 9: Label sort order — alphabetical
# ---------------------------------------------------------------------------


def test_label_sort_order_alphabetical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Labels on a Ticket from _map_graphql_issue are sorted alphabetically,
    matching the _map_issue behaviour."""
    project = _project()
    issue_node = _make_issue_node(
        number=7,
        labels=["zebra", "alpha", "middleware", "bug"],
    )

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": [issue_node]},
            "pullRequests": {"nodes": []},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    ticket = results[0].tickets[0]
    assert ticket.labels == ["alpha", "bug", "middleware", "zebra"]


# ---------------------------------------------------------------------------
# Test 10: PullRequest.reviewers == [] and mergeable is None
# ---------------------------------------------------------------------------


def test_pr_reviewers_empty_and_mergeable_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """PullRequest.reviewers must be [] and PullRequest.mergeable must be None
    (matching the REST list_prs contract)."""
    project = _project()
    pr_node = _make_pr_node(number=99, requested_reviewers=["reviewer1"])

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.reviewers == []
    assert pr.mergeable is None
    assert pr.requested_reviewers == ["reviewer1"]


# ---------------------------------------------------------------------------
# Test 11: Timestamps through normalize_timestamp
# ---------------------------------------------------------------------------


def test_timestamps_normalized_fractional_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fractional-second createdAt/updatedAt must be normalised to second precision."""
    project = _project()
    # Simulate a GraphQL response with sub-second precision (GitLab-style)
    issue_node = _make_issue_node(
        number=3,
        created_at="2025-05-20T23:07:59.507Z",
        updated_at="2025-06-01T00:00:00.123456Z",
    )

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": [issue_node]},
            "pullRequests": {"nodes": []},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    ticket = results[0].tickets[0]
    assert ticket.created_at == "2025-05-20T23:07:59Z"
    assert ticket.updated_at == "2025-06-01T00:00:00Z"


def test_pr_timestamps_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR createdAt/updatedAt with fractional seconds are also normalised."""
    project = _project()
    pr_node = _make_pr_node(
        number=42,
        created_at="2025-03-15T14:22:33.999Z",
        updated_at="2025-03-16T08:00:00.001Z",
    )

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.created_at == "2025-03-15T14:22:33Z"
    assert pr.updated_at == "2025-03-16T08:00:00Z"


# ---------------------------------------------------------------------------
# Test 12: Parity — Ticket field names match _map_issue output
# ---------------------------------------------------------------------------


def test_ticket_field_parity_with_map_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Ticket produced by _map_graphql_issue must have the same field names
    as one produced by _map_issue (REST path), and equivalent values for
    comparable inputs."""
    import dataclasses

    # Build the equivalent REST payload that _map_issue understands
    rest_payload = {
        "number": 42,
        "title": "Parity test",
        "body": "body text",
        "state": "open",
        "state_reason": None,
        "user": {"login": "carol"},
        "assignees": [{"login": "dave"}],
        "labels": [{"name": "bug"}, {"name": "alpha"}],
        "html_url": "https://github.com/acme/backend/issues/42",
        "created_at": "2025-01-10T12:00:00Z",
        "updated_at": "2025-01-11T08:00:00Z",
    }
    rest_ticket = _map_issue(rest_payload)

    # Build the equivalent GraphQL node
    graphql_node = _make_issue_node(
        number=42,
        title="Parity test",
        body="body text",
        state="OPEN",
        author="carol",
        assignees=["dave"],
        labels=["bug", "alpha"],
        url="https://github.com/acme/backend/issues/42",
        created_at="2025-01-10T12:00:00Z",
        updated_at="2025-01-11T08:00:00Z",
    )

    project = _project()
    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": [graphql_node]},
            "pullRequests": {"nodes": []},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")
    graphql_ticket = results[0].tickets[0]

    # Field names must be identical (same dataclass)
    rest_fields = {f.name for f in dataclasses.fields(rest_ticket)}
    graphql_fields = {f.name for f in dataclasses.fields(graphql_ticket)}
    assert rest_fields == graphql_fields

    # Values must match for all comparable fields
    assert graphql_ticket.id == rest_ticket.id
    assert graphql_ticket.title == rest_ticket.title
    assert graphql_ticket.body == rest_ticket.body
    assert graphql_ticket.status == rest_ticket.status
    assert graphql_ticket.author == rest_ticket.author
    assert graphql_ticket.assignees == rest_ticket.assignees
    assert graphql_ticket.labels == rest_ticket.labels
    assert graphql_ticket.url == rest_ticket.url
    assert graphql_ticket.created_at == rest_ticket.created_at
    assert graphql_ticket.updated_at == rest_ticket.updated_at


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_pr_head_base_fields_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR head and base dicts must be populated with ref/sha/repo_full_name."""
    project = _project()
    pr_node = _make_pr_node(
        number=5,
        head_ref="feature/my-feature",
        head_sha="deadbeef",
        head_repo="acme/backend",
        base_ref="main",
        base_sha="cafebabe",
    )

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.head == {"ref": "feature/my-feature", "sha": "deadbeef", "repo_full_name": "acme/backend"}
    assert pr.base == {"ref": "main", "sha": "cafebabe"}


def test_pr_draft_field_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """isDraft from GraphQL must map to PR.draft=True."""
    project = _project()
    pr_node = _make_pr_node(number=8, is_draft=True)

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.draft is True


def test_merged_pr_has_merged_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR node with merged=True must have status='merged'."""
    project = _project()
    pr_node = _make_pr_node(
        number=11, state="MERGED", merged=True, merged_at="2025-02-01T00:00:00Z"
    )

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.merged is True
    assert pr.status == "merged"


def test_batch_result_order_matches_input_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Results must be in the same order as the input project list."""
    projects = [_project(f"owner/repo{i}", f"p{i}") for i in range(5)]

    data_payload: dict = {}
    for i in range(5):
        data_payload[f"r{i}"] = {
            "issues": {"nodes": [_make_issue_node(number=i + 1, title=f"Issue {i}")]},
            "pullRequests": {"nodes": []},
        }

    body = {"data": data_payload}

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board(projects, token="tok")

    assert len(results) == 5
    for i, (result, project) in enumerate(zip(results, projects)):
        assert result.project is project
        assert result.tickets[0].id == str(i + 1)


def test_graphql_query_contains_owner_and_repo_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POST body sent to GraphQL must include variables with correct owner/repo."""
    project = _project("myorg/myrepo")
    captured: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.content)
        body = _make_graphql_body({
            "r0": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}
        })
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    fetch_open_board([project], token="tok")

    assert len(captured) == 1
    payload = json.loads(captured[0])
    variables = payload.get("variables", {})
    assert variables.get("owner0") == "myorg"
    assert variables.get("name0") == "myrepo"


def test_429_x_ratelimit_reset_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 with no Retry-After but x-ratelimit-reset → retry_after derived from reset epoch."""
    import time
    reset_ts = int(time.time()) + 90

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            content=b'{"message":"rate limited"}',
            headers={
                "Content-Type": "application/json",
                "x-ratelimit-reset": str(reset_ts),
            },
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(RateLimitError) as exc:
        fetch_open_board([_project()], token="tok")

    assert exc.value.status == 429
    assert exc.value.retry_after is not None
    assert abs(exc.value.retry_after - 90) <= 5


def test_403_rate_limit_no_reset_header_retry_after_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 + x-ratelimit-remaining=0 but no reset header → retry_after is None."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            content=b'{"message":"API rate limit exceeded"}',
            headers={
                "Content-Type": "application/json",
                "x-ratelimit-remaining": "0",
            },
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(RateLimitError) as exc:
        fetch_open_board([_project()], token="tok")

    assert exc.value.status == 403
    assert exc.value.retry_after is None


def test_500_raises_github_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic 500 from the GraphQL endpoint raises GitHubError(500)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=500,
            content=b'{"message":"Internal Server Error"}',
            headers={"Content-Type": "application/json"},
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        fetch_open_board([_project()], token="tok")

    assert exc.value.status == 500


# ---------------------------------------------------------------------------
# Regression: reviewRequests field (fix for ticket #106)
# ---------------------------------------------------------------------------


def test_requested_reviewers_extracted_from_review_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: reviewRequests GraphQL field maps to requested_reviewers correctly.

    Previously the query used the non-existent field 'requestedReviewers', which
    caused GitHub to return a GraphQL error and fail all GitHub projects in a batch.
    The fix switches to 'reviewRequests(first:20){nodes{requestedReviewer{...on User{login}}}}'.
    This test uses the corrected two-level structure and asserts only User-type
    reviewers (those with a login) appear in requested_reviewers.
    """
    project = _project()
    # Mix: two User reviewers and one non-User (Team), which the ...on User fragment
    # causes to produce an empty requestedReviewer object (no login key).
    pr_node = _make_pr_node(number=55, requested_reviewers=["alice", "bob"])
    # Manually inject a non-User reviewer node (Team: requestedReviewer has no login)
    pr_node["reviewRequests"]["nodes"].append({"requestedReviewer": {}})

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.requested_reviewers == ["alice", "bob"]


def test_non_user_reviewer_types_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-User reviewer types (Team, Mannequin) are silently excluded.

    When ...on User does not match, requestedReviewer is an empty object with
    no login key.  The parser must skip those nodes rather than raising KeyError
    or producing None/empty-string entries.
    """
    project = _project()
    # Build a PR node manually with only non-User reviewer entries
    pr_node = _make_pr_node(number=77, requested_reviewers=[])
    pr_node["reviewRequests"]["nodes"] = [
        {"requestedReviewer": {}},           # Team (no login)
        {"requestedReviewer": {}},           # Mannequin (no login)
    ]

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.requested_reviewers == []


def test_review_requests_key_absent_yields_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If reviewRequests key is missing entirely, requested_reviewers defaults to []."""
    project = _project()
    pr_node = _make_pr_node(number=88)
    # Remove the reviewRequests key to simulate an absent field
    del pr_node["reviewRequests"]

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.requested_reviewers == []


def test_review_requests_nodes_empty_yields_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If reviewRequests.nodes is empty, requested_reviewers is []."""
    project = _project()
    pr_node = _make_pr_node(number=99, requested_reviewers=[])

    body = _make_graphql_body({
        "r0": {
            "issues": {"nodes": []},
            "pullRequests": {"nodes": [pr_node]},
        }
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(body)

    _install_mock(monkeypatch, handler)
    results = fetch_open_board([project], token="tok")

    pr = results[0].pull_requests[0]
    assert pr.requested_reviewers == []
