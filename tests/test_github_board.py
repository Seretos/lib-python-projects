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
from typing import Callable

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
) -> Board:
    return Board(
        columns=columns,
        binding=GithubProjectsV2Binding(
            kind="github-projects-v2",
            owner=owner,
            project_number=project_number,
            status_field=status_field,
            map=map_,
        ),
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
    assert all(r.url.path != "/graphql" for r in seen)


def test_get_ticket_without_include_custom_fields_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `include_custom_fields=False` leaves `custom_fields` at its
    `None` default even with a board configured."""
    board = _board(["Todo"])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
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
