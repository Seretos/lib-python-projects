"""Tests for GitHub Projects v2 board support (ticket #118).

Covers `GitHubProvider.list_board_columns` and the `TicketFilters.board_column`
listing path added on top of the neutral `board` schema from #117. GraphQL
is mocked via `httpx.MockTransport`, following the pattern already used by
`test_github_list_filters.py` — the provider's module-level `_client` is
monkey-patched so both `_client(token)` REST calls and the `/graphql` POSTs
made through the same client are captured by one handler.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from lib_python_projects import AzureBoardsBinding, Board, GithubProjectsV2Binding, ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.base import BoardColumnSpec, TicketFilters
from lib_python_projects.providers.github import (
    GitHubError,
    GitHubProvider,
    PartialTicketCreateError,
)


# ---------- helpers ----------------------------------------------------------


def _board(
    columns: list[str],
    *,
    map_: dict[str, str] | None = None,
    owner: str | None = "acme-org",
    project_number: int | None = 7,
    status_field: str = "Status",
    iteration_field: str | None = None,
    auto_labels: dict | None = None,
) -> Board:
    kwargs: dict = {}
    if auto_labels is not None:
        kwargs["auto_labels"] = auto_labels
    return Board(
        columns=columns,
        binding=GithubProjectsV2Binding(
            kind="github-projects-v2",
            owner=owner,
            project_number=project_number,
            status_field=status_field,
            iteration_field=iteration_field,
            map=map_,
        ),
        **kwargs,
    )


def _project(board: Board | None = None) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        board=board,
    )


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


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _graphql_body(req: httpx.Request) -> dict:
    return json.loads(req.content.decode("utf-8"))


def _assert_brace_balanced(query: str) -> None:
    assert query.count("{") == query.count("}"), (
        f"unbalanced braces ({query.count('{')} '{{' vs "
        f"{query.count('}')} '}}'): {query!r}"
    )


def _owner_field(query: str) -> str:
    """Which owner field ('organization' or 'user') a GraphQL query uses."""
    return "organization" if "organization(login:" in query else "user"


def _is_columns_query(query: str) -> bool:
    return "options{id name}" in query


def _columns_response(owner_field: str, options: list[dict]) -> dict:
    return {
        "data": {
            owner_field: {
                "projectV2": {
                    "field": {
                        "id": "field-1",
                        "name": "Status",
                        "options": options,
                    }
                }
            }
        }
    }


def _items_response(
    owner_field: str,
    nodes: list[dict],
    *,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict:
    return {
        "data": {
            owner_field: {
                "projectV2": {
                    "items": {
                        "pageInfo": {
                            "hasNextPage": has_next_page,
                            "endCursor": end_cursor,
                        },
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def _org_not_resolved_response() -> dict:
    return {
        "data": {"organization": None},
        "errors": [
            {"message": "Could not resolve to an Organization with the login of 'acme-org'."}
        ],
    }


def _issue_node(
    number: int,
    *,
    status_name: str,
    typename: str = "Issue",
    title: str = "T",
    body: str = "",
    state: str = "OPEN",
    state_reason: str | None = None,
    author: str = "alice",
    assignees: list[str] | None = None,
    labels: list[str] | None = None,
    created_at: str = "2024-01-01T00:00:00Z",
    repository: str | None = "acme/backend",
) -> dict:
    return {
        "fieldValueByName": {"name": status_name, "optionId": f"opt-{status_name.lower()}"},
        "content": {
            "__typename": typename,
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "stateReason": state_reason,
            "url": f"https://github.com/acme/backend/issues/{number}",
            "createdAt": created_at,
            "updatedAt": "2024-01-02T00:00:00Z",
            "author": {"login": author},
            "assignees": {"nodes": [{"login": a} for a in (assignees or [])]},
            "labels": {"nodes": [{"name": lbl} for lbl in (labels or [])]},
            "repository": {"nameWithOwner": repository} if repository is not None else None,
        },
    }


# ---------- ticket #131: malformed GraphQL query regression ------------------
#
# GitHub's real GraphQL endpoint 400s on an unbalanced query document — the
# `httpx.MockTransport` used throughout this file returns canned JSON
# regardless of query text, so a brace-count bug is invisible to every
# behavioural test above/below. These tests assert on the query *string*
# itself, independent of the mock transport.


def test_board_columns_query_is_brace_balanced() -> None:
    _assert_brace_balanced(github_provider._board_columns_query("organization"))
    _assert_brace_balanced(github_provider._board_columns_query("user"))
    _assert_brace_balanced(github_provider._BOARD_COLUMNS_ORG_QUERY)
    _assert_brace_balanced(github_provider._BOARD_COLUMNS_USER_QUERY)


def test_board_items_query_is_brace_balanced() -> None:
    _assert_brace_balanced(github_provider._board_items_query("organization"))
    _assert_brace_balanced(github_provider._board_items_query("user"))
    _assert_brace_balanced(github_provider._BOARD_ITEMS_ORG_QUERY)
    _assert_brace_balanced(github_provider._BOARD_ITEMS_USER_QUERY)


# ---------- ticket #145: field-concatenation regression (`updatedAtauthor`) --
#
# The `_board_items_query` template used adjacent string literals
# `"...createdAt updatedAt"` and `"author{login}"` with no separating space,
# which Python concatenates into the single invalid GraphQL field
# `updatedAtauthor`. GitHub's real GraphQL endpoint rejects the whole query
# with a field-not-found error, breaking every column-filtered `list_tickets`
# call. The `httpx.MockTransport` used elsewhere in this file returns canned
# JSON regardless of query text, so this bug is invisible to the behavioural
# tests above/below unless we assert on the query *string* itself.


def test_board_items_query_has_no_field_concatenation_bug() -> None:
    for query in (
        github_provider._board_items_query("organization"),
        github_provider._board_items_query("user"),
        github_provider._BOARD_ITEMS_ORG_QUERY,
        github_provider._BOARD_ITEMS_USER_QUERY,
    ):
        assert "updatedAtauthor" not in query
        assert "updatedAt author" in query


def test_list_tickets_board_column_query_body_has_no_field_concatenation_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the query actually posted to `/graphql` by
    `list_tickets(filters.board_column=...)` must be well-formed, and the
    matching issue must come back — covers the org path directly."""
    board = _board(["Review"])
    seen_queries: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        seen_queries.append(body["query"])
        owner_field = _owner_field(body["query"])
        return _json(
            _items_response(owner_field, [_issue_node(1, status_name="Review")])
        )

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["1"]
    assert seen_queries
    for query in seen_queries:
        assert "updatedAtauthor" not in query
        assert "updatedAt author" in query


def test_list_tickets_board_column_falls_back_to_user_query_body_has_no_field_concatenation_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same regression check, but through the user-fallback path (org
    login unresolved) — both `_BOARD_ITEMS_ORG_QUERY` and
    `_BOARD_ITEMS_USER_QUERY` come from the same template."""
    board = _board(["Review"])
    seen_queries: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        seen_queries.append(body["query"])
        owner_field = _owner_field(body["query"])
        if owner_field == "organization":
            return _json(_org_not_resolved_response())
        return _json(
            _items_response("user", [_issue_node(9, status_name="Review")])
        )

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["9"]
    assert len(seen_queries) == 2
    for query in seen_queries:
        assert "updatedAtauthor" not in query
        assert "updatedAt author" in query


# ---------- list_board_columns ------------------------------------------------


def test_list_board_columns_explicit_map(monkeypatch: pytest.MonkeyPatch) -> None:
    board = _board(["Todo", "Doing", "Done"], map_={"Todo": "Backlog"})

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/graphql"
        body = _graphql_body(req)
        assert _is_columns_query(body["query"])
        owner_field = _owner_field(body["query"])
        return _json(
            _columns_response(
                owner_field,
                [
                    {"id": "opt-backlog", "name": "Backlog"},
                    {"id": "opt-doing", "name": "Doing"},
                    {"id": "opt-done", "name": "Done"},
                ],
            )
        )

    _install_mock(monkeypatch, handler)
    specs = GitHubProvider().list_board_columns(_project(board), "t")
    assert specs == [
        BoardColumnSpec(logical="Todo", native="Backlog", option_id="opt-backlog"),
        BoardColumnSpec(logical="Doing", native="Doing", option_id="opt-doing"),
        BoardColumnSpec(logical="Done", native="Done", option_id="opt-done"),
    ]


def test_list_board_columns_identity_fallback_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `map` — logical columns resolve to themselves, matched against
    the live board's option names case-insensitively."""
    board = _board(["Todo", "Doing", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        return _json(
            _columns_response(
                owner_field,
                [
                    {"id": "o1", "name": "todo"},
                    {"id": "o2", "name": "Doing"},
                    {"id": "o3", "name": "DONE"},
                ],
            )
        )

    _install_mock(monkeypatch, handler)
    specs = GitHubProvider().list_board_columns(_project(board), "t")
    assert specs == [
        BoardColumnSpec(logical="Todo", native="Todo", option_id="o1"),
        BoardColumnSpec(logical="Doing", native="Doing", option_id="o2"),
        BoardColumnSpec(logical="Done", native="Done", option_id="o3"),
    ]


def test_list_board_columns_no_board_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when project has no board")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="no 'board' configuration"):
        GitHubProvider().list_board_columns(_project(None), "t")


def test_list_board_columns_wrong_binding_kind_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = Board(
        columns=["Todo"],
        binding=AzureBoardsBinding(kind="azure-boards"),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for a non-GitHub binding")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not 'github-projects-v2'"):
        GitHubProvider().list_board_columns(_project(board), "t")


def test_list_board_columns_missing_owner_or_number_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo"], owner=None, project_number=None)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected without owner/project_number")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="owner.*project_number|missing"):
        GitHubProvider().list_board_columns(_project(board), "t")


def test_list_board_columns_mapped_option_missing_from_live_board_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`map` resolves "Todo" to "Backlog", but the live board has no such
    option — must raise, not silently drop the column."""
    board = _board(["Todo", "Done"], map_={"Todo": "Backlog"})

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        return _json(
            _columns_response(
                owner_field,
                [{"id": "o1", "name": "Done"}],  # no "Backlog" option
            )
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not present on the live"):
        GitHubProvider().list_board_columns(_project(board), "t")


# ---------- list_tickets(board_column=...) ------------------------------------


def test_list_tickets_board_column_returns_only_matching_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Review", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        assert not _is_columns_query(body["query"])
        assert body["variables"]["fieldName"] == "Status"
        nodes = [
            _issue_node(1, status_name="Todo"),
            _issue_node(2, status_name="Review"),
            _issue_node(3, status_name="Done"),
            # A PR sitting in the "Review" column must be excluded.
            _issue_node(4, status_name="Review", typename="PullRequest"),
            # A draft issue sitting in "Review" must also be excluded.
            _issue_node(5, status_name="Review", typename="DraftIssue"),
        ]
        return _json(_items_response(owner_field, nodes))

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["2"]
    assert has_more is False


# ---------- ticket #174: cross-repo board leak regression --------------------
#
# Projects v2 boards can span multiple repos under the same owner. A
# `filters.board_column` listing for project `acme/backend` must only
# return issues that actually live in `acme/backend`, even when another
# repo's issue is sitting in the same board column.


def test_list_tickets_board_column_excludes_foreign_repo_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        nodes = [
            _issue_node(1, status_name="Review"),  # acme/backend — in-repo
            _issue_node(2, status_name="Review", repository="acme/other-service"),
        ]
        return _json(_items_response(owner_field, nodes))

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["1"]


def test_list_tickets_board_column_repo_match_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub logins/repo names are case-insensitive, so a `repository`
    differing only in case from the project's `owner/repo` must still be
    treated as the same repo and INCLUDED."""
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        nodes = [_issue_node(1, status_name="Review", repository="ACME/Backend")]
        return _json(_items_response(owner_field, nodes))

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["1"]


def test_list_tickets_board_column_excludes_issue_with_missing_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node whose content has no (or a null) `repository` must be
    dropped rather than assumed to belong to the requesting project."""
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        nodes = [
            _issue_node(1, status_name="Review"),
            _issue_node(2, status_name="Review", repository=None),
        ]
        return _json(_items_response(owner_field, nodes))

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["1"]


def test_list_tickets_board_column_uses_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """The logical column is resolved via `Board.resolve()` (map wins)
    before being matched against the live field value's name."""
    board = _board(["Todo", "Review", "Done"], map_={"Review": "In Review"})

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        nodes = [
            _issue_node(1, status_name="In Review"),
            _issue_node(2, status_name="Review"),  # unmapped literal — must NOT match
        ]
        return _json(_items_response(owner_field, nodes))

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["1"]


def test_list_tickets_board_column_combined_with_labels_assignee_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        nodes = [
            _issue_node(
                1, status_name="Review", labels=["bug"], assignees=["bob"],
                state="OPEN",
            ),
            _issue_node(
                2, status_name="Review", labels=["docs"], assignees=["bob"],
                state="OPEN",
            ),
            _issue_node(
                3, status_name="Review", labels=["bug"], assignees=["carol"],
                state="OPEN",
            ),
            _issue_node(
                4, status_name="Review", labels=["bug"], assignees=["bob"],
                state="CLOSED", state_reason="COMPLETED",
            ),
        ]
        return _json(_items_response(owner_field, nodes))

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board),
        token="t",
        filters=TicketFilters(
            board_column="Review",
            labels=["bug"],
            assignee="bob",
            states=["open"],
        ),
    )
    # Only ticket 1 satisfies column + label + assignee + state simultaneously.
    assert [t.id for t in tickets] == ["1"]


def test_list_tickets_board_column_empty_column_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        return _json(_items_response(owner_field, []))

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert tickets == []
    assert has_more is False


def test_list_tickets_board_column_with_search_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when search+board_column combine")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="board_column cannot be combined with search"):
        GitHubProvider().list_tickets(
            _project(board),
            token="t",
            filters=TicketFilters(board_column="Review", search="login failure"),
        )


def test_list_tickets_board_column_with_area_path_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when area_path+board_column combine")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="area_path is not supported on GitHub"):
        GitHubProvider().list_tickets(
            _project(board),
            token="t",
            filters=TicketFilters(board_column="Review", area_path="MyProj\\Team A"),
        )


def test_list_tickets_board_column_no_board_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when project has no board")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="no 'board' configuration"):
        GitHubProvider().list_tickets(
            _project(None), token="t", filters=TicketFilters(board_column="Review"),
        )


def test_list_tickets_board_column_wrong_binding_kind_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = Board(columns=["Review"], binding=AzureBoardsBinding(kind="azure-boards"))

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for a non-GitHub binding")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not 'github-projects-v2'"):
        GitHubProvider().list_tickets(
            _project(board), token="t", filters=TicketFilters(board_column="Review"),
        )


def test_list_tickets_board_column_not_in_columns_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Doing", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an unknown column")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="is not one of this project's board columns"):
        GitHubProvider().list_tickets(
            _project(board), token="t", filters=TicketFilters(board_column="Bogus"),
        )


def test_list_tickets_board_column_paginates_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Review"])
    calls: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        after = body["variables"].get("after")
        calls.append(after)
        if after is None:
            return _json(
                _items_response(
                    owner_field,
                    [_issue_node(1, status_name="Review")],
                    has_next_page=True,
                    end_cursor="cursor-1",
                )
            )
        assert after == "cursor-1"
        return _json(
            _items_response(owner_field, [_issue_node(2, status_name="Review")])
        )

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review", limit=30),
    )
    assert [t.id for t in tickets] == ["1", "2"]
    assert has_more is False
    assert calls == [None, "cursor-1"]


def test_list_tickets_board_column_sorts_after_exhausting_all_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test (#118 review fix): the loop must fetch every page
    before sorting/slicing to `limit` — not stop as soon as the running
    `matched` count crosses `limit`.

    Page 1 alone already has 2 matches, crossing `limit=1`. Under the old
    early-exit code (`break` on `len(matched) > filters.limit`), page 2 is
    never fetched, so its issue — which has the newest `created_at` and
    must sort first under the default `sort_by="created"`,
    `sort_order="desc"` — is silently dropped from consideration. This
    test fails against that code (it would return "2", the newest of the
    page-1-only items) and passes once all pages are exhausted before
    sorting.
    """
    board = _board(["Review"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        after = body["variables"].get("after")
        if after is None:
            return _json(
                _items_response(
                    owner_field,
                    [
                        _issue_node(1, status_name="Review", created_at="2024-01-02T00:00:00Z"),
                        _issue_node(2, status_name="Review", created_at="2024-01-03T00:00:00Z"),
                    ],
                    has_next_page=True,
                    end_cursor="cursor-1",
                )
            )
        assert after == "cursor-1"
        return _json(
            _items_response(
                owner_field,
                # Newest of all — must win under sort_order="desc", but
                # only if the loop actually reaches this second page.
                [_issue_node(3, status_name="Review", created_at="2024-01-10T00:00:00Z")],
            )
        )

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitHubProvider().list_tickets(
        _project(board),
        token="t",
        filters=TicketFilters(board_column="Review", limit=1, sort_by="created", sort_order="desc"),
    )
    assert [t.id for t in tickets] == ["3"]
    assert has_more is True


# ---------- owner auto-detect (organization vs user) --------------------------


def test_list_board_columns_org_query_succeeds_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        assert _owner_field(body["query"]) == "organization"
        return _json(
            _columns_response("organization", [{"id": "o1", "name": "Todo"}])
        )

    seen = _install_mock(monkeypatch, handler)
    specs = GitHubProvider().list_board_columns(_project(board), "t")
    assert specs == [BoardColumnSpec(logical="Todo", native="Todo", option_id="o1")]
    assert len(seen) == 1, "org succeeding directly must not trigger a user fallback"


def test_list_board_columns_falls_back_to_user_when_org_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo"])
    call_owner_fields: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        call_owner_fields.append(owner_field)
        if owner_field == "organization":
            return _json(_org_not_resolved_response())
        return _json(_columns_response("user", [{"id": "o1", "name": "Todo"}]))

    seen = _install_mock(monkeypatch, handler)
    specs = GitHubProvider().list_board_columns(_project(board), "t")
    assert specs == [BoardColumnSpec(logical="Todo", native="Todo", option_id="o1")]
    assert call_owner_fields == ["organization", "user"]
    assert len(seen) == 2, "both organization and user GraphQL calls must fire"


def test_list_tickets_board_column_falls_back_to_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Review"])
    call_owner_fields: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = _graphql_body(req)
        owner_field = _owner_field(body["query"])
        call_owner_fields.append(owner_field)
        if owner_field == "organization":
            return _json(_org_not_resolved_response())
        return _json(
            _items_response("user", [_issue_node(9, status_name="Review")])
        )

    _install_mock(monkeypatch, handler)
    tickets, _ = GitHubProvider().list_tickets(
        _project(board), token="t", filters=TicketFilters(board_column="Review"),
    )
    assert [t.id for t in tickets] == ["9"]
    assert call_owner_fields == ["organization", "user"]


# ---------- ticket #123: custom_fields read/write via Projects v2 ------------


def _rest_issue_payload(number: int, **overrides) -> dict:
    base = {
        "number": number,
        "node_id": f"issue-node-{number}",
        "title": "T",
        "body": "",
        "state": "open",
        "user": {"login": "a"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_get_ticket_include_custom_fields_returns_populated_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            body = _graphql_body(req)
            assert "repository(owner:$owner,name:$repo)" in body["query"]
            assert body["variables"] == {
                "owner": "acme", "repo": "backend", "number": 42,
            }
            return _json({
                "data": {
                    "repository": {
                        "issue": {
                            "projectItems": {
                                "nodes": [
                                    {
                                        "project": {"number": 7},
                                        "fieldValues": {
                                            "nodes": [
                                                {"name": "Done", "field": {"name": "Status"}},
                                                {"number": 3, "field": {"name": "Points"}},
                                                {"text": "note", "field": {"name": "Notes"}},
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            })
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields == {"Status": "Done", "Points": 3, "Notes": "note"}


def test_get_ticket_include_custom_fields_no_item_on_board_returns_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding is configured but the issue has no item on that project ->
    `custom_fields = {}`, not `None`."""
    board = _board(["Todo", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            return _json({
                "data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}
            })
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields == {}


def test_get_ticket_include_custom_fields_no_board_returns_none_no_graphql_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No board configured -> `custom_fields` stays `None` (never raises on
    read) and no GraphQL request is made at all."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(None), "t", "42", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields is None
    assert ticket.milestone is None
    assert all(r.url.path != "/graphql" for r in seen)


def test_get_ticket_without_include_custom_fields_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `include_custom_fields=False` leaves `custom_fields` at its
    `None` default even with a board configured.

    A board-bound `get_ticket` now (ticket #151) always issues the
    `projectItems` query to populate `milestone` regardless of
    `include_custom_fields` — so the mocked `/graphql` route must be
    present, but `custom_fields` itself stays `None` since it wasn't
    requested.
    """
    board = _board(["Todo"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            return _json({
                "data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}
            })
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False,
    )
    assert ticket.custom_fields is None
    assert ticket.milestone is None


def test_create_ticket_custom_fields_writes_via_project_v2_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                assert variables == {
                    "projectId": "proj-node-id", "contentId": "issue-node-99",
                }
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                field_name = variables["fieldName"]
                if field_name == "Status":
                    return _json({"data": {owner_field: {"projectV2": {"field": {
                        "id": "field-status", "name": "Status",
                        "options": [{"id": "opt-done", "name": "Done"}],
                    }}}}})
                if field_name == "Points":
                    return _json({"data": {owner_field: {"projectV2": {"field": {
                        "id": "field-points", "name": "Points",
                    }}}}})
                raise AssertionError(f"unexpected fieldName {field_name!r}")
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
        custom_fields={"Status": "Done", "Points": 3},
    )
    assert ticket.id == "99"
    update_calls = {
        v["fieldId"]: v["value"] for q, v in calls
        if "updateProjectV2ItemFieldValue" in q
    }
    assert update_calls == {
        "field-status": {"singleSelectOptionId": "opt-done"},
        "field-points": {"number": 3},
    }


def test_create_ticket_custom_fields_single_select_case_insensitive_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_project_v2_field_value_input` matches single-select option names
    case-insensitively (ticket #123 settled design point): a differently
    cased value (`"done"`) must still resolve to the live option named
    `"Done"`'s `optionId`, not raise 'not a valid option'."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                return _json({"data": {owner_field: {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
        custom_fields={"Status": "done"},
    )
    assert ticket.id == "99"
    update_calls = {
        v["fieldId"]: v["value"] for q, v in calls
        if "updateProjectV2ItemFieldValue" in q
    }
    assert update_calls == {"field-status": {"singleSelectOptionId": "opt-done"}}


def test_create_ticket_custom_fields_no_board_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No board configured + non-empty custom_fields -> ValueError naming
    the missing board config, before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when board is not configured")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="github-projects-v2"):
        GitHubProvider().create_ticket(
            _project(None), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"},
        )


def test_create_ticket_custom_fields_unmatched_single_select_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(5))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                return _json({"data": {owner_field: {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not a valid option"):
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Bogus"},
        )


def test_get_ticket_include_custom_fields_non_projects_v2_binding_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board configured but bound to a non-`github-projects-v2` provider
    (e.g. Azure Boards) -> `custom_fields` stays `None`, no GraphQL call —
    same "not applicable" semantics as no board at all."""
    board = Board(
        columns=["Todo", "Done"],
        binding=AzureBoardsBinding(kind="azure-boards", team="T", board="Stories"),
    )
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields is None
    assert all(r.url.path != "/graphql" for r in seen)


def test_get_ticket_include_custom_fields_missing_owner_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`github-projects-v2` binding present but missing `owner` (or
    `project_number`) -> `custom_fields` stays `None`, no GraphQL call."""
    board = _board(["Todo", "Done"], owner=None, project_number=None)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields is None
    assert all(r.url.path != "/graphql" for r in seen)


def test_create_ticket_custom_fields_non_projects_v2_binding_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board configured but bound to a non-`github-projects-v2` provider
    (e.g. Azure Boards) + non-empty custom_fields -> ValueError naming the
    missing 'github-projects-v2' config, before any HTTP call — mirrors the
    no-board case."""
    board = Board(
        columns=["Todo", "Done"],
        binding=AzureBoardsBinding(kind="azure-boards", team="T", board="Stories"),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when binding isn't github-projects-v2")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="github-projects-v2"):
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"},
        )


def test_create_ticket_custom_fields_missing_project_number_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`github-projects-v2` binding present but missing `project_number`
    (or `owner`) + non-empty custom_fields -> ValueError, before any HTTP
    call."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=None)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when project_number is missing")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="github-projects-v2"):
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"},
        )


# ---------- ticket #131: partial-failure `create_ticket(custom_fields=...)` --
#
# `_write_custom_fields_to_board` can fail after the REST issue already
# exists (project-id resolve, `addProjectV2ItemById`, or a per-field
# `updateProjectV2ItemFieldValue`). The issue is never rolled back (real
# deletion needs elevated GraphQL rights and is destructive/irreversible),
# so the failure must surface as a `PartialTicketCreateError` carrying the
# already-created issue's identity as structured attributes, not just a
# `GitHubError` a caller has to string-parse.


def test_create_ticket_board_write_project_id_resolve_failure_raises_partial_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The project-id resolve GraphQL call (the very first board-write
    step) fails with a GraphQL error -> `PartialTicketCreateError`, not a
    bare `GitHubError` and not a silently-dropped issue."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query = body["query"]
            if "projectV2(number:$number){id}" in query:
                return _json({
                    "data": {"organization": None},
                    "errors": [{"message": "something went wrong resolving the project"}],
                })
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(PartialTicketCreateError) as excinfo:
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"},
        )
    exc = excinfo.value
    assert isinstance(exc, GitHubError), (
        "PartialTicketCreateError must subclass GitHubError so existing "
        "'except GitHubError' callers keep working"
    )
    assert exc.issue_number == 99
    assert exc.issue_url == "https://github.com/acme/backend/issues/99"
    assert exc.issue_node_id == "issue-node-99"
    assert "#99" in str(exc)


def test_create_ticket_board_write_add_item_failure_raises_partial_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`addProjectV2ItemById` (adding the already-created issue to the
    board) fails -> `PartialTicketCreateError`; the REST issue POST still
    happened (documented partial-success reality — no rollback)."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)
    rest_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            rest_calls.append(path)
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query = body["query"]
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
            if "addProjectV2ItemById" in query:
                return _json({
                    "data": {"addProjectV2ItemById": None},
                    "errors": [{"message": "could not add item to project"}],
                })
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(PartialTicketCreateError) as excinfo:
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"},
        )
    exc = excinfo.value
    assert isinstance(exc, GitHubError)
    assert exc.issue_number == 99
    assert exc.issue_url == "https://github.com/acme/backend/issues/99"
    assert "#99" in str(exc)
    assert rest_calls == ["/repos/acme/backend/issues"], (
        "the REST issue POST must actually have happened — the created "
        "issue is real, not rolled back"
    )


def test_create_ticket_board_write_field_update_failure_raises_partial_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The item is successfully added to the board, but a per-field
    `updateProjectV2ItemFieldValue` mutation fails -> `PartialTicketCreateError`
    (same handler shape as the earlier-stage failures above)."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query = body["query"]
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                return _json({"data": {owner_field: {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {"updateProjectV2ItemFieldValue": None},
                    "errors": [{"message": "could not update field value"}],
                })
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(PartialTicketCreateError) as excinfo:
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"},
        )
    exc = excinfo.value
    assert isinstance(exc, GitHubError)
    assert exc.issue_number == 99
    assert exc.issue_url == "https://github.com/acme/backend/issues/99"
    assert "#99" in str(exc)


def test_create_ticket_missing_node_id_raises_plain_github_error_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `node_id`-missing branch is checked before any board
    interaction, so it must still raise a plain `GitHubError` — not the
    new `PartialTicketCreateError` subclass (there is no board write to
    have partially failed)."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            payload = _rest_issue_payload(99)
            del payload["node_id"]
            return _json(payload)
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as excinfo:
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"},
        )
    assert not isinstance(excinfo.value, PartialTicketCreateError)
    assert "node_id" in str(excinfo.value)


def test_create_ticket_custom_fields_none_or_empty_is_unaffected_by_partial_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`custom_fields=None`/`{}` stays a silent no-op — no board GraphQL
    call is ever attempted, so the new try/except around the board write
    never engages."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            raise AssertionError("no GraphQL call expected when custom_fields is empty")
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket_none = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
        custom_fields=None,
    )
    assert ticket_none.id == "99"

    ticket_empty = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
        custom_fields={},
    )
    assert ticket_empty.id == "99"


# ---------- ticket #145: update_ticket(custom_fields=...) — write-side parity
#
# `create_ticket` has supported `custom_fields` since #123; `update_ticket`
# did not, so a ticket's board column/status could never be changed after
# creation through this provider. These tests mirror the `create_ticket`
# custom_fields suite above, adapted for the update path: the issue already
# exists (a `GET` fetches it), the REST write is a `PATCH` (which may end up
# empty if `custom_fields` is the only thing changing), and a failed board
# write raises a plain `GitHubError` — not `PartialTicketCreateError` (see
# module docstring in the plan: no new partial-failure type on the update
# path).
#
# The current issue is given the `ai-generated` label so `update_ticket`'s
# unrelated "is this AI-generated / needs ai-modified label" bookkeeping
# doesn't add incidental label mutations to the PATCH payload, keeping each
# test focused on the custom_fields behaviour under test.


def _ai_issue_payload(number: int, **overrides) -> dict:
    overrides.setdefault("labels", [{"name": "ai-generated"}])
    return _rest_issue_payload(number, **overrides)


def test_update_ticket_custom_fields_writes_via_project_v2_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                assert variables == {
                    "projectId": "proj-node-id", "contentId": "issue-node-42",
                }
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                field_name = variables["fieldName"]
                if field_name == "Status":
                    return _json({"data": {owner_field: {"projectV2": {"field": {
                        "id": "field-status", "name": "Status",
                        "options": [{"id": "opt-done", "name": "Done"}],
                    }}}}})
                if field_name == "Points":
                    return _json({"data": {owner_field: {"projectV2": {"field": {
                        "id": "field-points", "name": "Points",
                    }}}}})
                raise AssertionError(f"unexpected fieldName {field_name!r}")
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42",
        custom_fields={"Status": "Done", "Points": 3},
    )
    assert ticket.id == "42"
    update_calls = {
        v["fieldId"]: v["value"] for q, v in calls
        if "updateProjectV2ItemFieldValue" in q
    }
    assert update_calls == {
        "field-status": {"singleSelectOptionId": "opt-done"},
        "field-points": {"number": 3},
    }


def test_update_ticket_custom_fields_single_select_case_insensitive_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity with `create_ticket`: a differently-cased value (`"done"`)
    still resolves to the live option named `"Done"`'s `optionId`."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                return _json({"data": {owner_field: {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"Status": "done"},
    )
    assert ticket.id == "42"
    update_calls = {
        v["fieldId"]: v["value"] for q, v in calls
        if "updateProjectV2ItemFieldValue" in q
    }
    assert update_calls == {"field-status": {"singleSelectOptionId": "opt-done"}}


def test_update_ticket_custom_fields_combined_with_status_and_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single REST `PATCH` lands `title`/`status` AND the board write
    fires — the two are not mutually exclusive."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)
    patch_payloads: list[dict] = []
    board_write_seen = {"add_item": False, "update_field": False}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42, title="old title"))
        if req.method == "PATCH" and path.endswith("/issues/42"):
            patch_payloads.append(json.loads(req.content.decode("utf-8")))
            return _json(_ai_issue_payload(42, title="new title", state="closed"))
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            if "addProjectV2ItemById" in query:
                board_write_seen["add_item"] = True
                assert variables["contentId"] == "issue-node-42"
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                board_write_seen["update_field"] = True
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                return _json({"data": {owner_field: {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42",
        title="new title",
        status="closed:completed",
        custom_fields={"Status": "Done"},
    )
    assert ticket.id == "42"
    assert patch_payloads == [{
        "title": "new title", "state": "closed", "state_reason": "completed",
    }]
    assert board_write_seen == {"add_item": True, "update_field": True}


def test_update_ticket_custom_fields_none_or_empty_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`custom_fields=None`/`{}` performs no GraphQL call at all — same
    silent no-op contract as `create_ticket`."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if path == "/graphql":
            raise AssertionError("no GraphQL call expected when custom_fields is empty")
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket_none = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields=None,
    )
    assert ticket_none.id == "42"

    ticket_empty = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={},
    )
    assert ticket_empty.id == "42"


def test_update_ticket_status_reopen_without_custom_fields_does_not_touch_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #175: `status="open"` alone (no `custom_fields`) must only
    PATCH the REST issue state/state_reason — it must never write the
    Projects-v2 board Status field, even when the ticket was previously
    moved to a terminal column like "Done". There is no configured
    default/open column to reset it to, so `update_ticket` leaves the
    board column untouched; callers who want that must pass
    `custom_fields={"Status": "<column>"}` explicitly (see the updated
    docstring)."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42, state="closed"))
        if req.method == "PATCH" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42, state="open"))
        if path == "/graphql":
            raise AssertionError(
                "no GraphQL/board call expected for a status-only reopen "
                "without custom_fields"
            )
        raise AssertionError(f"unexpected request {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", status="open",
    )
    assert ticket.status == "open"

    patch_requests = [r for r in seen if r.method == "PATCH"]
    assert len(patch_requests) == 1
    patch_payload = json.loads(patch_requests[0].content.decode("utf-8"))
    assert patch_payload["state"] == "open"

    assert not any(r.url.path == "/graphql" for r in seen)


def test_update_ticket_custom_fields_no_board_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No board configured + non-empty custom_fields -> ValueError naming
    the missing board config, before any HTTP call (including the GET)."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when board is not configured")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="github-projects-v2"):
        GitHubProvider().update_ticket(
            _project(None), "t", "42", custom_fields={"Status": "Done"},
        )


def test_update_ticket_custom_fields_non_projects_v2_binding_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = Board(
        columns=["Todo", "Done"],
        binding=AzureBoardsBinding(kind="azure-boards", team="T", board="Stories"),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for a non-GitHub binding")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="github-projects-v2"):
        GitHubProvider().update_ticket(
            _project(board), "t", "42", custom_fields={"Status": "Done"},
        )


def test_update_ticket_custom_fields_missing_owner_or_project_number_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], owner="acme-org", project_number=None)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when project_number is missing")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="github-projects-v2"):
        GitHubProvider().update_ticket(
            _project(board), "t", "42", custom_fields={"Status": "Done"},
        )


def test_update_ticket_custom_fields_unmatched_single_select_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if path == "/graphql":
            body = _graphql_body(req)
            query = body["query"]
            if "ProjectV2FieldCommon" in query:
                owner_field = _owner_field(query)
                return _json({"data": {owner_field: {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not a valid option"):
        GitHubProvider().update_ticket(
            _project(board), "t", "42", custom_fields={"Status": "Bogus"},
        )


def test_update_ticket_custom_fields_ticket_not_found_still_404s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `custom_fields` update on a non-existent ticket still gets the
    existing 404 remap — validated before the GET, so the 404 comes from
    the GET itself once binding validation passes."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/999"):
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError, match="not found"):
        GitHubProvider().update_ticket(
            _project(board), "t", "999", custom_fields={"Status": "Done"},
        )


def test_update_ticket_board_write_failure_raises_github_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REST `PATCH` succeeds (title lands), but the board mutation
    fails -> plain `GitHubError`, not `PartialTicketCreateError` — the
    update path deliberately has no partial-failure exception type."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42, title="old title"))
        if req.method == "PATCH" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42, title="new title"))
        if path == "/graphql":
            body = _graphql_body(req)
            query = body["query"]
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
            if "addProjectV2ItemById" in query:
                return _json({
                    "data": {"addProjectV2ItemById": None},
                    "errors": [{"message": "could not add item to project"}],
                })
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as excinfo:
        GitHubProvider().update_ticket(
            _project(board), "t", "42",
            title="new title", custom_fields={"Status": "Done"},
        )
    assert not isinstance(excinfo.value, PartialTicketCreateError)


# ---------- ticket #151: Ticket.milestone read/write via the iteration field -


def _iteration_field_response(owner_field: str, iterations: list[dict], completed: list[dict] | None = None) -> dict:
    return {
        "data": {
            owner_field: {
                "projectV2": {
                    "field": {
                        "id": "field-sprint", "name": "Sprint",
                        "configuration": {
                            "iterations": iterations,
                            "completedIterations": completed or [],
                        },
                    }
                }
            }
        }
    }


def test_get_ticket_milestone_populated_from_iteration_field_auto_detect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `iteration_field` configured -> the first iteration-typed node
    in `fieldValues` wins (auto-detect by node order)."""
    board = _board(["Todo", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            body = _graphql_body(req)
            assert body["variables"] == {
                "owner": "acme", "repo": "backend", "number": 42,
            }
            return _json({
                "data": {
                    "repository": {
                        "issue": {
                            "projectItems": {
                                "nodes": [
                                    {
                                        "project": {"number": 7},
                                        "fieldValues": {
                                            "nodes": [
                                                {"name": "Done", "field": {"name": "Status"}},
                                                {"title": "Sprint 3", "field": {"name": "Sprint"}},
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            })
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False,
    )
    assert ticket.milestone == "Sprint 3"


def test_get_ticket_milestone_matches_configured_iteration_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`binding.iteration_field` set -> matches that field name exactly,
    even when a different iteration-typed field appears earlier."""
    board = _board(["Todo", "Done"], iteration_field="Release Train")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            return _json({
                "data": {
                    "repository": {
                        "issue": {
                            "projectItems": {
                                "nodes": [
                                    {
                                        "project": {"number": 7},
                                        "fieldValues": {
                                            "nodes": [
                                                {"title": "Sprint 3", "field": {"name": "Sprint"}},
                                                {"title": "R12", "field": {"name": "Release Train"}},
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            })
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False,
    )
    assert ticket.milestone == "R12"


def test_get_ticket_milestone_none_when_no_item_on_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board bound but the issue has no item on that project ->
    `milestone = None`, not an error."""
    board = _board(["Todo", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            return _json(
                {"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}}
            )
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False,
    )
    assert ticket.milestone is None


def test_get_ticket_milestone_and_custom_fields_share_single_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board bound + `include_custom_fields=True` -> a single
    `projectItems` round-trip feeds both `milestone` and `custom_fields`
    (no double-query)."""
    board = _board(["Todo", "Done"])
    graphql_calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            body = _graphql_body(req)
            graphql_calls.append(body)
            return _json({
                "data": {
                    "repository": {
                        "issue": {
                            "projectItems": {
                                "nodes": [
                                    {
                                        "project": {"number": 7},
                                        "fieldValues": {
                                            "nodes": [
                                                {"name": "Done", "field": {"name": "Status"}},
                                                {"title": "Sprint 3", "field": {"name": "Sprint"}},
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            })
        if path.endswith("/issues/42"):
            return _json(_rest_issue_payload(42))
        if path.endswith("/issues/42/comments"):
            return _json([])
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        _project(board), "t", "42", include_relations=False, include_custom_fields=True,
    )
    assert ticket.milestone == "Sprint 3"
    assert ticket.custom_fields == {"Status": "Done", "Sprint": "Sprint 3"}
    assert len(graphql_calls) == 1


def test_create_ticket_milestone_writes_via_iteration_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        iteration_field="Sprint",
    )
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2IterationField" in query:
                owner_field = _owner_field(query)
                return _json(_iteration_field_response(
                    owner_field,
                    [{"id": "iter-3", "title": "Sprint 3"}],
                    [{"id": "iter-2", "title": "Sprint 2"}],
                ))
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
        milestone="Sprint 3",
    )
    assert ticket.id == "99"
    update_calls = [v for q, v in calls if "updateProjectV2ItemFieldValue" in q]
    assert update_calls == [{
        "projectId": "proj-node-id", "itemId": "item-1",
        "fieldId": "field-sprint", "value": {"iterationId": "iter-3"},
    }]


def test_create_ticket_milestone_matches_completed_iteration_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        iteration_field="Sprint",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query = body["query"]
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2IterationField" in query:
                owner_field = _owner_field(query)
                return _json(_iteration_field_response(
                    owner_field, [], [{"id": "iter-2", "title": "Sprint 2"}],
                ))
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
        milestone="sprint 2",
    )
    assert ticket.id == "99"


def test_create_ticket_milestone_no_iteration_field_configured_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board bound but `iteration_field` unset -> `ValueError` naming the
    missing config, before any HTTP call."""
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when iteration_field is unset")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="iteration_field"):
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            milestone="Sprint 3",
        )


def test_create_ticket_milestone_no_board_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when board is not configured")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="github-projects-v2"):
        GitHubProvider().create_ticket(
            _project(None), "t", title="hi", body="b", labels=[], assignees=[],
            milestone="Sprint 3",
        )


def test_create_ticket_milestone_unmatched_title_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        iteration_field="Sprint",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = _graphql_body(req)
            query = body["query"]
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "ProjectV2IterationField" in query:
                owner_field = _owner_field(query)
                return _json(_iteration_field_response(
                    owner_field, [{"id": "iter-3", "title": "Sprint 3"}],
                ))
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not a valid iteration"):
        GitHubProvider().create_ticket(
            _project(board), "t", title="hi", body="b", labels=[], assignees=[],
            milestone="does-not-exist",
        )


def test_create_ticket_milestone_omitted_issues_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`milestone=` omitted (`_UNSET`) issues no board write at all, even
    with a board + `iteration_field` fully configured."""
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        iteration_field="Sprint",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
    )
    assert ticket.id == "99"


def test_update_ticket_milestone_writes_via_iteration_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        iteration_field="Sprint",
    )
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "PATCH" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42, title="new title"))
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2IterationField" in query:
                owner_field = _owner_field(query)
                return _json(_iteration_field_response(
                    owner_field, [{"id": "iter-4", "title": "Sprint 4"}],
                ))
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", title="new title", milestone="Sprint 4",
    )
    assert ticket.id == "42"
    update_calls = [v for q, v in calls if "updateProjectV2ItemFieldValue" in q]
    assert update_calls == [{
        "projectId": "proj-node-id", "itemId": "item-1",
        "fieldId": "field-sprint", "value": {"iterationId": "iter-4"},
    }]


def test_update_ticket_milestone_none_clears_via_clear_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`milestone=None` on update clears the iteration field via
    `clearProjectV2ItemFieldValue` — there's no "clear" input shape for
    `updateProjectV2ItemFieldValue`."""
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        iteration_field="Sprint",
    )
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if path == "/graphql":
            body = _graphql_body(req)
            query, variables = body["query"], body["variables"]
            calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "clearProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "clearProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
            if "ProjectV2IterationField" in query:
                owner_field = _owner_field(query)
                return _json(_iteration_field_response(
                    owner_field, [{"id": "iter-4", "title": "Sprint 4"}],
                ))
            if "projectV2(number:$number){id}" in query:
                owner_field = _owner_field(query)
                return _json(
                    {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
                )
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", milestone=None,
    )
    assert ticket.id == "42"
    clear_calls = [v for q, v in calls if "clearProjectV2ItemFieldValue" in q]
    assert clear_calls == [{
        "projectId": "proj-node-id", "itemId": "item-1", "fieldId": "field-sprint",
    }]


def test_update_ticket_milestone_omitted_issues_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        iteration_field="Sprint",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(_project(board), "t", "42")
    assert ticket.id == "42"


def test_update_ticket_milestone_no_iteration_field_configured_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], owner="acme-org", project_number=7)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when iteration_field is unset")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="iteration_field"):
        GitHubProvider().update_ticket(_project(board), "t", "42", milestone="Sprint 4")


# ---------- ticket #154: board.auto_labels (on_create/on_update/on_move_to) --
#
# `on_create`/`on_update` are additive, best-effort label sets folded into
# every provider's create/update payload (GitHub covered here; GitLab/Azure
# in their own test files). `on_move_to` is GitHub-only for this iteration —
# it fires from `update_ticket` when `custom_fields` carries a new value for
# the board's `status_field`.


def _status_field_options(owner_field: str, options: list[dict]) -> dict:
    return {"data": {owner_field: {"projectV2": {"field": {
        "id": "field-status", "name": "Status", "options": options,
    }}}}}


def _status_field_number(owner_field: str) -> dict:
    """A `Status`-named field with no `options` — lets a non-string value
    write successfully as a plain number, isolating the `on_move_to`
    matching logic (which only ever looks at `str` values) from the
    unrelated single-select value-type validation."""
    return {"data": {owner_field: {"projectV2": {"field": {
        "id": "field-status", "name": "Status",
    }}}}}


def _board_write_graphql_handler(
    field_responses: dict[str, Any],
) -> Callable[[httpx.Request], httpx.Response]:
    """Shared GraphQL responder for the `addProjectV2ItemById` /
    `updateProjectV2ItemFieldValue` / `ProjectV2FieldCommon` /
    `projectV2(number:$number){id}` sequence `_write_custom_fields_to_board`
    issues. `field_responses` maps a `fieldName` to the JSON `field_responses[name](owner_field)`
    the `ProjectV2FieldCommon` lookup should answer with.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path != "/graphql":
            raise AssertionError(f"unexpected non-graphql request {req.method} {path}")
        body = _graphql_body(req)
        query, variables = body["query"], body["variables"]
        if "addProjectV2ItemById" in query:
            return _json(
                {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
            )
        if "updateProjectV2ItemFieldValue" in query:
            return _json({
                "data": {
                    "updateProjectV2ItemFieldValue": {
                        "projectV2Item": {"id": "item-1"},
                    }
                }
            })
        if "ProjectV2FieldCommon" in query:
            owner_field = _owner_field(query)
            field_name = variables["fieldName"]
            builder = field_responses.get(field_name)
            if builder is None:
                raise AssertionError(f"unexpected fieldName {field_name!r}")
            return _json(builder(owner_field))
        if "projectV2(number:$number){id}" in query:
            owner_field = _owner_field(query)
            return _json(
                {"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}}
            )
        raise AssertionError(f"unexpected graphql query: {query!r}")

    return handler


def test_create_ticket_on_create_labels_land_in_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], auto_labels={"on_create": ["triaged"]})
    created_payload: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            created_payload.update(json.loads(req.content.decode("utf-8")))
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "x", "color": "ededed"})
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=["bug"], assignees=[],
    )
    assert ticket.id == "99"
    assert set(created_payload["labels"]) == {"bug", "ai-generated", "triaged"}


def test_create_ticket_on_create_label_already_in_caller_labels_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], auto_labels={"on_create": ["triaged"]})
    created_payload: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            created_payload.update(json.loads(req.content.decode("utf-8")))
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "x", "color": "ededed"})
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=["triaged"], assignees=[],
    )
    assert created_payload["labels"].count("triaged") == 1


def test_update_ticket_on_update_labels_land_in_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Done"], auto_labels={"on_update": ["touched"]})
    patch_payloads: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "PATCH" and path.endswith("/issues/42"):
            patch_payloads.append(json.loads(req.content.decode("utf-8")))
            return _json(_ai_issue_payload(
                42, labels=[{"name": "ai-generated"}, {"name": "touched"}],
            ))
        if "/labels" in path:
            return _json({"name": "touched", "color": "ededed"})
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(_project(board), "t", "42")
    assert ticket.id == "42"
    assert patch_payloads, "expected a REST PATCH carrying the new label"
    assert "touched" in patch_payloads[0]["labels"]


def test_update_ticket_on_move_to_matches_logical_column_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary feature test (ticket #154): moving a ticket's board status
    to a column configured under `on_move_to` adds the configured label to
    the REST PATCH payload. This fails on pre-#154 code — no such wiring
    exists there — and passes once `update_ticket` fires `on_move_to`."""
    board = _board(
        ["Todo", "Doing", "Done"], owner="acme-org", project_number=7,
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    patch_payloads: list[dict] = []
    graphql_handler = _board_write_graphql_handler({
        "Status": lambda owner_field: _status_field_options(
            owner_field, [{"id": "opt-done", "name": "Done"}],
        ),
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "POST" and "/labels" in path:
            return _json({"name": "deployed", "color": "ededed"})
        if req.method == "PATCH" and path.endswith("/issues/42"):
            patch_payloads.append(json.loads(req.content.decode("utf-8")))
            return _json(_ai_issue_payload(
                42, labels=[{"name": "ai-generated"}, {"name": "deployed"}],
            ))
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"Status": "Done"},
    )
    assert ticket.id == "42"
    assert patch_payloads, "expected a REST PATCH carrying the new label"
    assert "deployed" in patch_payloads[0]["labels"]


def test_update_ticket_on_move_to_matches_resolved_native_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching also works against the column's resolved provider-native
    value (via `binding.map`), not just the logical column name."""
    board = _board(
        ["Todo", "Doing", "Done"], owner="acme-org", project_number=7,
        map_={"Done": "Closed"},
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    patch_payloads: list[dict] = []
    graphql_handler = _board_write_graphql_handler({
        "Status": lambda owner_field: _status_field_options(
            owner_field, [{"id": "opt-closed", "name": "Closed"}],
        ),
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "POST" and "/labels" in path:
            return _json({"name": "deployed", "color": "ededed"})
        if req.method == "PATCH" and path.endswith("/issues/42"):
            patch_payloads.append(json.loads(req.content.decode("utf-8")))
            return _json(_ai_issue_payload(
                42, labels=[{"name": "ai-generated"}, {"name": "deployed"}],
            ))
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"Status": "Closed"},
    )
    assert ticket.id == "42"
    assert patch_payloads
    assert "deployed" in patch_payloads[0]["labels"]


def test_update_ticket_on_move_to_no_match_adds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "Doing", "Done"], owner="acme-org", project_number=7,
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    graphql_handler = _board_write_graphql_handler({
        "Status": lambda owner_field: _status_field_options(
            owner_field, [{"id": "opt-todo", "name": "Todo"}],
        ),
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "POST" and "/labels" in path:
            raise AssertionError("no label ensure expected — no on_move_to match")
        if req.method == "PATCH" and path.endswith("/issues/42"):
            raise AssertionError("no REST label PATCH expected — nothing changed")
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"Status": "Todo"},
    )
    assert ticket.id == "42"


def test_update_ticket_on_move_to_ignores_non_status_field_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `custom_fields` key that is not the board's `status_field`
    (case-insensitively) never triggers `on_move_to`, even if its value
    happens to match a configured column name."""
    board = _board(
        ["Todo", "Doing", "Done"], owner="acme-org", project_number=7,
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    graphql_handler = _board_write_graphql_handler({
        "OtherField": lambda owner_field: {"data": {owner_field: {"projectV2": {
            "field": {"id": "field-other", "name": "OtherField"},
        }}}},
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "POST" and "/labels" in path:
            raise AssertionError("no label ensure expected — key isn't status_field")
        if req.method == "PATCH" and path.endswith("/issues/42"):
            raise AssertionError("no REST label PATCH expected — nothing changed")
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"OtherField": "Done"},
    )
    assert ticket.id == "42"


def test_update_ticket_on_move_to_non_str_value_ignored_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "Doing", "Done"], owner="acme-org", project_number=7,
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    graphql_handler = _board_write_graphql_handler({
        "Status": lambda owner_field: _status_field_number(owner_field),
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "POST" and "/labels" in path:
            raise AssertionError("no label ensure expected — value isn't a str")
        if req.method == "PATCH" and path.endswith("/issues/42"):
            raise AssertionError("no REST label PATCH expected — nothing changed")
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"Status": 42},
    )
    assert ticket.id == "42"


def test_update_ticket_on_move_to_label_create_failure_does_not_block_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort lifecycle, matching the AI markers: a repo that refuses
    to create the `deployed` label still gets its status moved."""
    board = _board(
        ["Todo", "Doing", "Done"], owner="acme-org", project_number=7,
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    graphql_handler = _board_write_graphql_handler({
        "Status": lambda owner_field: _status_field_options(
            owner_field, [{"id": "opt-done", "name": "Done"}],
        ),
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(42))
        if req.method == "POST" and "/labels" in path:
            return _json({"message": "Forbidden"}, status_code=403)
        if req.method == "PATCH" and path.endswith("/issues/42"):
            raise AssertionError(
                "no REST label PATCH expected — label create failed, dropped"
            )
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"Status": "Done"},
    )
    assert ticket.id == "42"


def test_update_ticket_on_move_to_already_present_label_preserves_early_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedup / additive-only: when the moved-to label is already on the
    ticket, `new_labels == current_labels` and the existing
    custom-fields-only board-write early-return path (no REST PATCH) is
    preserved unchanged."""
    board = _board(
        ["Todo", "Doing", "Done"], owner="acme-org", project_number=7,
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    graphql_handler = _board_write_graphql_handler({
        "Status": lambda owner_field: _status_field_options(
            owner_field, [{"id": "opt-done", "name": "Done"}],
        ),
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/42"):
            return _json(_ai_issue_payload(
                42, labels=[{"name": "ai-generated"}, {"name": "deployed"}],
            ))
        if req.method == "POST" and "/labels" in path:
            raise AssertionError("label already present — no ensure call expected")
        if req.method == "PATCH" and path.endswith("/issues/42"):
            raise AssertionError("no REST label PATCH expected — label unchanged")
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().update_ticket(
        _project(board), "t", "42", custom_fields={"Status": "Done"},
    )
    assert ticket.id == "42"


def test_create_ticket_on_move_to_does_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_move_to` is update-path only — `create_ticket` never applies it,
    even when a matching `custom_fields["Status"]` value is supplied."""
    board = _board(
        ["Todo", "Done"], owner="acme-org", project_number=7,
        auto_labels={"on_move_to": {"Done": ["deployed"]}},
    )
    created_payload: dict = {}
    graphql_handler = _board_write_graphql_handler({
        "Status": lambda owner_field: _status_field_options(
            owner_field, [{"id": "opt-done", "name": "Done"}],
        ),
    })

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            created_payload.update(json.loads(req.content.decode("utf-8")))
            return _json(_rest_issue_payload(99))
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            return graphql_handler(req)
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        _project(board), "t", title="hi", body="b", labels=[], assignees=[],
        custom_fields={"Status": "Done"},
    )
    assert ticket.id == "99"
    assert "deployed" not in created_payload.get("labels", [])
