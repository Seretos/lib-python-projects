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
from lib_python_projects.providers.github import GitHubProvider


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
