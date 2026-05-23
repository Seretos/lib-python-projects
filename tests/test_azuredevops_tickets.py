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
    tickets = AzureDevOpsProvider().list_tickets(
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
    tickets = AzureDevOpsProvider().list_tickets(
        _project(default_type="Issue"),
        token="t",
        filters=TicketFilters(limit=2),
    )
    assert len(tickets) == 2


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
    # raw ADO 400 into a curated "state '<x>' rejected" ValueError; that
    # rewrap is the upstream signal we surface here.
    assert "state transition" in msg
    assert "rejected" in msg
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
    assert "ticket #5" in msg
    assert "Bogus" in msg
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
    assert relations is None
    assert truncated is None
