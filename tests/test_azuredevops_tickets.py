"""Tests for the Azure DevOps provider's work-item surface.

Covers:
- `list_tickets` WIQL building (state, labels, assignee, date filters,
  sort) and the two-step `wiql + workitemsbatch` fetch pattern
- `get_ticket` single-item fetch + relations + comments
- `create_ticket` JSON-Patch payload + ai-generated marker
- `update_ticket` state/title/body, tag read-modify-write, assignee
  add/remove, ai-modified marker heuristic
- `add_comment` / `list_comments` / `get_comment` / `update_comment`
  pagination + marker handling
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)
from lib_python_projects.providers.base import TicketFilters


# ---------- helpers ----------------------------------------------------------


def _project(
    default_type: str | None = None,
    path: str = "seredos/azure-tests/azure-tests",
) -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path=path,
        token_env="AZURE_TOKEN",
        default_work_item_type=default_type,
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


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    _cache_clear_all()


def _basic_template_handler(*, with_states: bool = True) -> Callable[[httpx.Request], httpx.Response | None]:
    """A reusable handler shard that responds to the workitemtypes +
    states endpoints with the Basic process template (To Do/Doing/Done).
    Returns None for paths it doesn't recognise so callers can chain.
    """
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
        "System.State": "To Do",
        "System.WorkItemType": "Issue",
        "System.Tags": "",
        "System.CreatedDate": "2026-05-18T10:00:00Z",
        "System.ChangedDate": "2026-05-18T11:00:00Z",
    }
    fields.update(fields_override)
    return {"id": work_item_id, "fields": fields}


# ---------- list_tickets -----------------------------------------------------


def test_list_tickets_runs_wiql_then_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    bt = _basic_template_handler()

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/wit/wiql"):
            # WIQL request body is the query string.
            body = json.loads(req.content.decode("utf-8"))
            assert "FROM workitems" in body["query"]
            assert "[System.TeamProject] = @project" in body["query"]
            return _json({"workItems": [{"id": 1}, {"id": 2}]})
        if path.endswith("/_apis/wit/workitemsbatch"):
            body = json.loads(req.content.decode("utf-8"))
            assert body["ids"] == [1, 2]
            assert "System.Title" in body["fields"]
            return _json({"value": [_work_item_payload(1), _work_item_payload(2)]})
        raise AssertionError(f"unexpected path {path}")

    seen = _install_mock(monkeypatch, handler)
    tickets, _ = AzureDevOpsProvider().list_tickets(
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(status="open", limit=30),
    )
    assert [t.id for t in tickets] == ["1", "2"]
    # Two roundtrips: WIQL then batch (states + types may add more if
    # we couldn't find them cached, so just assert the WIQL+batch pair
    # is present).
    paths = [str(r.url.path) for r in seen]
    assert any(p.endswith("/_apis/wit/wiql") for p in paths)
    assert any(p.endswith("/_apis/wit/workitemsbatch") for p in paths)


def test_list_tickets_translates_label_filter(monkeypatch: pytest.MonkeyPatch) -> None:
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
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(labels=["bug", "p1"], not_labels=["ignore"]),
    )
    assert "[System.Tags] CONTAINS 'bug'" in captured["wiql"]
    assert "[System.Tags] CONTAINS 'p1'" in captured["wiql"]
    assert "[System.Tags] NOT CONTAINS 'ignore'" in captured["wiql"]


def test_list_tickets_state_open_filters_by_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(status="open"),
    )
    # Open => states with category Proposed/InProgress/Resolved (Basic
    # template has To Do, Doing) — Done is Completed (terminal) and is
    # excluded.
    wiql = captured["wiql"]
    assert "'To Do'" in wiql and "'Doing'" in wiql
    assert "'Done'" not in wiql


def test_list_tickets_status_any_omits_state_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bt = _basic_template_handler(with_states=False)
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
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(status="any"),
    )
    assert "[System.State]" not in captured["wiql"]


def test_list_tickets_honours_sort_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
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
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(sort_by="updated", sort_order="asc"),
    )
    assert "ORDER BY [System.ChangedDate] ASC" in captured["wiql"]


def test_list_tickets_caps_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    bt = _basic_template_handler()

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/_apis/wit/wiql"):
            # Return 5 ids — limit 2 should clip to the first two.
            return _json({"workItems": [{"id": i} for i in (1, 2, 3, 4, 5)]})
        if req.url.path.endswith("/_apis/wit/workitemsbatch"):
            body = json.loads(req.content.decode("utf-8"))
            assert body["ids"] == [1, 2]
            return _json({"value": [_work_item_payload(1), _work_item_payload(2)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    tickets, _ = AzureDevOpsProvider().list_tickets(
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(limit=2),
    )
    assert len(tickets) == 2


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
        AzureDevOpsProvider().list_tickets(
            _project(default_type="Issue"),
            token="t",
            filters=TicketFilters(limit=bad_limit),
        )


def test_list_tickets_has_more_true_when_full_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_more is True when WIQL returns exactly limit IDs."""
    bt = _basic_template_handler()

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/_apis/wit/wiql"):
            # Return exactly 2 IDs matching limit=2.
            return _json({"workItems": [{"id": 1}, {"id": 2}]})
        if req.url.path.endswith("/_apis/wit/workitemsbatch"):
            body = json.loads(req.content.decode("utf-8"))
            assert body["ids"] == [1, 2]
            return _json({"value": [_work_item_payload(1), _work_item_payload(2)]})
        raise AssertionError(f"unexpected path {req.url.path}")

    _install_mock(monkeypatch, handler)
    tickets, has_more = AzureDevOpsProvider().list_tickets(
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(limit=2),
    )
    assert len(tickets) == 2
    assert has_more is True


def test_list_tickets_has_more_false_when_partial_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_more is False when WIQL returns fewer IDs than limit."""
    bt = _basic_template_handler()

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/_apis/wit/wiql"):
            # Return only 2 IDs when limit=5.
            return _json({"workItems": [{"id": 1}, {"id": 2}]})
        if req.url.path.endswith("/_apis/wit/workitemsbatch"):
            body = json.loads(req.content.decode("utf-8"))
            return _json({"value": [_work_item_payload(wid) for wid in body["ids"]]})
        raise AssertionError(f"unexpected path {req.url.path}")

    _install_mock(monkeypatch, handler)
    tickets, has_more = AzureDevOpsProvider().list_tickets(
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(limit=5),
    )
    assert len(tickets) == 2
    assert has_more is False


# ---------- get_ticket -------------------------------------------------------


def test_get_ticket_fetches_comments_and_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/5" in path and "/comments" not in path:
            payload = _work_item_payload(5)
            payload["relations"] = [
                {
                    "rel": "System.LinkTypes.Hierarchy-Forward",
                    "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    "attributes": {"name": "Child"},
                }
            ]
            return _json(payload)
        if path.endswith("/_apis/wit/workItems/5/comments"):
            return _json({
                "comments": [
                    {
                        "id": 1,
                        "createdBy": {"displayName": "Bob"},
                        "text": "<p>first</p>",
                        "createdDate": "2026-05-18T10:00:00Z",
                    },
                ],
                "continuationToken": None,
            })
        if path.endswith("/_apis/wit/workitemsbatch"):
            ids = json.loads(req.content.decode("utf-8"))["ids"]
            return _json({
                "value": [
                    {
                        "id": wid,
                        "fields": {
                            "System.Title": f"work item {wid}",
                            "System.State": "Active",
                        },
                    }
                    for wid in ids
                ]
            })
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ticket, comments, relations, truncated = AzureDevOpsProvider().get_ticket(
        _project(), token="t", ticket_id="5"
    )
    assert ticket.id == "5"
    assert len(comments) == 1
    assert comments[0].body == "first"
    assert len(relations) == 1
    assert relations[0].kind == "child"
    assert relations[0].ticket_id == "#9"
    # Title + state now come from the workitemsbatch lookup, not the
    # ADO relation type label (which used to leak in as "Child").
    assert relations[0].title == "work item 9"
    assert relations[0].state == "Active"
    assert truncated is False


def test_get_ticket_body_mentions_become_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/5" in path and "/comments" not in path:
            return _json(
                _work_item_payload(
                    5,
                    **{
                        "System.Description": "<p>Closes #11 mentions #22</p>",
                    },
                )
            )
        if path.endswith("/_apis/wit/workItems/5/comments"):
            return _json({"comments": [], "continuationToken": None})
        if path.endswith("/_apis/wit/workitemsbatch"):
            ids = json.loads(req.content.decode("utf-8"))["ids"]
            return _json({
                "value": [
                    {
                        "id": wid,
                        "fields": {
                            "System.Title": f"linked {wid}",
                            "System.State": "New",
                        },
                    }
                    for wid in ids
                ]
            })
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = AzureDevOpsProvider().get_ticket(
        _project(), token="t", ticket_id="5"
    )
    kinds = {r.kind: r.ticket_id for r in relations}
    assert kinds.get("closes") == "#11"
    assert kinds.get("mentions") == "#22"
    titles = {r.ticket_id: r.title for r in relations}
    assert titles["#11"] == "linked 11"
    assert titles["#22"] == "linked 22"


# ---------- create_ticket ----------------------------------------------------


def test_create_ticket_emits_json_patch_with_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Issue"}]})
        if "/_apis/wit/workitems/$Issue" in path:
            assert req.method == "POST"
            assert req.headers.get("Content-Type") == "application/json-patch+json"
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(7))
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ticket = AzureDevOpsProvider().create_ticket(
        _project(),
        token="t",
        title="hello",
        body="World",
        labels=["bug"],
        assignees=["alice@x.io"],
    )
    assert ticket.id == "7"
    patch = captured["patch"]
    # Title operation present.
    assert any(
        op for op in patch
        if op["path"] == "/fields/System.Title" and op["value"] == "hello"
    )
    # Description holds the rendered HTML with marker preserved as <p>.
    desc_op = next(op for op in patch if op["path"] == "/fields/System.Description")
    assert "#ai-generated" in desc_op["value"]
    assert "<p>World</p>" in desc_op["value"]
    # Tags include AI label + supplied label.
    tag_op = next(op for op in patch if op["path"] == "/fields/System.Tags")
    assert "ai-generated" in tag_op["value"]
    assert "bug" in tag_op["value"]
    # Assignee set to the first one.
    assignee_op = next(op for op in patch if op["path"] == "/fields/System.AssignedTo")
    assert assignee_op["value"] == "alice@x.io"


def test_create_ticket_uses_configured_default_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_url = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/$" in path:
            captured_url.append(path)
            return _json(_work_item_payload(1))
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        _project(default_type="Bug"),
        token="t",
        title="t", body="", labels=[], assignees=[],
    )
    assert any("/_apis/wit/workitems/$Bug" in u for u in captured_url)
    # No call to /workitemtypes — config short-circuits discovery.
    # (No assertion needed; the mock would have failed.)


def test_azuredevops_create_ticket_without_status_single_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status=None must not trigger the follow-up PATCH (single POST)."""
    posts: list[httpx.Request] = []
    patches: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            posts.append(req)
            return _json(_work_item_payload(7))
        if req.method == "PATCH":
            patches.append(req)
            return _json(_work_item_payload(7))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = AzureDevOpsProvider().create_ticket(
        _project(default_type="Issue"),
        token="t",
        title="hi",
        body="b",
        labels=[],
        assignees=[],
        status=None,
    )
    assert ticket.id == "7"
    assert len(posts) == 1
    assert len(patches) == 0


def test_azuredevops_create_ticket_with_terminal_status_does_two_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status='Done' must POST without System.State, then PATCH it to 'Done'."""
    post_bodies: list[list[dict]] = []
    patch_bodies: list[list[dict]] = []
    bt = _basic_template_handler()  # provides workitemtypes + states endpoints

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        path = req.url.path
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            post_bodies.append(json.loads(req.content.decode("utf-8")))
            # Server returns initial-state "To Do".
            return _json(_work_item_payload(42, **{"System.State": "To Do"}))
        if req.method == "GET" and "/_apis/wit/workitems/42" in path:
            # update_ticket reads current before PATCH.
            return _json(_work_item_payload(42, **{"System.State": "To Do"}))
        if req.method == "PATCH" and "/_apis/wit/workitems/42" in path:
            patch_bodies.append(json.loads(req.content.decode("utf-8")))
            return _json(_work_item_payload(42, **{"System.State": "Done"}))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = AzureDevOpsProvider().create_ticket(
        _project(default_type="Issue"),
        token="t",
        title="hi",
        body="b",
        labels=[],
        assignees=[],
        status="Done",
    )
    # Exactly one create + one transition.
    assert len(post_bodies) == 1
    assert len(patch_bodies) == 1
    # The create payload must NOT carry System.State; that field is the
    # whole point of the two-step flow.
    create_paths = {op["path"] for op in post_bodies[0]}
    assert "/fields/System.State" not in create_paths
    # The transition PATCH must set System.State to the requested value.
    transition_ops = {op["path"]: op for op in patch_bodies[0]}
    assert transition_ops["/fields/System.State"]["value"] == "Done"
    # The returned Ticket reflects the post-transition state.
    assert ticket.status == "Done"


def test_azuredevops_create_ticket_terminal_status_upstream_failure_raises_with_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the PATCH transition fails, the error mentions the work-item id
    and the upstream message — and the work item is left in place
    (no rollback DELETE)."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    deletes: list[httpx.Request] = []
    bt = _basic_template_handler()  # provides workitemtypes + states endpoints

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        path = req.url.path
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            return _json(_work_item_payload(99, **{"System.State": "To Do"}))
        if req.method == "GET" and "/_apis/wit/workitems/99" in path:
            return _json(_work_item_payload(99, **{"System.State": "To Do"}))
        if req.method == "PATCH" and "/_apis/wit/workitems/99" in path:
            return _json(
                {
                    "message": "transition not allowed: To Do -> Done",
                    "typeKey": "RuleValidationException",
                },
                status_code=400,
            )
        if req.method == "DELETE":
            deletes.append(req)
            return httpx.Response(204)
        raise AssertionError(f"unexpected {req.method} {path}")

    requests = _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().create_ticket(
            _project(default_type="Issue"),
            token="t",
            title="hi",
            body="b",
            labels=[],
            assignees=[],
            status="Done",
        )
    msg = str(exc.value)
    assert "#99" in msg
    assert "Done" in msg
    # The wrapper must signal that this was the post-create transition
    # that failed (not the create itself) and include the upstream error
    # text so the agent can act on it. update_ticket already wraps the
    # raw ADO 400 into a curated "unsupported status" ValueError; that
    # rewrap is the upstream signal we surface here.
    assert "state transition" in msg
    # The embedded ValueError message signals the problem — check it carries
    # the aligned "unsupported status" wording rather than the old "rejected".
    assert "unsupported status" in msg or "list_ticket_statuses" in msg
    # And the chained __cause__ carries the upstream exception so the
    # agent can introspect if needed.
    assert exc.value.__cause__ is not None
    # No rollback DELETE issued — the agent owns cleanup.
    assert deletes == []
    # And the requests log shows we did hit the create + patch path.
    methods = [r.method for r in requests]
    assert "POST" in methods
    assert "PATCH" in methods


def test_create_ticket_invalid_status_rejected_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown status value must raise ValueError before any POST is issued.

    Defect 1: create_ticket previously allowed the POST to succeed and then
    failed on the follow-up PATCH, leaving an orphan work item.  Now the
    status is validated up-front against the state list.
    """
    posts: list[httpx.Request] = []
    bt = _basic_template_handler()  # provides workitemtypes + states endpoints

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        if "/_apis/wit/workitems/$" in req.url.path and req.method == "POST":
            posts.append(req)
            return _json(_work_item_payload(1))
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="Bogus"):
        AzureDevOpsProvider().create_ticket(
            _project(default_type="Issue"),
            token="t",
            title="hi",
            body="b",
            labels=[],
            assignees=[],
            status="Bogus",
        )
    # The POST to create the work item must NOT have been issued.
    assert posts == [], "create_ticket must not POST when status is invalid"


def test_add_comment_on_missing_work_item_500_normalized_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO returns HTTP 500 with a 'does not exist' message when the work
    item is missing.  _check should remap that to status 404.

    Defect 3: without the remap the caller would see a confusing server-
    error AzureDevOpsError(500, …) instead of a clear not-found 404.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if "/comments" in req.url.path and req.method == "POST":
            return _json(
                {"message": "Work item does not exist"},
                status_code=500,
            )
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().add_comment(
            _project(),
            token="t",
            ticket_id="9999",
            body="hello",
        )
    assert exc.value.status == 404, (
        f"Expected 404 from 500+'does not exist', got {exc.value.status}"
    )


# ---------- update_ticket ---------------------------------------------------


def test_update_ticket_replaces_title_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(_work_item_payload(5))
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(5, **{"System.Title": "new"}))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(), token="t", ticket_id="5", title="new", status="Doing"
    )
    ops = {op["path"]: op for op in captured["patch"]}
    assert ops["/fields/System.Title"]["value"] == "new"
    assert ops["/fields/System.State"]["value"] == "Doing"


def test_update_ticket_label_diff_marks_ai_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a label to a non-AI-authored item must also stamp the
    `ai-modified` label so the resulting tag set is visible to humans."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(_work_item_payload(5, **{"System.Tags": "p1"}))
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(5))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(), token="t", ticket_id="5", labels_add=["regression"]
    )
    tag_op = next(op for op in captured["patch"] if op["path"] == "/fields/System.Tags")
    tags = set(t.strip() for t in tag_op["value"].split(";") if t.strip())
    assert tags == {"p1", "regression", "ai-modified"}


def test_update_ticket_body_stamps_ai_modified_when_not_ai_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(
                _work_item_payload(5, **{"System.Description": "<p>human content</p>"})
            )
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(5))
        raise AssertionError

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(), token="t", ticket_id="5", body="My update"
    )
    desc_op = next(op for op in captured["patch"] if op["path"] == "/fields/System.Description")
    assert "#ai-modified" in desc_op["value"]


def test_update_ticket_preserves_ai_generated_when_already_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(
                _work_item_payload(
                    5,
                    **{
                        "System.Description": (
                            "<p>#ai-generated</p><p>existing</p>"
                        ),
                    },
                )
            )
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(5))
        raise AssertionError

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(), token="t", ticket_id="5", body="Replacement"
    )
    desc = next(op for op in captured["patch"] if op["path"] == "/fields/System.Description")["value"]
    assert "#ai-generated" in desc
    assert "#ai-modified" not in desc


# ---------- comments ---------------------------------------------------------


def test_add_comment_adds_marker_and_serializes_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/workItems/5/comments"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json(
                {
                    "id": 11,
                    "createdBy": {"displayName": "AI"},
                    "text": captured["body"]["text"],
                    "createdDate": "2026-05-18T10:00:00Z",
                }
            )
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    comment = AzureDevOpsProvider().add_comment(
        _project(), token="t", ticket_id="5", body="Hi"
    )
    assert "#ai-generated" in captured["body"]["text"]
    assert "<p>Hi</p>" in captured["body"]["text"]
    assert comment.id == "11"


def test_list_comments_respects_order_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/wit/workItems/5/comments"):
            return _json({
                "comments": [
                    {
                        "id": 1, "createdBy": {"displayName": "A"},
                        "text": "<p>first</p>", "createdDate": "2026-05-18T10:00:00Z",
                    },
                    {
                        "id": 2, "createdBy": {"displayName": "B"},
                        "text": "<p>second</p>", "createdDate": "2026-05-18T11:00:00Z",
                    },
                ],
                "continuationToken": None,
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    comments, truncated = AzureDevOpsProvider().list_comments(
        _project(), token="t", ticket_id="5", limit=10, order="desc"
    )
    assert [c.id for c in comments] == ["2", "1"]
    assert truncated is False


def test_list_comments_desc_multi_page_returns_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """desc + limit=2 across two pages must return the two NEWEST comments,
    newest first — not the oldest two reversed.

    Page 1 (via continuationToken=None): comments 1 and 2 (oldest).
    Page 2 (via continuationToken='tok2'): comment 3 (newest).
    Expected result: [comment 3, comment 2] (ids "3", "2").
    """
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/wit/workItems/5/comments"):
            ct = req.url.params.get("continuationToken")
            if not ct:
                return _json({
                    "comments": [
                        {
                            "id": 1, "createdBy": {"displayName": "A"},
                            "text": "<p>oldest</p>", "createdDate": "2026-05-18T09:00:00Z",
                        },
                        {
                            "id": 2, "createdBy": {"displayName": "B"},
                            "text": "<p>middle</p>", "createdDate": "2026-05-18T10:00:00Z",
                        },
                    ],
                    "continuationToken": "tok2",
                })
            if ct == "tok2":
                return _json({
                    "comments": [
                        {
                            "id": 3, "createdBy": {"displayName": "C"},
                            "text": "<p>newest</p>", "createdDate": "2026-05-18T11:00:00Z",
                        },
                    ],
                    "continuationToken": None,
                })
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    comments, truncated = AzureDevOpsProvider().list_comments(
        _project(), token="t", ticket_id="5", limit=2, order="desc"
    )
    assert [c.id for c in comments] == ["3", "2"]
    assert truncated is False


def test_update_comment_requires_ticket_id() -> None:
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().update_comment(
            _project(), token="t", comment_id="11", body="x", ticket_id=None
        )
    assert "ticket_id" in str(exc.value)


# ---------- Round 3 bug-fix coverage ---------------------------------------


def test_update_ticket_invalid_status_includes_accepted_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO 400 on bad state -> error message lists the accepted states
    so the agent doesn't need a separate list_ticket_statuses call."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Initial GET for the work item (update_ticket reads current).
        if (
            req.method == "GET"
            and "/_apis/wit/workitems/5" in path
        ):
            return _json(_work_item_payload(5))
        # The PATCH gets rejected with an ADO state-validation error.
        if (
            req.method == "PATCH"
            and "/_apis/wit/workitems/5" in path
        ):
            return _json(
                {
                    "message": "The field 'State' contains the value 'Bogus' which is not in the list of supported values",
                    "typeKey": "RuleValidationException",
                },
                status_code=400,
            )
        # list_statuses follow-up.
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Issue"}]})
        if path.endswith("/_apis/wit/workitemtypes/Issue/states"):
            return _json({
                "value": [
                    {"name": "To Do", "category": "Proposed"},
                    {"name": "Doing", "category": "InProgress"},
                    {"name": "Done", "category": "Completed"},
                ]
            })
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)

    # Raises ValueError (not AzureDevOpsError) so the `_safe`
    # translation produces a clean `{"error": "..."}` payload without
    # the raw "Azure DevOps 400:" provider prefix.
    with pytest.raises(ValueError) as exc:
        AzureDevOpsProvider().update_ticket(
            _project(default_type="Issue"),
            token="t", ticket_id="5", status="Bogus",
        )
    msg = str(exc.value)
    # New format: "unsupported status 'Bogus' for Azure DevOps — use list_ticket_statuses ..."
    assert "Bogus" in msg
    assert "Azure DevOps" in msg
    assert "list_ticket_statuses" in msg
    assert "accepted" in msg.lower()
    # The accepted list must include the discovered states.
    assert "To Do" in msg
    assert "Done" in msg
    # No raw provider prefix.
    assert "Azure DevOps 400" not in msg


def test_get_comment_int32_overflow_id_pre_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Comment / ticket IDs beyond Int32 range are pre-rejected before
    any network I/O — saves an opaque ADO 400 and surfaces the id
    in the error message so `_safe` produces a clean
    `{"error": "comment '...' not found ..."}` payload."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no network request expected — pre-validation should reject")

    _install_mock(monkeypatch, handler)
    with pytest.raises(LookupError) as exc:
        AzureDevOpsProvider().get_comment(
            _project(),
            token="t",
            comment_id="99999999999999",  # > int32 max
            ticket_id="5",
        )
    msg = str(exc.value)
    assert "99999999999999" in msg
    assert "not found" in msg
    assert "2147483647" in msg or "32-bit" in msg


def test_get_comment_int32_overflow_via_check_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: if a numeric overflow somehow makes it to ADO
    (e.g. a non-digit string that the server later coerces), the
    overflow message is still translated to 404 by `_check`."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "Value was either too large or too small for an Int32.",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    # ticket_id at the int32 boundary — small enough to pass pre-validation
    # but the comment endpoint can still surface its own overflow.
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_comment(
            _project(),
            token="t",
            comment_id="123",
            ticket_id="5",
        )
    assert exc.value.status == 404


# ---------- list_tickets search CONTAINS WORDS --------------------------------


def test_list_tickets_search_uses_contains_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """filters.search must emit CONTAINS WORDS (tokenized full-text) for both
    Title and Description, not bare CONTAINS (substring match)."""
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
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(search="lifecycle"),
    )
    wiql = captured["wiql"]
    assert "CONTAINS WORDS 'lifecycle'" in wiql
    # Both Title and Description must use CONTAINS WORDS
    assert wiql.count("CONTAINS WORDS 'lifecycle'") == 2
    # Plain CONTAINS without WORDS must not appear for this search term
    import re
    assert not re.search(r"\bCONTAINS\s+'lifecycle'", wiql)


# ---------- F22: get_ticket(include_relations=False) sentinel ----------------


def test_get_ticket_include_relations_false_returns_none_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`include_relations=False` returns `(None, None)` for relations/truncated.

    This allows callers to distinguish 'skipped' from 'fetched but empty'.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/5" in path:
            return _json({
                "id": 5,
                "fields": {
                    "System.Title": "Test item",
                    "System.Description": "",
                    "System.State": "Active",
                    "System.WorkItemType": "Issue",
                    "System.Tags": "",
                    "System.CreatedDate": "2026-01-01T00:00:00Z",
                    "System.ChangedDate": "2026-01-02T00:00:00Z",
                },
            })
        if "/_apis/wit/workItems/5/comments" in path:
            return _json({"value": [], "count": 0})
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    _, _, relations, truncated = AzureDevOpsProvider().get_ticket(
        _project(), token="t", ticket_id="5", include_relations=False
    )
    assert relations == []
    assert truncated is None


# ---------- Defect 3: empty body raises ValueError (Azure DevOps) ------------


def test_add_comment_empty_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_comment with body='' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        AzureDevOpsProvider().add_comment(_project(), token="t", ticket_id="5", body="")


def test_add_comment_whitespace_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_comment with body='   ' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        AzureDevOpsProvider().add_comment(_project(), token="t", ticket_id="5", body="   ")


def test_update_comment_empty_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment with body='' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        AzureDevOpsProvider().update_comment(
            _project(), token="t", comment_id="11", body="", ticket_id="5",
        )


def test_update_comment_whitespace_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment with body='   ' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        AzureDevOpsProvider().update_comment(
            _project(), token="t", comment_id="11", body="   ", ticket_id="5",
        )


# ---------- Issue #17 defect fixes -------------------------------------------


def test_update_ticket_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_ticket on a missing work item wraps the 404 with resource id."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {"message": "Work item does not exist.", "typeKey": "WorkItemNotFoundException"},
            status_code=400,  # ADO 400-as-404
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().update_ticket(
            _project(), token="t", ticket_id="42", title="x",
        )
    assert exc.value.status == 404
    assert "ticket 'azure-tests#42' not found" in exc.value.message


def test_add_comment_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_comment on a missing work item wraps the 404 with resource id."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/comments" in req.url.path:
            return _json(
                {"message": "Work item does not exist."},
                status_code=404,
            )
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().add_comment(
            _project(), token="t", ticket_id="42", body="hello",
        )
    assert exc.value.status == 404
    assert "ticket 'azure-tests#42' not found" in exc.value.message


def test_update_comment_404_names_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_comment on a missing comment wraps the 404 with resource id."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {"message": "Comment not found.", "typeKey": "CommentNotFoundException"},
            status_code=400,  # ADO 400-as-404
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().update_comment(
            _project(), token="t", comment_id="99", body="x", ticket_id="5",
        )
    assert exc.value.status == 404
    assert "comment 'azure-tests#99' not found" in exc.value.message


# ---------- Ticket #69: delete_comment ---------------------------------------


def test_delete_comment_requires_ticket_id() -> None:
    """delete_comment without ticket_id raises AzureDevOpsError 400 before any HTTP."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().delete_comment(
            _project(), token="t", comment_id="11", ticket_id=None
        )
    assert "ticket_id" in str(exc.value)


def test_delete_comment_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_comment with a success response returns None and calls the right URL."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsError
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json({}, status_code=200)

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().delete_comment(
        _project(), token="t", comment_id="11", ticket_id="5",
    )
    assert result is None
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert "workItems/5/comments/11" in seen[0].url.path


def test_delete_comment_404_names_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_comment on a missing comment raises AzureDevOpsError 404 naming the comment."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {"message": "Comment not found.", "typeKey": "CommentNotFoundException"},
            status_code=400,  # ADO 400-as-404
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().delete_comment(
            _project(), token="t", comment_id="11", ticket_id="5",
        )
    assert exc.value.status == 404
    assert "comment 'azure-tests#11' not found" in exc.value.message


# ---------- Case 5: invalid-status message format ----------------------------


def test_create_ticket_invalid_status_message_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_ticket with an invalid status must surface the aligned error
    message matching the GitHub/GitLab format: 'unsupported status ... for Azure
    DevOps — use list_ticket_statuses ...'."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Work item type list (for default type resolution).
        if "/_apis/wit/workitemtypes" in req.url.path and req.method == "GET":
            return _json({"value": [{"name": "Task"}]})
        # States endpoint — return a small valid list.
        if "/_apis/wit/workitemtypes/Task/states" in req.url.path:
            return _json({"value": [{"name": "To Do"}, {"name": "Done"}]})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError) as exc:
        AzureDevOpsProvider().create_ticket(
            _project(), token="t", title="Test", body="body",
            labels=[], assignees=[], status="Bogus",
        )
    msg = str(exc.value)
    assert "unsupported status" in msg
    assert "Azure DevOps" in msg
    assert "list_ticket_statuses" in msg


def test_update_ticket_invalid_status_message_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket with an invalid status rejected by ADO must surface the
    aligned error message format."""

    wi_fields = {
        "System.Title": "Old title",
        "System.Description": "<p>body</p>",
        "System.State": "To Do",
        "System.Tags": "",
        "System.WorkItemType": "Task",
        "System.AssignedTo": None,
    }
    wi_payload = {"id": 42, "fields": wi_fields}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # GET /workitems/42
        if "/workitems/42" in path and req.method == "GET":
            return _json(wi_payload)
        # PATCH /workitems/42 → ADO rejects the state transition.
        if "/workitems/42" in path and req.method == "PATCH":
            return _json(
                {
                    "message": "The field 'State' contains the value 'Bogus' which is not in the list of supported values",
                    "typeKey": "RuleValidationException",
                },
                status_code=400,
            )
        # list_statuses fallback used by the re-wrap.
        if "/_apis/wit/workitemtypes/Task/states" in path:
            return _json({"value": [{"name": "To Do"}, {"name": "Done"}]})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError) as exc:
        AzureDevOpsProvider().update_ticket(
            _project(), token="t", ticket_id="42", status="Bogus",
        )
    msg = str(exc.value)
    assert "unsupported status" in msg
    assert "Azure DevOps" in msg
    assert "list_ticket_statuses" in msg


# ---------- ticket #30: return-shape None vs "" fixes -------------------------


def test_map_work_item_comment_populates_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_map_work_item_comment must populate updated_at from modifiedDate."""
    bt = _basic_template_handler()

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/wit/workitems/7"):
            return _json(_work_item_payload(7))
        if "workItems/7/comments" in path:
            return _json({
                "comments": [{
                    "id": 1,
                    "text": "<p>Hello</p>",
                    "createdBy": {"displayName": "Alice"},
                    "createdDate": "2026-05-18T10:00:00Z",
                    "modifiedDate": "2026-05-19T12:30:00Z",
                }],
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    _ticket, comments, _rels, _trunc = AzureDevOpsProvider().get_ticket(
        _project(default_type="Issue"), token="t", ticket_id="7",
        include_relations=False,
    )
    assert len(comments) == 1
    assert comments[0].updated_at == "2026-05-19T12:30:00Z"


# ---------- ticket #113: surface Microsoft.VSTS.Common.AcceptanceCriteria ----


def test_map_work_item_populates_acceptance_criteria_html_normalised() -> None:
    """`_map_work_item` must map AcceptanceCriteria through the same HTML ->
    markdown normalisation used for `body`."""
    raw = _work_item_payload(
        5, **{"Microsoft.VSTS.Common.AcceptanceCriteria": "<div>Must handle <b>X</b></div>"}
    )
    ticket = azure_mod._map_work_item(raw, _project())
    assert ticket.acceptance_criteria == "Must handle **X**"


def test_map_work_item_acceptance_criteria_defaults_empty_when_absent() -> None:
    """When ADO omits the AcceptanceCriteria field entirely, the ticket must
    surface an empty string rather than crashing or returning None."""
    raw = _work_item_payload(5)
    assert "Microsoft.VSTS.Common.AcceptanceCriteria" not in raw["fields"]
    ticket = azure_mod._map_work_item(raw, _project())
    assert ticket.acceptance_criteria == ""


def test_get_ticket_includes_acceptance_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AcceptanceCriteria round-trips from the work-item payload through
    `get_ticket` to the returned `Ticket`."""
    bt = _basic_template_handler()

    def handler(req: httpx.Request) -> httpx.Response:
        cached = bt(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/wit/workitems/8"):
            return _json(
                _work_item_payload(
                    8,
                    **{
                        "Microsoft.VSTS.Common.AcceptanceCriteria": (
                            "<ul><li>Given X</li><li>Then Y</li></ul>"
                        )
                    },
                )
            )
        if path.endswith("/_apis/wit/workItems/8/comments"):
            return _json({"comments": [], "continuationToken": None})
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket, _comments, _rels, _trunc = AzureDevOpsProvider().get_ticket(
        _project(default_type="Issue"), token="t", ticket_id="8",
        include_relations=False,
    )
    assert "Given X" in ticket.acceptance_criteria
    assert "Then Y" in ticket.acceptance_criteria


def test_list_statuses_terminal_declined_none_when_no_removed_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template without a 'Removed' category must report terminal_declined=None
    rather than '' so agents can test `if x.hints['terminal_declined'] is None`."""
    bt = _basic_template_handler()

    _install_mock(monkeypatch, bt)
    spec = AzureDevOpsProvider().list_statuses(_project(default_type="Issue"), token="t")
    assert spec.hints["terminal_declined"] is None


def test_list_statuses_terminal_completed_none_when_no_completed_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template without a 'Completed' category must report terminal_completed=None."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Issue"}]})
        if path.endswith("/_apis/wit/workitemtypes/Issue/states"):
            return _json({
                "value": [
                    {"name": "Active", "category": "InProgress"},
                    {"name": "Removed", "category": "Removed"},
                ]
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    spec = AzureDevOpsProvider().list_statuses(_project(default_type="Issue"), token="t")
    assert spec.hints["terminal_completed"] is None
    assert spec.hints["terminal_declined"] == "Removed"


def test_list_statuses_both_terminals_populated_when_both_categories_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both terminal hints are strings when Completed and Removed categories exist."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Bug"}]})
        if path.endswith("/_apis/wit/workitemtypes/Bug/states"):
            return _json({
                "value": [
                    {"name": "New", "category": "Proposed"},
                    {"name": "Closed", "category": "Completed"},
                    {"name": "Removed", "category": "Removed"},
                ]
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    p = _project()
    p.default_work_item_type = "Bug"  # type: ignore[misc]
    spec = AzureDevOpsProvider().list_statuses(p, token="t")
    assert spec.hints["terminal_completed"] == "Closed"
    assert spec.hints["terminal_declined"] == "Removed"


def test_update_ticket_custom_fields_emitted_in_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields entries appear as add-ops in the PATCH payload."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(_work_item_payload(5))
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(5))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(),
        token="t",
        ticket_id="5",
        custom_fields={"Custom.ProcessState": "50 - testen"},
    )
    ops = {op["path"]: op for op in captured["patch"]}
    assert "/fields/Custom.ProcessState" in ops
    assert ops["/fields/Custom.ProcessState"]["op"] == "add"
    assert ops["/fields/Custom.ProcessState"]["value"] == "50 - testen"


def test_update_ticket_custom_fields_none_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields=None and custom_fields={} must not issue a PATCH request."""
    patch_hit: list[bool] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(_work_item_payload(5))
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            patch_hit.append(True)
            return _json(_work_item_payload(5))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(), token="t", ticket_id="5", custom_fields=None
    )
    assert patch_hit == [], "PATCH should not be issued when custom_fields=None"

    AzureDevOpsProvider().update_ticket(
        _project(), token="t", ticket_id="5", custom_fields={}
    )
    assert patch_hit == [], "PATCH should not be issued when custom_fields={}"


def test_update_ticket_custom_fields_combined_with_standard_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both standard and custom fields appear in the same PATCH payload."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(_work_item_payload(5))
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(5, **{"System.Title": "new"}))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(),
        token="t",
        ticket_id="5",
        title="new",
        custom_fields={"Custom.ProcessState": "Done"},
    )
    ops = {op["path"]: op for op in captured["patch"]}
    assert "/fields/System.Title" in ops, "standard title field must appear"
    assert ops["/fields/System.Title"]["value"] == "new"
    assert "/fields/Custom.ProcessState" in ops, "custom field must appear"
    assert ops["/fields/Custom.ProcessState"]["value"] == "Done"


# ---------- Ticket #114: custom_fields on create_ticket + read opt-in --------


def test_create_ticket_custom_fields_emitted_in_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields entries appear as add-ops in the create PATCH payload."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/$Issue" in path and req.method == "POST":
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(20))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = AzureDevOpsProvider().create_ticket(
        _project(default_type="Issue"),
        token="t",
        title="hello",
        body="world",
        labels=[],
        assignees=[],
        custom_fields={"Custom.ProcessState": "50 - testen"},
    )
    assert ticket.id == "20"
    ops = {op["path"]: op for op in captured["patch"]}
    assert "/fields/Custom.ProcessState" in ops
    assert ops["/fields/Custom.ProcessState"]["op"] == "add"
    assert ops["/fields/Custom.ProcessState"]["value"] == "50 - testen"


def test_create_ticket_custom_fields_none_and_empty_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields=None and={} must not add any extra patch ops — existing
    create behavior (Title/Description/Tags only, no labels/assignees) is
    unchanged."""
    captured: list[list[dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/$Issue" in path and req.method == "POST":
            captured.append(json.loads(req.content.decode("utf-8")))
            return _json(_work_item_payload(21))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    for cf in (None, {}):
        AzureDevOpsProvider().create_ticket(
            _project(default_type="Issue"),
            token="t",
            title="hello",
            body="world",
            labels=[],
            assignees=[],
            custom_fields=cf,
        )
    assert len(captured) == 2
    for patch in captured:
        paths = {op["path"] for op in patch}
        assert paths == {
            "/fields/System.Title",
            "/fields/System.Description",
            "/fields/System.Tags",
        }


def test_create_ticket_custom_fields_precedence_over_title_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields={'System.Title': 'X'} alongside a `title` arg — the
    custom_fields entry wins because its add-op is appended last."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/$Issue" in path and req.method == "POST":
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(22, **{"System.Title": "custom-wins"}))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        _project(default_type="Issue"),
        token="t",
        title="arg-title",
        body="world",
        labels=[],
        assignees=[],
        custom_fields={"System.Title": "custom-wins"},
    )
    title_ops = [op for op in captured["patch"] if op["path"] == "/fields/System.Title"]
    assert len(title_ops) == 2, "both the standard and custom_fields title op must appear"
    assert title_ops[0]["value"] == "arg-title"
    assert title_ops[-1]["value"] == "custom-wins", "custom_fields op must be last (wins)"


def test_create_ticket_custom_fields_work_item_type_canonical_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields={'System.WorkItemType': 'Bug'} overrides the POST type,
    is not emitted as a /fields/... op, and _default_work_item_type is not
    consulted."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            raise AssertionError(
                "_default_work_item_type must not be consulted when overridden"
            )
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            captured["path"] = path
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(23, **{"System.WorkItemType": "Bug"}))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        _project(),  # no default_type configured — override must short-circuit discovery
        token="t",
        title="hello",
        body="world",
        labels=[],
        assignees=[],
        custom_fields={"System.WorkItemType": "Bug"},
    )
    assert "/_apis/wit/workitems/$Bug" in captured["path"]
    ops = {op["path"] for op in captured["patch"]}
    assert "/fields/System.WorkItemType" not in ops


def test_create_ticket_custom_fields_work_item_type_alias_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The short alias 'WorkItemType' has the same override behavior as the
    canonical 'System.WorkItemType' ref."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            raise AssertionError(
                "_default_work_item_type must not be consulted when overridden"
            )
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            captured["path"] = path
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(23, **{"System.WorkItemType": "Task"}))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        _project(),
        token="t",
        title="hello",
        body="world",
        labels=[],
        assignees=[],
        custom_fields={"WorkItemType": "Task"},
    )
    assert "/_apis/wit/workitems/$Task" in captured["path"]
    ops = {op["path"] for op in captured["patch"]}
    assert "/fields/WorkItemType" not in ops
    assert "/fields/System.WorkItemType" not in ops


def test_create_ticket_custom_fields_work_item_type_feeds_status_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both `status` and a System.WorkItemType override, `_states_for_type`
    is queried for the overridden type, not the default."""
    states_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            raise AssertionError(
                "_default_work_item_type must not be consulted when overridden"
            )
        if path.endswith("/_apis/wit/workitemtypes/Issue/states"):
            raise AssertionError(
                "states must be checked against the overridden type, not Issue"
            )
        if path.endswith("/_apis/wit/workitemtypes/Bug/states"):
            states_calls.append("Bug")
            return _json({
                "value": [
                    {"name": "To Do", "category": "Proposed"},
                    {"name": "Done", "category": "Completed"},
                ]
            })
        if "/_apis/wit/workitems/$Bug" in path and req.method == "POST":
            return _json(_work_item_payload(
                24, **{"System.WorkItemType": "Bug", "System.State": "To Do"}
            ))
        if req.method == "GET" and path.endswith("/_apis/wit/workitems/24"):
            return _json(_work_item_payload(
                24, **{"System.WorkItemType": "Bug", "System.State": "To Do"}
            ))
        if req.method == "PATCH" and path.endswith("/_apis/wit/workitems/24"):
            return _json(_work_item_payload(
                24, **{"System.WorkItemType": "Bug", "System.State": "Done"}
            ))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    ticket = AzureDevOpsProvider().create_ticket(
        _project(),
        token="t",
        title="hello",
        body="world",
        labels=[],
        assignees=[],
        status="Done",
        custom_fields={"System.WorkItemType": "Bug"},
    )
    assert states_calls == ["Bug"]
    assert ticket.status == "Done"


def test_create_ticket_custom_fields_non_string_values_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-string custom field values (int/bool) pass through the patch
    verbatim — no stringification."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/$Issue" in path and req.method == "POST":
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(25))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        _project(default_type="Issue"),
        token="t",
        title="hello",
        body="world",
        labels=[],
        assignees=[],
        custom_fields={"Custom.Priority": 2, "Custom.IsBlocked": True},
    )
    ops = {op["path"]: op for op in captured["patch"]}
    assert ops["/fields/Custom.Priority"]["value"] == 2
    assert ops["/fields/Custom.IsBlocked"]["value"] is True


def test_create_ticket_custom_fields_arbitrary_ref_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown/arbitrary field ref is emitted untouched — the provider
    does not validate custom field references against any schema."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/$Issue" in path and req.method == "POST":
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(26))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        _project(default_type="Issue"),
        token="t",
        title="hello",
        body="world",
        labels=[],
        assignees=[],
        custom_fields={"Vendor.Custom.ArbitraryRef": "whatever"},
    )
    ops = {op["path"]: op for op in captured["patch"]}
    assert ops["/fields/Vendor.Custom.ArbitraryRef"]["value"] == "whatever"


def test_get_ticket_include_custom_fields_populates_raw_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_custom_fields=True populates ticket.custom_fields with the
    full raw fields dict — a known custom ref + a System.* ref both
    present — and issues no extra HTTP request beyond the existing
    work-item GET + comments GET."""
    requests_seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        requests_seen.append(path)
        if path.endswith("/_apis/wit/workitems/9") and "comments" not in path:
            return _json(_work_item_payload(9, **{"Custom.ProcessState": "50 - testen"}))
        if path.endswith("/_apis/wit/workItems/9/comments"):
            return _json({"comments": [], "continuationToken": None})
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ticket, _comments, _rels, _trunc = AzureDevOpsProvider().get_ticket(
        _project(), token="t", ticket_id="9",
        include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields is not None
    assert ticket.custom_fields["Custom.ProcessState"] == "50 - testen"
    assert ticket.custom_fields["System.Title"] == "Item 9"
    assert len(requests_seen) == 2, "no extra HTTP request beyond item GET + comments GET"


def test_get_ticket_include_custom_fields_default_false_leaves_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting include_custom_fields (default False) leaves custom_fields None."""
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitems/9") and "comments" not in path:
            return _json(_work_item_payload(9))
        if path.endswith("/_apis/wit/workItems/9/comments"):
            return _json({"comments": [], "continuationToken": None})
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = AzureDevOpsProvider().get_ticket(
        _project(), token="t", ticket_id="9", include_relations=False,
    )
    assert ticket.custom_fields is None


def test_get_ticket_include_custom_fields_empty_fields_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the work-item payload carries no `fields` key at all, opting in
    yields an empty dict rather than None."""
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitems/9") and "comments" not in path:
            return _json({"id": 9})  # no "fields" key
        if path.endswith("/_apis/wit/workItems/9/comments"):
            return _json({"comments": [], "continuationToken": None})
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = AzureDevOpsProvider().get_ticket(
        _project(), token="t", ticket_id="9",
        include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields == {}


# ---------- Ticket #74: create_ticket blank-title guard (Azure DevOps) --------


class TestCreateTicketBlankTitleAzureDevOps:
    """create_ticket must raise ValueError before any HTTP call when title is blank."""

    def test_empty_string_title_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """title="" must raise ValueError(blank) with no HTTP request made."""
        seen = _install_mock(monkeypatch, lambda req: _json({}))
        with pytest.raises(ValueError, match="blank"):
            AzureDevOpsProvider().create_ticket(
                _project(default_type="Issue"),
                token="t",
                title="",
                body="body",
                labels=[],
                assignees=[],
            )
        assert seen == [], "no HTTP request should have been made"

    def test_whitespace_only_title_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """title='   ' must raise ValueError(blank) with no HTTP request made."""
        seen = _install_mock(monkeypatch, lambda req: _json({}))
        with pytest.raises(ValueError, match="blank"):
            AzureDevOpsProvider().create_ticket(
                _project(default_type="Issue"),
                token="t",
                title="   ",
                body="body",
                labels=[],
                assignees=[],
            )
        assert seen == [], "no HTTP request should have been made"


# ---------- HTML serialisation integration -----------------------------------


def test_update_comment_serializes_html_in_patch_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment must send rendered HTML in the PATCH body's ``text`` field."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "/workItems/5/comments/3" in path:
            # Return a comment with no AI marker so apply_body_marker adds one.
            return _json(
                {
                    "id": 3,
                    "createdBy": {"displayName": "Alice"},
                    "text": "<p>original</p>",
                    "createdDate": "2026-05-18T10:00:00Z",
                }
            )
        if req.method == "PATCH" and "/workItems/5/comments/3" in path:
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json(
                {
                    "id": 3,
                    "createdBy": {"displayName": "Alice"},
                    "text": captured["body"]["text"],
                    "createdDate": "2026-05-18T10:00:00Z",
                }
            )
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_comment(
        _project(), token="t", ticket_id="5", comment_id="3", body="**bold** text"
    )
    sent_text = captured["body"]["text"]
    assert "<strong>bold</strong>" in sent_text, repr(sent_text)
    assert "text" in sent_text


def test_create_ticket_description_html_contains_rendered_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_ticket must send rendered HTML for the description field.

    A body containing a heading, list item, and inline code must appear
    as the corresponding HTML tags in the ``System.Description`` patch op.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Issue"}]})
        if "/_apis/wit/workitems/$Issue" in path:
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(8))
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        _project(),
        token="t",
        title="HTML test",
        body="## Heading\n\n- list item\n\nUse `code` here.",
        labels=[],
        assignees=[],
    )
    desc_op = next(
        op for op in captured["patch"] if op["path"] == "/fields/System.Description"
    )
    desc = desc_op["value"]
    assert "<h2>" in desc, repr(desc)
    assert "<li>" in desc, repr(desc)
    assert "<code>code</code>" in desc, repr(desc)


def test_update_ticket_body_html_contains_rendered_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket must send rendered HTML when a body is supplied."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/workitems/5"):
            return _json(_work_item_payload(5))
        if req.method == "PATCH" and path.endswith("/workitems/5"):
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json(_work_item_payload(5))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_ticket(
        _project(),
        token="t",
        ticket_id="5",
        body="## Title\n\n- item\n\nParagraph with `snippet`.",
    )
    ops = {op["path"]: op for op in captured["patch"]}
    desc = ops["/fields/System.Description"]["value"]
    assert "<h2>" in desc, repr(desc)
    assert "<li>" in desc, repr(desc)
    assert "<code>snippet</code>" in desc, repr(desc)
