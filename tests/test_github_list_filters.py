"""Tests for the extended `list_tickets` filter set (Plan 7).

Covers the routing between the cheap `/repos/.../issues` endpoint and the
search endpoint, plus the query-string construction details (label
quoting, date formatting, state-qualifier omission for `status="any"`,
sort fallthrough on both paths).

We use `httpx.MockTransport` to intercept HTTP calls and return canned
responses; the provider is monkey-patched so `_client(token)` returns a
client backed by our mock transport.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.base import TicketFilters
from lib_python_projects.providers.github import GitHubError, GitHubProvider


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _issue_payload(number: int, **overrides) -> dict:
    base = {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
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


# ---------- tests ------------------------------------------------------------


def test_default_filters_hit_issues_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only-default-filters must use the cheap `/issues` endpoint."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/issues", (
            f"expected /issues endpoint, got {req.url}"
        )
        params = dict(req.url.params)
        # Sort fallthrough on the legacy endpoint: native `sort`/`direction`.
        assert params["sort"] == "created"
        assert params["direction"] == "desc"
        assert params["state"] == "open"
        return _json([_issue_payload(1), _issue_payload(2)])

    seen = _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    tickets, has_more = provider.list_tickets(_project(), token="t", filters=TicketFilters())
    assert [t.id for t in tickets] == ["1", "2"]
    assert len(seen) == 1
    assert seen[0].url.path == "/repos/acme/backend/issues"


def test_not_labels_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`not_labels` is a Plan-7 filter — it must force the search endpoint."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues", (
            f"expected /search/issues, got {req.url}"
        )
        q = req.url.params["q"]
        # Excluded label appears with `-label:` prefix.
        assert "-label:bug" in q
        # Repo and issue qualifiers always present.
        assert "repo:acme/backend" in q
        assert "is:issue" in q
        # Search response shape differs: `{ "items": [...] }`.
        return _json({"items": [_issue_payload(5)], "total_count": 1})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    tickets, _ = provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(not_labels=["bug"]),
    )
    assert [t.id for t in tickets] == ["5"]


def test_label_with_spaces_is_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Labels containing whitespace must be wrapped in `"..."` for search."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues"
        q = req.url.params["q"]
        # Both inclusion and exclusion variants must quote when needed.
        assert 'label:"good first issue"' in q
        assert '-label:"help wanted"' in q
        return _json({"items": []})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(
            labels=["good first issue"],
            not_labels=["help wanted"],
        ),
    )


def test_created_after_formatted_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """`created_after` (ISO date) must render as `created:>=YYYY-MM-DD` in `q`."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues"
        q = req.url.params["q"]
        assert "created:>=2024-06-01" in q
        return _json({"items": []})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(created_after="2024-06-01"),
    )


def test_status_any_omits_state_qualifier_in_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Search API's `state:` qualifier only supports `open`/`closed`.

    For `status="any"` we must omit the qualifier entirely (otherwise the
    API returns zero results for the unrecognized value).
    """

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues"
        q = req.url.params["q"]
        assert "state:" not in q, f"unexpected state qualifier in q: {q!r}"
        return _json({"items": []})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    # Need to be on the search path — use `author` to force it.
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(status="any", author="bob"),
    )


def test_sort_fallthrough_on_issues_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sort args must turn into `sort=`/`direction=` on the legacy endpoint."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/issues"
        params = dict(req.url.params)
        assert params["sort"] == "comments"
        assert params["direction"] == "asc"
        return _json([])

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(sort_by="comments", sort_order="asc"),
    )


def test_sort_fallthrough_on_search_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sort args must turn into a `sort:<key>-<order>` qualifier on search.

    Note: the search endpoint takes its sort via the `q=` qualifier, NOT
    a separate `sort=` parameter (which is what the legacy endpoint uses).
    """

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues"
        params = dict(req.url.params)
        q = params["q"]
        assert "sort:comments-asc" in q
        # And it must NOT also appear as a separate `sort=` param.
        assert "sort" not in params or params.get("sort") is None
        return _json({"items": []})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(
            author="bob",  # force search path
            sort_by="comments",
            sort_order="asc",
        ),
    )


def test_search_text_still_routes_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy behavior: free-text `search` keeps routing to `/search/issues`.

    This regression-checks that adding Plan-7 routing didn't accidentally
    push search-text queries onto the legacy endpoint.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues"
        q = req.url.params["q"]
        assert q.startswith("login failure ") or "login failure" in q
        return _json({"items": []})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(search="login failure"),
    )


def test_empty_not_labels_does_not_route_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`not_labels=[]` must be treated as "not set" — stay on `/issues`."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/issues", (
            f"empty not_labels should not route to search; hit {req.url}"
        )
        return _json([])

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(not_labels=[]),
    )


def test_author_routes_to_search_with_qualifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """`author` is a Plan-7 filter — routes to search and emits `author:`."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search/issues"
        q = req.url.params["q"]
        assert "author:bob" in q
        return _json({"items": []})

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(author="bob"),
    )


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_tickets_nonpositive_limit_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError without any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        GitHubProvider().list_tickets(
            _project(),
            token="t",
            filters=TicketFilters(limit=bad_limit),
        )


def test_list_tickets_has_more_true_when_full_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_more is True when the API returns exactly per_page items."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Return exactly 2 items matching limit=2.
        return _json([_issue_payload(1), _issue_payload(2)])

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitHubProvider().list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(limit=2),
    )
    assert len(tickets) == 2
    assert has_more is True


def test_list_tickets_has_more_false_when_partial_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_more is False when the API returns fewer than per_page items."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Return 1 item when limit=5.
        return _json([_issue_payload(1)])

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitHubProvider().list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(limit=5),
    )
    assert len(tickets) == 1
    assert has_more is False


def test_list_tickets_pr_filtered_returns_full_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRs mixed into /issues pages must not shrink the result below limit.

    Page 1 returns [PR, issue, PR] — only 1 real issue.
    Page 2 returns [issue, issue] — 2 more real issues.
    With limit=2 the provider must paginate to collect 2 real issues
    and has_more must be True (page 2 was a full page of per_page=2).
    """
    pr_payload = _issue_payload(10, pull_request={"url": "https://github.com/acme/backend/pull/10"})

    pages: dict[str, list] = {
        "1": [pr_payload, _issue_payload(1), pr_payload],
        "2": [_issue_payload(2), _issue_payload(3)],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/issues"
        page_num = req.url.params.get("page", "1")
        return _json(pages.get(page_num, []))

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitHubProvider().list_tickets(
        _project(),
        token="t",
        filters=TicketFilters(limit=2),
    )
    assert len(tickets) == 2
    # The id assertions below implicitly prove PR items were excluded
    # (only real issue ids 1 and 2 appear, not the PR id 10).
    assert tickets[0].id == "1"
    assert tickets[1].id == "2"
    # Page 2 had exactly per_page=2 items, so has_more should be True
    # (we cannot tell whether more pages exist — the page was full).
    assert has_more is True


# ---------- Regression: bogus-assignee 422 (ticket #74) ----------------------


class TestListTicketsBogusAssignee:
    """GitHub returns 422 for unknown assignee on the /repos/.../issues path.

    The expected behaviour is: return ([], False) rather than raising
    GitHubError.  This mirrors the search endpoint which silently returns
    zero results for unknown assignees.
    """

    def test_bogus_assignee_422_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """list_tickets with an unknown assignee must return ([], False) not raise."""

        def handler(req: httpx.Request) -> httpx.Response:
            # Only the /issues paginating path (not /search) is exercised here.
            assert "/repos/" in str(req.url)
            return _json(
                {
                    "message": "Validation Failed",
                    "errors": [
                        {
                            "resource": "Issue",
                            "field": "assignee",
                            "code": "invalid",
                        }
                    ],
                },
                status_code=422,
            )

        _install_mock(monkeypatch, handler)
        provider = GitHubProvider()
        tickets, has_more = provider.list_tickets(
            _project(),
            token="t",
            filters=TicketFilters(assignee="ghost-user-xyz"),
        )
        assert tickets == []
        assert has_more is False

    def test_422_without_assignee_filter_still_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 422 not caused by an assignee filter must still propagate as GitHubError."""

        def handler(req: httpx.Request) -> httpx.Response:
            return _json(
                {"message": "Validation Failed", "errors": []},
                status_code=422,
            )

        _install_mock(monkeypatch, handler)
        provider = GitHubProvider()
        with pytest.raises(GitHubError) as exc:
            provider.list_tickets(
                _project(),
                token="t",
                filters=TicketFilters(),  # no assignee filter
            )
        assert exc.value.status == 422
