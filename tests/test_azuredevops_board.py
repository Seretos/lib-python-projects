"""Tests for Azure Boards board support (ticket #119).

Covers `AzureDevOpsProvider.list_board_columns` and the
`TicketFilters.board_column` listing path added on top of the neutral
`board` schema from #117 / the Doing/Done split design from #119's plan.
HTTP is mocked via `httpx.MockTransport`, following the pattern already
used by `tests/test_azuredevops_tickets.py` — the provider module-level
`_client` is monkey-patched.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import AzureBoardsBinding, Board, GithubProjectsV2Binding, ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)
from lib_python_projects.providers.base import BoardColumnSpec, TicketFilters


# ---------- helpers ----------------------------------------------------------


def _board(
    columns: list[str],
    *,
    map_: dict[str, str] | None = None,
    team: str | None = "acme-team",
    board_name: str | None = "Stories",
    extras: dict | None = None,
) -> Board:
    return Board(
        columns=columns,
        binding=AzureBoardsBinding(
            kind="azure-boards",
            team=team,
            board=board_name,
            map=map_,
            provider_extras=extras or {},
        ),
    )


def _project(
    board: Board | None = None,
    default_type: str | None = None,
    path: str = "acme-org/acme-project/acme-repo",
    area_path: str | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="azuredevops",
        path=path,
        token_env="AZURE_TOKEN",
        default_work_item_type=default_type,
        board=board,
        area_path=area_path,
    )


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen


def _column(
    *,
    id_: str,
    name: str,
    column_type: str = "inProgress",
    is_split: bool = False,
    state_mappings: dict | None = None,
) -> dict:
    return {
        "id": id_,
        "name": name,
        "columnType": column_type,
        "isSplit": is_split,
        "stateMappings": state_mappings or {},
    }


def _basic_template_handler(*, with_states: bool = True) -> Callable[[httpx.Request], httpx.Response | None]:
    """Reusable handler shard for the workitemtypes + states discovery
    endpoints (Basic process template), mirroring
    tests/test_azuredevops_tickets.py's helper of the same name."""
    def inner(req: httpx.Request) -> httpx.Response | None:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Issue"}, {"name": "Task"}]})
        if with_states and path.endswith("/_apis/wit/workitemtypes/Issue/states"):
            return _json({
                "value": [
                    {"name": "To Do", "category": "Proposed"},
                    {"name": "Doing", "category": "InProgress"},
                    {"name": "Done", "category": "Completed"},
                ]
            })
        return None

    return inner


def _work_item_payload(work_item_id: int, **fields_override) -> dict:
    fields = {
        "System.Title": f"Item {work_item_id}",
        "System.Description": "<p>Body</p>",
        "System.State": "Doing",
        "System.WorkItemType": "Issue",
        "System.Tags": "",
        "System.CreatedDate": "2026-05-18T10:00:00Z",
        "System.ChangedDate": "2026-05-18T11:00:00Z",
    }
    fields.update(fields_override)
    return {"id": work_item_id, "fields": fields}


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    _cache_clear_all()


# ---------- regression: list_tickets(board_column=...) no longer raises -----


def test_list_tickets_board_column_regression_no_longer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #119 regression: previously `board_column` unconditionally
    raised ValueError on Azure DevOps. With a valid team+board binding it
    must now issue a WIQL query containing the resolved
    `[System.BoardColumn]` clause and return batch-fetched items."""
    board = _board(["Todo", "Doing", "Done"])
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/wiql"):
            captured["wiql"] = json.loads(req.content.decode("utf-8"))["query"]
            return _json({"workItems": [{"id": 1}, {"id": 2}]})
        if path.endswith("/_apis/wit/workitemsbatch"):
            body = json.loads(req.content.decode("utf-8"))
            assert body["ids"] == [1, 2]
            return _json({"value": [_work_item_payload(1), _work_item_payload(2)]})
        raise AssertionError(f"unexpected path {path}")

    _install_mock(monkeypatch, handler)
    tickets, has_more = AzureDevOpsProvider().list_tickets(
        _project(board),
        token="t",
        filters=TicketFilters(board_column="Doing", status="any"),
    )
    assert [t.id for t in tickets] == ["1", "2"]
    assert has_more is False
    assert "[System.BoardColumn] = 'Doing'" in captured["wiql"]


# ---------- list_board_columns — happy paths ---------------------------------


def test_list_board_columns_explicit_map(monkeypatch: pytest.MonkeyPatch) -> None:
    board = _board(["Todo", "Doing", "Done"], map_={"Todo": "Backlog"})

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/acme-org/acme-project/acme-team/_apis/work/boards/Stories/columns"
        assert req.url.params["api-version"] == "7.1"
        return _json({
            "value": [
                _column(id_="c1", name="Backlog", state_mappings={"Issue": "To Do"}),
                _column(id_="c2", name="Doing", state_mappings={"Issue": "Doing"}),
                _column(id_="c3", name="Done", state_mappings={"Issue": "Done"}),
            ]
        })

    _install_mock(monkeypatch, handler)
    specs = AzureDevOpsProvider().list_board_columns(_project(board), "t")
    assert specs == [
        BoardColumnSpec(logical="Todo", native="Backlog", option_id="c1", states=("To Do",)),
        BoardColumnSpec(logical="Doing", native="Doing", option_id="c2", states=("Doing",)),
        BoardColumnSpec(logical="Done", native="Done", option_id="c3", states=("Done",)),
    ]


def test_list_board_columns_identity_fallback_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `map` — logical columns resolve to themselves, matched against
    the live board's column names case-insensitively."""
    board = _board(["Todo", "Doing", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({
            "value": [
                _column(id_="o1", name="todo"),
                _column(id_="o2", name="Doing"),
                _column(id_="o3", name="DONE"),
            ]
        })

    _install_mock(monkeypatch, handler)
    specs = AzureDevOpsProvider().list_board_columns(_project(board), "t")
    assert [(s.logical, s.native, s.option_id) for s in specs] == [
        ("Todo", "Todo", "o1"),
        ("Doing", "Doing", "o2"),
        ("Done", "Done", "o3"),
    ]


def test_list_board_columns_surfaces_state_mappings_and_split_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Doing"])

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({
            "value": [
                _column(
                    id_="c1",
                    name="Doing",
                    is_split=True,
                    state_mappings={"Bug": "Committed", "User Story": "Committed"},
                ),
            ]
        })

    _install_mock(monkeypatch, handler)
    specs = AzureDevOpsProvider().list_board_columns(_project(board), "t")
    assert len(specs) == 1
    assert specs[0].is_split is True
    assert specs[0].states == ("Committed",)


# ---------- list_board_columns — error paths ---------------------------------


def test_list_board_columns_no_board_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when project has no board")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="no 'board' configuration"):
        AzureDevOpsProvider().list_board_columns(_project(None), "t")


def test_list_board_columns_wrong_binding_kind_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = Board(
        columns=["Todo"],
        binding=GithubProjectsV2Binding(kind="github-projects-v2", owner="x", project_number=1),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for a non-Azure-Boards binding")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not 'azure-boards'"):
        AzureDevOpsProvider().list_board_columns(_project(board), "t")


def test_list_board_columns_missing_team_or_board_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo"], team=None, board_name=None)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected without team/board")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="'team' and/or 'board'"):
        AzureDevOpsProvider().list_board_columns(_project(board), "t")


def test_list_board_columns_mapped_column_missing_from_live_board_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`map` resolves "Todo" to "Backlog", but the live board has no such
    column — must raise, not silently drop the column."""
    board = _board(["Todo", "Done"], map_={"Todo": "Backlog"})

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"value": [_column(id_="c1", name="Done")]})  # no "Backlog"

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not present on the live Azure"):
        AzureDevOpsProvider().list_board_columns(_project(board), "t")


# ---------- WIQL filter edge cases (via list_tickets) -------------------------


def _capture_wiql(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Install a mock that captures the WIQL query and returns zero
    matches, and return the dict the query gets written into."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/wit/wiql"):
            captured["wiql"] = json.loads(req.content.decode("utf-8"))["query"]
            return _json({"workItems": []})
        raise AssertionError(f"unexpected path {req.url.path}")

    _install_mock(monkeypatch, handler)
    return captured


def test_wiql_non_split_column_has_no_board_column_done_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Todo", "Doing", "Done"])
    captured = _capture_wiql(monkeypatch)
    AzureDevOpsProvider().list_tickets(
        _project(board), token="t",
        filters=TicketFilters(board_column="Todo", status="any"),
    )
    assert "[System.BoardColumn] = 'Todo'" in captured["wiql"]
    assert "BoardColumnDone" not in captured["wiql"]


def test_wiql_done_half_of_split_column_gets_done_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "InProgress", "Complete"],
        map_={"InProgress": "Doing", "Complete": "Doing"},
        extras={"split_done_column": "Complete"},
    )
    captured = _capture_wiql(monkeypatch)
    AzureDevOpsProvider().list_tickets(
        _project(board), token="t",
        filters=TicketFilters(board_column="Complete", status="any"),
    )
    assert "[System.BoardColumn] = 'Doing'" in captured["wiql"]
    assert "[System.BoardColumnDone] = true" in captured["wiql"]


def test_wiql_doing_half_sibling_gets_done_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(
        ["Todo", "InProgress", "Complete"],
        map_={"InProgress": "Doing", "Complete": "Doing"},
        extras={"split_done_column": "Complete"},
    )
    captured = _capture_wiql(monkeypatch)
    AzureDevOpsProvider().list_tickets(
        _project(board), token="t",
        filters=TicketFilters(board_column="InProgress", status="any"),
    )
    assert "[System.BoardColumn] = 'Doing'" in captured["wiql"]
    assert "[System.BoardColumnDone] = false" in captured["wiql"]


def test_wiql_board_column_combined_with_other_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Doing"])
    bt = _basic_template_handler()
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/_apis/wit/wiql"):
            captured["wiql"] = json.loads(req.content.decode("utf-8"))["query"]
            return _json({"workItems": []})
        raise AssertionError(f"unexpected path {req.url.path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().list_tickets(
        _project(board, default_type="Issue"),
        token="t",
        filters=TicketFilters(
            board_column="Doing",
            labels=["bug"],
            assignee="bob",
            search="flaky",
            status="open",
        ),
    )
    wiql = captured["wiql"]
    assert "[System.BoardColumn] = 'Doing'" in wiql
    assert "[System.Tags] CONTAINS 'bug'" in wiql
    assert "[System.AssignedTo] = 'bob'" in wiql
    assert "CONTAINS WORDS 'flaky'" in wiql
    assert "[System.State] IN (" in wiql
    # All clauses are AND-ed into the single WHERE expression.
    assert wiql.count(" AND ") >= 4


def test_wiql_board_column_combined_with_config_area_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #172: a `board_column` filter AND a config-level
    `project.area_path` both appear, AND-ed, in the same WIQL — the
    board-column clause-builder and the area-path resolver must compose
    rather than one silently dropping the other."""
    board = _board(["Doing"])
    bt = _basic_template_handler()
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/_apis/wit/wiql"):
            captured["wiql"] = json.loads(req.content.decode("utf-8"))["query"]
            return _json({"workItems": []})
        raise AssertionError(f"unexpected path {req.url.path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().list_tickets(
        _project(board, default_type="Issue", area_path="acme-project\\Team A"),
        token="t",
        filters=TicketFilters(board_column="Doing", status="any"),
    )
    wiql = captured["wiql"]
    assert "[System.BoardColumn] = 'Doing'" in wiql
    assert "[System.AreaPath] UNDER 'acme-project\\Team A'" in wiql
    assert " AND " in wiql


def test_wiql_unknown_board_column_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    board = _board(["Todo", "Doing", "Done"])

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an unknown column")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="is not one of this project's board columns"):
        AzureDevOpsProvider().list_tickets(
            _project(board), token="t",
            filters=TicketFilters(board_column="Bogus", status="any"),
        )


def test_wiql_missing_board_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when project has no board")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="no 'board' configuration"):
        AzureDevOpsProvider().list_tickets(
            _project(None), token="t",
            filters=TicketFilters(board_column="Doing", status="any"),
        )


def test_wiql_mismatched_binding_kind_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    board = Board(
        columns=["Doing"],
        binding=GithubProjectsV2Binding(kind="github-projects-v2", owner="x", project_number=1),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for a mismatched binding")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not 'azure-boards'"):
        AzureDevOpsProvider().list_tickets(
            _project(board), token="t",
            filters=TicketFilters(board_column="Doing", status="any"),
        )


def test_wiql_missing_team_or_board_on_binding_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Doing"], team=None, board_name=None)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected without team/board")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="'team' and/or 'board'"):
        AzureDevOpsProvider().list_tickets(
            _project(board), token="t",
            filters=TicketFilters(board_column="Doing", status="any"),
        )


def test_list_tickets_board_column_empty_result_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(["Doing"])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/wit/wiql"):
            return _json({"workItems": []})
        raise AssertionError(f"unexpected path {req.url.path}")

    _install_mock(monkeypatch, handler)
    tickets, has_more = AzureDevOpsProvider().list_tickets(
        _project(board), token="t",
        filters=TicketFilters(board_column="Doing", status="any"),
    )
    assert tickets == []
    assert has_more is False
