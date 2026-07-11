"""Misc tests for the Azure DevOps provider.

Covers the surface not already in `_scaffold` / `_tickets` / `_pulls`:
- relation kind mapping (parent/child/blocks/blocked_by/duplicate_of/relates_to)
- `add_relation` JSON-Patch payload
- `remove_relation` array-index resolution
- relation kind unsupported raises with the right list
- cross-project relation targets are rejected
- pipelines: list_runs_for_branch, list_runs_for_ticket, get_run with failure
- token probe: 401 / 403 / 200 mapping
- refs URL parser: work item + PR + cross-project guard
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsError,
    AzureDevOpsProvider,
    SUPPORTED_RELATION_KINDS,
    _RELATION_FORWARD,
    _ado_rel_to_kind,
    _basic_auth_header,
    _cache_clear_all,
    _default_open_state,
    _html_to_markdown,
    _markdown_to_html,
)
from lib_python_projects.markers import apply_body_marker, has_ai_generated_marker
from lib_python_projects.providers.base import (
    RelationAlreadyExists,
    RelationKindUnsupported,
    RelationNotFound,
)

# `refs.normalize_id` lives in the agent-project-issues plugin's tool
# layer (URL → provider-native id mapping). It's not part of the
# provider domain, so the 4 refs-URL-parsing tests below are skipped
# here and remain in the plugin's own test suite.
try:  # pragma: no cover
    from project_issues_plugin.refs import normalize_id  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    normalize_id = None  # type: ignore[assignment]


def _project(path: str = "seredos/azure-tests/azure-tests") -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path=path,
        token_env="AZURE_TOKEN",
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


# ---------- relation kind mapping -------------------------------------------


@pytest.mark.parametrize("kind,rel", [
    ("parent", "System.LinkTypes.Hierarchy-Reverse"),
    ("child", "System.LinkTypes.Hierarchy-Forward"),
    ("blocks", "System.LinkTypes.Dependency-Forward"),
    ("blocked_by", "System.LinkTypes.Dependency-Reverse"),
    ("duplicate_of", "System.LinkTypes.Duplicate-Forward"),
    ("relates_to", "System.LinkTypes.Related"),
])
def test_relation_kind_mapping(kind: str, rel: str) -> None:
    assert _RELATION_FORWARD[kind] == rel
    assert _ado_rel_to_kind(rel) == kind


def test_unknown_ado_rel_returns_none() -> None:
    assert _ado_rel_to_kind("ArtifactLink") is None


# ---------- add_relation ----------------------------------------------------


def test_add_relation_emits_json_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        # Pre-flight GET: returns empty relations so the duplicate check passes.
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({"id": 5, "relations": []})
        if req.method == "PATCH" and "/workitems/5" in req.url.path:
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            assert req.headers.get("Content-Type") == "application/json-patch+json"
            return _json({"id": 5})
        # add_relation now also batch-fetches the target's title + state
        # so the returned Relation is populated.
        if req.url.path.endswith("/_apis/wit/workitemsbatch"):
            ids = json.loads(req.content.decode("utf-8"))["ids"]
            return _json({
                "value": [
                    {
                        "id": wid,
                        "fields": {
                            "System.Title": f"target {wid}",
                            "System.State": "Active",
                        },
                    }
                    for wid in ids
                ]
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    rel = AzureDevOpsProvider().add_relation(
        _project(), token="t", ticket_id="5", kind="child", target="9"
    )
    patch = captured["patch"]
    assert len(patch) == 1
    op = patch[0]
    assert op["op"] == "add"
    assert op["path"] == "/relations/-"
    assert op["value"]["rel"] == "System.LinkTypes.Hierarchy-Forward"
    assert op["value"]["url"].endswith("/_apis/wit/workItems/9")
    assert rel.kind == "child"
    assert rel.ticket_id == "#9"
    # Title + state now populated via the batch lookup.
    assert rel.title == "target 9"
    assert rel.state == "Active"
    # resolved=True: add_relation responses are built from live API data.
    assert rel.resolved is True


def test_add_relation_unsupported_kind_raises() -> None:
    with pytest.raises(RelationKindUnsupported) as exc:
        AzureDevOpsProvider().add_relation(
            _project(), token="t", ticket_id="5", kind="closes", target="9"
        )
    assert exc.value.kind == "closes"
    assert exc.value.provider == "azuredevops"
    assert "child" in exc.value.supported_kinds


def test_add_relation_cross_project_target_rejected() -> None:
    with pytest.raises(NotImplementedError) as exc:
        AzureDevOpsProvider().add_relation(
            _project(),
            token="t",
            ticket_id="5",
            kind="relates_to",
            target="other/proj/repo#9",
        )
    assert "cross-project" in str(exc.value)


# ---------- remove_relation -------------------------------------------------


def test_remove_relation_finds_index_and_emits_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "ArtifactLink",
                        "url": "vstfs:///Build/Build/100",
                    },
                    {
                        "rel": "System.LinkTypes.Hierarchy-Forward",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    },
                ],
            })
        if req.method == "PATCH" and "/workitems/5" in req.url.path:
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json({"id": 5})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().remove_relation(
        _project(), token="t", ticket_id="5", kind="child", target="9"
    )
    assert result["removed"] is True
    assert set(result.keys()) == {"removed"}
    op = captured["patch"][0]
    assert op["op"] == "remove"
    # Index 1 in the relations array.
    assert op["path"] == "/relations/1"


def test_remove_relation_not_found_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({"id": 5, "relations": []})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    # `tools/relations.py` documents that removing a non-existent relation
    # surfaces as `{"error": ...}` via `_safe` — that's a LookupError at
    # provider level. The structured RelationNotFound subclass carries
    # typed attributes and is still a LookupError.
    with pytest.raises(RelationNotFound) as exc:
        AzureDevOpsProvider().remove_relation(
            _project(), token="t", ticket_id="5", kind="child", target="9"
        )
    assert exc.value.kind == "child"
    assert exc.value.ticket_id == "5"
    assert "#9" in exc.value.target
    # Must still be a LookupError for _safe wrapper compatibility.
    assert isinstance(exc.value, LookupError)
    msg = str(exc.value)
    assert "child" in msg
    assert "#5" in msg
    assert "#9" in msg


# ---------- remove_relation duplicate_of (ticket #146) ----------------------


def test_remove_relation_duplicate_of_strips_body_and_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_relation(kind='duplicate_of') must:
    1. Issue the relations-array remove PATCH first.
    2. Strip the 'Duplicate of #9' line from System.Description.
    3. Issue a second PATCH setting System.State to the resolved open
       state and System.Description to the stripped body.
    4. Return {'removed': True}.
    """
    html = _markdown_to_html("Duplicate of #9\n\nOriginal body")
    patches: list[list[dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "/workitems/5" in path and "workitemtypes" not in path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "System.LinkTypes.Duplicate-Forward",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    },
                ],
                "fields": {
                    "System.Description": html,
                    "System.WorkItemType": "Bug",
                },
            })
        if req.method == "GET" and "workitemtypes/Bug/states" in path:
            return _json({"value": [
                {"name": "New", "category": "Proposed"},
                {"name": "Active", "category": "InProgress"},
                {"name": "Closed", "category": "Completed"},
            ]})
        if req.method == "PATCH" and "/workitems/5" in path:
            patches.append(json.loads(req.content.decode("utf-8")))
            return _json({"id": 5})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().remove_relation(
        _project(), token="t", ticket_id="5", kind="duplicate_of", target="9"
    )
    assert result == {"removed": True}
    assert len(patches) == 2, "expected relations-remove PATCH + body/state PATCH"

    remove_patch, reopen_patch = patches
    assert remove_patch[0]["op"] == "remove"
    assert remove_patch[0]["path"] == "/relations/0"

    state_ops = [op for op in reopen_patch if op.get("path") == "/fields/System.State"]
    desc_ops = [op for op in reopen_patch if op.get("path") == "/fields/System.Description"]
    assert state_ops and state_ops[0]["value"] == "New"
    assert desc_ops
    new_markdown = _html_to_markdown(desc_ops[0]["value"])
    assert "Duplicate of #9" not in new_markdown
    assert "Original body" in new_markdown


def test_remove_relation_duplicate_of_partial_id_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing 'Duplicate of #9' must not also eat a 'Duplicate of #90' line."""
    html = _markdown_to_html("Duplicate of #9\n\nDuplicate of #90\n\nBody")

    captured_desc: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "/workitems/5" in path and "workitemtypes" not in path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "System.LinkTypes.Duplicate-Forward",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    },
                ],
                "fields": {
                    "System.Description": html,
                    "System.WorkItemType": "Bug",
                },
            })
        if req.method == "GET" and "workitemtypes/Bug/states" in path:
            return _json({"value": [{"name": "New", "category": "Proposed"}]})
        if req.method == "PATCH" and "/workitems/5" in path:
            body = json.loads(req.content.decode("utf-8"))
            for op in body:
                if op.get("path") == "/fields/System.Description":
                    captured_desc.append(op["value"])
            return _json({"id": 5})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().remove_relation(
        _project(), token="t", ticket_id="5", kind="duplicate_of", target="9"
    )
    assert result == {"removed": True}
    assert captured_desc, "System.Description PATCH op missing"
    new_markdown = _html_to_markdown(captured_desc[0])
    lines = new_markdown.splitlines()
    assert "Duplicate of #9" not in lines
    assert "Duplicate of #90" in lines
    assert "Body" in new_markdown


def test_remove_relation_duplicate_of_preserves_ai_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI-generated bodies stay ai-generated after strip+reopen; a
    non-AI body stays non-AI (re-stamped as ai-modified, never
    ai-generated)."""

    def _run(markdown_body: str) -> str:
        html = _markdown_to_html(markdown_body)
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if req.method == "GET" and "/workitems/5" in path and "workitemtypes" not in path:
                return _json({
                    "id": 5,
                    "relations": [
                        {
                            "rel": "System.LinkTypes.Duplicate-Forward",
                            "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                        },
                    ],
                    "fields": {
                        "System.Description": html,
                        "System.WorkItemType": "Bug",
                    },
                })
            if req.method == "GET" and "workitemtypes/Bug/states" in path:
                return _json({"value": [{"name": "New", "category": "Proposed"}]})
            if req.method == "PATCH" and "/workitems/5" in path:
                body = json.loads(req.content.decode("utf-8"))
                for op in body:
                    if op.get("path") == "/fields/System.Description":
                        captured["desc"] = op["value"]
                return _json({"id": 5})
            raise AssertionError(f"unexpected {req.method} {req.url.path}")

        _install_mock(monkeypatch, handler)
        AzureDevOpsProvider().remove_relation(
            _project(), token="t", ticket_id="5", kind="duplicate_of", target="9"
        )
        _cache_clear_all()
        return _html_to_markdown(captured["desc"])

    ai_body = apply_body_marker(
        "Duplicate of #9\n\nOriginal body", will_be_ai_generated=True
    )
    new_ai_markdown = _run(ai_body)
    assert has_ai_generated_marker(new_ai_markdown)

    non_ai_body = "Duplicate of #9\n\nOriginal body"
    new_non_ai_markdown = _run(non_ai_body)
    assert not has_ai_generated_marker(new_non_ai_markdown)


def test_remove_relation_duplicate_of_states_fetch_failure_falls_back_to_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing states lookup must not block the reopen — fall back to 'New'."""
    html = _markdown_to_html("Duplicate of #9\n\nOriginal body")
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "/workitems/5" in path and "workitemtypes" not in path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "System.LinkTypes.Duplicate-Forward",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    },
                ],
                "fields": {
                    "System.Description": html,
                    "System.WorkItemType": "Bug",
                },
            })
        if req.method == "GET" and "workitemtypes/Bug/states" in path:
            return _json({"message": "boom"}, status_code=500)
        if req.method == "PATCH" and "/workitems/5" in path:
            body = json.loads(req.content.decode("utf-8"))
            captured["patch"] = body
            return _json({"id": 5})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().remove_relation(
        _project(), token="t", ticket_id="5", kind="duplicate_of", target="9"
    )
    assert result == {"removed": True}
    state_ops = [op for op in captured["patch"] if op.get("path") == "/fields/System.State"]
    assert state_ops and state_ops[0]["value"] == "New"


def test_remove_relation_non_duplicate_kind_has_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kind='child' must only issue the relations-array remove PATCH —
    no states GET, no body/state PATCH."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "System.LinkTypes.Hierarchy-Forward",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    },
                ],
                "fields": {
                    "System.Description": "<p>Duplicate of #9</p>",
                    "System.WorkItemType": "Bug",
                },
            })
        if req.method == "PATCH" and "/workitems/5" in req.url.path:
            return _json({"id": 5})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    seen = _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().remove_relation(
        _project(), token="t", ticket_id="5", kind="child", target="9"
    )
    assert result == {"removed": True}
    # Exactly one GET (relations lookup) and one PATCH (relations remove).
    gets = [r for r in seen if r.method == "GET"]
    patches = [r for r in seen if r.method == "PATCH"]
    assert len(gets) == 1
    assert len(patches) == 1
    assert not any("workitemtypes" in r.url.path for r in seen)


# ---------- _default_open_state ----------------------------------------------


def test_default_open_state_picks_first_open_category() -> None:
    states = [
        {"name": "New", "category": "Proposed"},
        {"name": "Active", "category": "InProgress"},
        {"name": "Closed", "category": "Completed"},
    ]
    assert _default_open_state(states) == "New"


def test_default_open_state_falls_back_to_first_state_when_all_terminal() -> None:
    states = [
        {"name": "Closed", "category": "Completed"},
        {"name": "Removed", "category": "Removed"},
    ]
    assert _default_open_state(states) == "Closed"


def test_default_open_state_falls_back_to_new_when_states_empty() -> None:
    assert _default_open_state([]) == "New"


# ---------- add_relation duplicate guard (Issue 5) --------------------------


def test_add_relation_duplicate_raises_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-flight GET finds a matching relation → RelationAlreadyExists, PATCH never issued."""
    patch_called = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "System.LinkTypes.Hierarchy-Forward",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    },
                ],
            })
        if req.method == "PATCH":
            patch_called.append(True)
            return _json({"id": 5})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        AzureDevOpsProvider().add_relation(
            _project(), token="t", ticket_id="5", kind="child", target="9"
        )
    assert exc.value.kind == "child"
    assert exc.value.ticket_id == "5"
    assert "#9" in exc.value.target
    assert isinstance(exc.value, ValueError)
    # PATCH must never be issued when duplicate is found.
    assert not patch_called


def test_add_relation_self_relation_ado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation with ticket_id == target must raise ValueError with
    'self-relation' in the message — no HTTP call should be made."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for self-relation: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="self-relation"):
        AzureDevOpsProvider().add_relation(
            _project(), token="t", ticket_id="5", kind="child", target="5"
        )


def test_add_relation_self_relation_ado_with_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-relation guard fires when target has '#' prefix."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for self-relation: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="self-relation"):
        AzureDevOpsProvider().add_relation(
            _project(), token="t", ticket_id="5", kind="child", target="#5"
        )


def test_add_relation_duplicate_already_exists_ado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-flight GET finds an existing matching relation → RelationAlreadyExists raised."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/10" in req.url.path:
            return _json({
                "id": 10,
                "relations": [
                    {
                        "rel": "System.LinkTypes.Related",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/20",
                    },
                ],
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        AzureDevOpsProvider().add_relation(
            _project(), token="t", ticket_id="10", kind="relates_to", target="20"
        )
    assert exc.value.kind == "relates_to"
    assert exc.value.ticket_id == "10"
    assert "#20" in exc.value.target
    assert isinstance(exc.value, ValueError)


def test_add_relation_no_duplicate_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-flight GET shows a non-matching relation → PATCH fires, Relation returned."""
    patch_count = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        # Different rel type — not a match.
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
                        "url": "https://dev.azure.com/seredos/_apis/wit/workItems/9",
                    },
                ],
            })
        if req.method == "PATCH" and "/workitems/5" in req.url.path:
            patch_count.append(True)
            return _json({"id": 5})
        if req.url.path.endswith("/_apis/wit/workitemsbatch"):
            ids = json.loads(req.content.decode("utf-8"))["ids"]
            return _json({
                "value": [
                    {
                        "id": wid,
                        "fields": {
                            "System.Title": f"target {wid}",
                            "System.State": "Active",
                        },
                    }
                    for wid in ids
                ]
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    rel = AzureDevOpsProvider().add_relation(
        _project(), token="t", ticket_id="5", kind="child", target="9"
    )
    # PATCH fired exactly once.
    assert len(patch_count) == 1
    assert rel.kind == "child"
    assert rel.ticket_id == "#9"


# ---------- pipelines -------------------------------------------------------


def _build_payload(build_id: int, **overrides) -> dict:
    base = {
        "id": build_id,
        "definition": {"name": "CI"},
        "sourceBranch": "refs/heads/main",
        "sourceVersion": "abc123",
        "status": "completed",
        "result": "succeeded",
        "queueTime": "2026-05-18T10:00:00Z",
        "finishTime": "2026-05-18T10:05:00Z",
        "_links": {"web": {"href": f"https://example/builds/{build_id}"}},
    }
    base.update(overrides)
    return base


def test_list_runs_for_branch_normalises_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        # Repository id resolution
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        # Branch existence probe
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 1, "value": [{"name": "refs/heads/main"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": [_build_payload(101)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_branch(
        _project(), token="t", ref="main", limit=5
    )
    assert captured["params"]["branchName"] == "refs/heads/main"
    assert len(runs) == 1
    assert runs[0].id == "101"
    assert runs[0].conclusion == "success"
    assert resolved_refs == ["main"]


def test_list_runs_for_commit_filters_client_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({
                "value": [
                    _build_payload(1, sourceVersion="abc"),
                    _build_payload(2, sourceVersion="def"),
                    _build_payload(3, sourceVersion="abc"),
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_commit(
        _project(), token="t", sha="abc", limit=10
    )
    assert sorted(r.id for r in runs) == ["1", "3"]
    assert resolved_refs == ["abc"]


def test_get_run_includes_failure_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/build/builds/101"):
            return _json(_build_payload(101, result="failed"))
        if path.endswith("/_apis/build/builds/101/timeline"):
            return _json({
                "records": [
                    {
                        "id": "j1",
                        "type": "Job",
                        "name": "Build",
                        "result": "failed",
                        "log": {"id": 5, "url": "x"},
                    },
                    {
                        "id": "j2",
                        "type": "Job",
                        "name": "OK",
                        "result": "succeeded",
                    },
                ]
            })
        if path.endswith("/_apis/build/builds/101/logs/5"):
            return httpx.Response(
                status_code=200,
                content=b"line1\nline2\nERROR boom\n",
                headers={"Content-Type": "text/plain"},
            )
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().get_run(
        _project(), token="t", run_id="101", include_failure_excerpt=True
    )
    assert run.conclusion == "failure"
    assert run.failure is not None
    assert len(run.failure.failing_jobs) == 1
    job = run.failure.failing_jobs[0]
    assert job.name == "Build"
    assert "ERROR boom" in (job.log_excerpt or "")


# ---------- token probe -----------------------------------------------------


def test_token_probe_success_returns_all_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/connectionData"):
            return _json({"authenticatedUser": {"id": "u1"}})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    caps = AzureDevOpsProvider().probe_token_capabilities(_project(), "PAT")
    assert caps.reason is None
    assert caps.issues_create is True
    assert caps.pulls_merge is True


def test_token_probe_401_means_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "TF400813"}, status_code=401)

    _install_mock(monkeypatch, handler)
    caps = AzureDevOpsProvider().probe_token_capabilities(_project(), "PAT")
    assert caps.reason == "bad_credentials"
    assert not caps.issues_create


def test_token_probe_403_means_invisible(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "forbidden"}, status_code=403)

    _install_mock(monkeypatch, handler)
    caps = AzureDevOpsProvider().probe_token_capabilities(_project(), "PAT")
    assert caps.reason == "repo_invisible_to_token"


def test_token_probe_empty_token() -> None:
    caps = AzureDevOpsProvider().probe_token_capabilities(_project(), "")
    assert caps.reason == "bad_credentials"


# ---------- duplicate_of double-count guard ---------------------------------


def test_duplicate_of_not_double_counted_as_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 5: a work item whose body mentions '#27' AND has a typed
    Duplicate-Forward relation to #27 must yield exactly ONE Relation for
    #27 (kind='duplicate_of'), not a second 'mentions' Relation.
    """
    from lib_python_projects.providers.azuredevops import _build_work_item_url

    raw = {
        "id": 10,
        "fields": {
            "System.Title": "Source",
            "System.Description": "<p>Duplicate of #27</p>",
            "System.State": "Active",
        },
        "relations": [
            {
                "rel": "System.LinkTypes.Duplicate-Forward",
                "url": "https://dev.azure.com/seredos/_apis/wit/workItems/27",
            }
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        # Batch fetch for title+state of related items.
        if req.url.path.endswith("/_apis/wit/workitemsbatch"):
            return _json({
                "value": [
                    {
                        "id": 27,
                        "fields": {
                            "System.Title": "Target 27",
                            "System.State": "Active",
                        },
                    }
                ]
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    provider = AzureDevOpsProvider()
    p = _project()
    relations = provider._build_relations_from_work_item(p, "t", raw, "10")

    # Exactly one relation for #27.
    rels_27 = [r for r in relations if r.ticket_id == "#27"]
    assert len(rels_27) == 1, (
        f"Expected exactly 1 relation for #27, got {len(rels_27)}: {rels_27}"
    )
    assert rels_27[0].kind == "duplicate_of", (
        f"Expected kind='duplicate_of', got {rels_27[0].kind!r}"
    )
    # No spurious 'mentions' relation for #27.
    mentions_27 = [r for r in relations if r.ticket_id == "#27" and r.kind == "mentions"]
    assert mentions_27 == [], f"Found unexpected mentions relation: {mentions_27}"


# ---------- refs URL parsing -----------------------------------------------
# TODO(ports-adapters): re-enable nach API-Stabilisierung
# `refs.normalize_id` lives in agent-project-issues (tool-layer URL
# parser), not in this lib. Skip when not importable.

_refs_unavailable = normalize_id is None


def test_refs_parses_work_item_url() -> None:
    if _refs_unavailable:
        pytest.skip("refs.normalize_id lives in agent-project-issues plugin")
    p = _project()
    assert normalize_id(
        "https://dev.azure.com/seredos/azure-tests/_workitems/edit/123", p
    ) == "123"


def test_refs_parses_pr_url() -> None:
    if _refs_unavailable:
        pytest.skip("refs.normalize_id lives in agent-project-issues plugin")
    p = _project()
    assert normalize_id(
        "https://dev.azure.com/seredos/azure-tests/_git/azure-tests/pullrequest/77",
        p,
    ) == "77"


def test_refs_rejects_url_for_wrong_project() -> None:
    if _refs_unavailable:
        pytest.skip("refs.normalize_id lives in agent-project-issues plugin")
    p = _project()
    with pytest.raises(ValueError) as exc:
        normalize_id(
            "https://dev.azure.com/seredos/other-proj/_workitems/edit/123", p
        )
    assert "other-proj" in str(exc.value)


def test_refs_rejects_url_for_wrong_repo() -> None:
    if _refs_unavailable:
        pytest.skip("refs.normalize_id lives in agent-project-issues plugin")
    p = _project()
    with pytest.raises(ValueError) as exc:
        normalize_id(
            "https://dev.azure.com/seredos/azure-tests/_git/other-repo/pullrequest/77",
            p,
        )
    assert "other-repo" in str(exc.value)


# ---------- post-#40 bug-fix coverage ---------------------------------------


def test_list_runs_for_tag_returns_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools/pipelines.py expects `(runs, resolved_refs)`. Previously
    Azure returned a bare list which raised ValueError on unpack."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [_build_payload(101)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_tag(
        _project(), token="t", tag="v1.0", limit=5
    )
    assert len(runs) == 1
    assert resolved_refs == ["v1.0"]


def test_list_runs_for_tag_empty_returns_empty_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No builds AND the tag-existence probe reports the tag doesn't
    exist → no resolved_refs (tool layer triggers the hint)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": []})
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 0, "value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_tag(
        _project(), token="t", tag="v1.0", limit=5
    )
    assert runs == []
    assert resolved_refs == []


def test_list_runs_for_tag_exists_but_no_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No builds reference the tag, but the tag itself exists (refs probe
    finds it) → ([], [tag]) so callers can tell "tag found, nothing
    linked" apart from "tag not found"."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": []})
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            assert req.url.params.get("filter") == "tags/v1.0"
            return _json({"count": 1, "value": [{"name": "refs/tags/v1.0"}]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_tag(
        _project(), token="t", tag="v1.0", limit=5
    )
    assert runs == []
    assert resolved_refs == ["v1.0"]


def test_list_runs_for_tag_found_skips_existence_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When builds already match the tag's branchName filter, the
    tag-existence probe (and repo-id resolution) must NOT be called."""
    requested_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requested_paths.append(req.url.path)
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [_build_payload(101)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_tag(
        _project(), token="t", tag="v1.0", limit=5
    )
    assert len(runs) == 1
    assert resolved_refs == ["v1.0"]
    assert not any(p.endswith("/_apis/git/repositories") for p in requested_paths)
    assert not any("/refs" in p for p in requested_paths)


def test_list_runs_for_ticket_returns_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolved refs are `build/{id}` markers for each ArtifactLink we
    actually walked."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/5" in path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "ArtifactLink",
                        "url": "vstfs:///Build/Build/42",
                        "attributes": {"name": "Build"},
                    },
                ],
            })
        if path.endswith("/_apis/build/builds/42"):
            return _json(_build_payload(42))
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_ticket(
        _project(), token="t", ticket_id="5", limit=10
    )
    assert len(runs) == 1
    assert resolved_refs == ["build/42"]


def test_check_400_with_workitem_typekey_becomes_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO returns 400 for several semantic-404 cases; we re-tag those
    so the tool-layer `_rewrap_404` can add context."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "Work item 9999 does not exist",
                "typeKey": "WorkItemNotFoundException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_ticket(_project(), token="t", ticket_id="9999")
    assert exc.value.status == 404


def test_check_400_transition_appends_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad status transition surfaces the GitHub/GitLab hint."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "Bogus is not a valid state for work item type Task",
                "typeKey": "WorkItemTransitionDeniedException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_ticket(_project(), token="t", ticket_id="9999")
    assert "list_ticket_statuses" in str(exc.value)


def test_check_400_with_comment_typekey_becomes_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_comment on a missing id used to surface as raw 400.
    CommentNotFoundException must be re-tagged as 404 so the tool
    layer's `_rewrap_404` adds the `comment '...' not found` context.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "The specified comment does not exist",
                "typeKey": "CommentNotFoundException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_comment(
            _project(), token="t", comment_id="99999", ticket_id="5",
        )
    assert exc.value.status == 404


def test_check_transition_hint_via_allowed_list_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some ADO state-validation errors don't say "transition" but use
    "is not in the allowed list" — the hint must still fire."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "The value 'Bogus' is not in the allowed list",
                "typeKey": "RuleValidationException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_ticket(_project(), token="t", ticket_id="5")
    assert "list_ticket_statuses" in str(exc.value)


def test_pipeline_get_run_kwarg_is_failure_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool layer passes `include_failure_excerpt`; the provider
    signature must accept that exact name (not the historical
    `include_failure_context` which existed on GitLab/Azure)."""
    import inspect

    sig = inspect.signature(AzureDevOpsProvider.get_run)
    assert "include_failure_excerpt" in sig.parameters
    assert "include_failure_context" not in sig.parameters

    from lib_python_projects.providers.gitlab import GitLabProvider

    sig_gl = inspect.signature(GitLabProvider.get_run)
    assert "include_failure_excerpt" in sig_gl.parameters
    assert "include_failure_context" not in sig_gl.parameters


def test_refs_accepts_visualstudio_com_legacy_url() -> None:
    if _refs_unavailable:
        pytest.skip("refs.normalize_id lives in agent-project-issues plugin")
    p = _project(path="seredos/azure-tests/azure-tests")
    assert normalize_id(
        "https://seredos.visualstudio.com/azure-tests/_workitems/edit/55", p
    ) == "55"


def test_refs_bare_hash_and_number_pass_through() -> None:
    if _refs_unavailable:
        pytest.skip("refs.normalize_id lives in agent-project-issues plugin")
    p = _project()
    assert normalize_id("#7", p) == "7"
    assert normalize_id(123, p) == "123"


# ---------- UX1: list_ticket_statuses hint scope narrowing --------------------


def test_check_invalid_argument_title_no_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """InvalidArgumentValueException with a Title-empty message must NOT
    receive the list_ticket_statuses hint — that exception type has been
    removed from _TRANSITION_TYPE_KEYS because it fires on non-state errors."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "TF401232: Work item field Title cannot be empty.",
                "typeKey": "InvalidArgumentValueException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_ticket(_project(), token="t", ticket_id="5")
    assert "list_ticket_statuses" not in str(exc.value)


def test_check_invalid_argument_state_still_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """InvalidArgumentValueException whose message contains a state-value
    fragment (e.g. "allowed values") must still get the hint — via the
    _TRANSITION_MSG_FRAGMENTS message-matching path."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "The value 'Bogus' is not in the allowed values for System.State",
                "typeKey": "InvalidArgumentValueException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_ticket(_project(), token="t", ticket_id="5")
    assert "list_ticket_statuses" in str(exc.value)


# ---------- add_relation duplicate_of ----------------------------------------


def test_add_relation_duplicate_of_appends_body_marker_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(kind='duplicate_of') must:
    1. Issue the relation-link PATCH first.
    2. GET the source work item to read description + type.
    3. GET workitemtypes/Issue/states for the closed state.
    4. Issue a second PATCH that sets System.Description (containing
       'Duplicate of #5') and System.State to 'Closed'.
    5. Return Relation(kind='duplicate_of', ticket_id='#5').
    """
    body_close_captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Relation-link PATCH and body+close PATCH both target /workitems/10.
        if req.method == "PATCH" and "/workitems/10" in path:
            body = json.loads(req.content.decode("utf-8"))
            # Distinguish by patch ops: relation-link patch has path "/relations/-"
            if any(op.get("path") == "/relations/-" for op in body):
                return _json({"id": 10})
            # Body+close patch has fields ops.
            body_close_captured["patch"] = body
            return _json({"id": 10})
        # GET source work item
        if req.method == "GET" and "/workitems/10" in path and "workitemtypes" not in path:
            return _json({
                "id": 10,
                "fields": {
                    "System.Description": "<p>Original body</p>",
                    "System.WorkItemType": "Issue",
                },
            })
        # GET workitemtypes/Issue/states
        if req.method == "GET" and "workitemtypes/Issue/states" in path:
            return _json({"value": [
                {"name": "Active", "category": "InProgress"},
                {"name": "Closed", "category": "Completed"},
            ]})
        # workitemsbatch for target title+state lookup
        if req.url.path.endswith("/_apis/wit/workitemsbatch"):
            ids = json.loads(req.content.decode("utf-8"))["ids"]
            return _json({
                "value": [
                    {
                        "id": wid,
                        "fields": {
                            "System.Title": f"target {wid}",
                            "System.State": "Active",
                        },
                    }
                    for wid in ids
                ]
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    rel = AzureDevOpsProvider().add_relation(
        _project(), token="t", ticket_id="10", kind="duplicate_of", target="5"
    )
    assert rel.kind == "duplicate_of"
    assert rel.ticket_id == "#5"

    # Verify the body+close patch was captured.
    assert body_close_captured, "body+close PATCH was never issued"
    patch_ops = body_close_captured["patch"]
    desc_ops = [op for op in patch_ops if op.get("path") == "/fields/System.Description"]
    state_ops = [op for op in patch_ops if op.get("path") == "/fields/System.State"]
    assert desc_ops, "System.Description op missing from body+close patch"
    assert state_ops, "System.State op missing from body+close patch"
    assert "Duplicate of #5" in (desc_ops[0].get("value") or ""), (
        "body must contain 'Duplicate of #5'"
    )
    assert state_ops[0].get("value") == "Closed"


# ---------- Issue #17 defect fixes -------------------------------------------


def test_get_run_404_names_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_run that receives a 404 must re-raise naming the project and run_id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {"message": "Build not found"},
            status_code=404,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_run(_project(), token="t", run_id="9999")
    assert exc.value.status == 404
    assert "pipeline 'azure-tests#9999' not found" in exc.value.message


def test_get_run_non_numeric_run_id_raises_404_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_run with a non-numeric run_id must raise AzureDevOpsError(404)
    proactively without making any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for non-numeric id")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_run(_project(), token="t", run_id="not-a-number")
    assert exc.value.status == 404
    assert "not-a-number" in exc.value.message


def test_list_runs_for_branch_branch_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refs endpoint returns count=0 → branch does not exist → ([], [])."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 0, "value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_branch(
        _project(), token="t", ref="nonexistent", limit=5
    )
    assert runs == []
    assert resolved_refs == []


def test_list_runs_for_commit_no_matching_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No build has the requested sourceVersion AND the commit-existence
    probe reports the commit doesn't exist → resolved_refs == []
    (issue #135: commit-not-found vs. commit-exists-no-builds)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({
                "value": [
                    _build_payload(10, sourceVersion="aaaa"),
                    _build_payload(11, sourceVersion="bbbb"),
                ]
            })
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if req.url.path.endswith("/_apis/git/repositories/repo-guid/commits/cccc"):
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_commit(
        _project(), token="t", sha="cccc", limit=10
    )
    assert runs == []
    assert resolved_refs == []


def test_list_runs_for_commit_exists_but_no_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit exists (probe succeeds) but no build references it →
    ([], [sha]) so callers can tell "commit found, nothing linked" apart
    from "commit not found"."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [_build_payload(10, sourceVersion="aaaa")]})
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if req.url.path.endswith("/_apis/git/repositories/repo-guid/commits/cccc"):
            return _json({"commitId": "cccc"})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_commit(
        _project(), token="t", sha="cccc", limit=10
    )
    assert runs == []
    assert resolved_refs == ["cccc"]


def test_list_runs_for_commit_found_skips_existence_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When filtered builds already match, the commit-existence probe
    (and repo-id resolution) must NOT be called — a match already proves
    the commit exists."""
    requested_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requested_paths.append(req.url.path)
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({
                "value": [
                    _build_payload(1, sourceVersion="abc"),
                    _build_payload(2, sourceVersion="def"),
                ]
            })
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_commit(
        _project(), token="t", sha="abc", limit=10
    )
    assert [r.id for r in runs] == ["1"]
    assert resolved_refs == ["abc"]
    assert not any("/repositories" in p and p.endswith("/commits/abc") for p in requested_paths)
    assert not any(p.endswith("/_apis/git/repositories") for p in requested_paths)


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_runs_for_branch_nonpositive_limit_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError without any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        AzureDevOpsProvider().list_runs_for_branch(
            _project(), token="t", ref="main", limit=bad_limit,
        )


def test_check_invalid_argument_allowed_values_non_state_no_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """InvalidArgumentValueException with 'allowed values' in the message but
    NOT about a state field must NOT trigger the list_ticket_statuses hint.
    (Ticket #17 issue 4: the fragment match was firing on non-state errors.)
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": (
                    "The field 'AssignedTo' contains a value that is not"
                    " in the allowed values for this field."
                ),
                "typeKey": "InvalidArgumentValueException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_ticket(_project(), token="t", ticket_id="5")
    assert "list_ticket_statuses" not in str(exc.value)


# ---------- Ticket #57: PL4 — BuildNotFoundException 400 → 404 remap ---------


def test_get_run_400_build_not_found_type_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO returns 400 with typeKey='BuildNotFoundException' for a missing build.
    The _check remap must treat this as 404 so get_run wraps it as
    'pipeline not found'."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "Build 9999 does not exist",
                "typeKey": "BuildNotFoundException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_run(_project(), token="t", run_id="9999")
    assert exc.value.status == 404
    assert "pipeline 'azure-tests#9999' not found" in exc.value.message


def test_get_run_400_build_not_found_type_key_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """typeKey='BuildNotFoundException' alone (message has no known fragment) must
    still trigger the 400→404 remap so the tool layer can add context."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "Unrecognized build identifier",
                "typeKey": "BuildNotFoundException",
            },
            status_code=400,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_run(_project(), token="t", run_id="9999")
    assert exc.value.status == 404
    assert "pipeline 'azure-tests#9999' not found" in exc.value.message


# ---------- list_runs_recent -------------------------------------------------


def test_list_runs_recent_sends_no_branch_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unfiltered call has no `branchName` but does set `$top`."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_recent(
        _project(), token="t"
    )
    assert "branchName" not in captured["params"]
    assert "$top" in captured["params"]
    assert resolved_refs == []
    assert runs == []


def test_list_runs_recent_status_in_progress_sends_status_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status='in_progress'` must send a `statusFilter` param."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", status="in_progress"
    )
    assert "statusFilter" in captured["params"]
    assert captured["params"]["statusFilter"] == "in_progress"


def test_list_runs_recent_status_all_omits_status_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status='all'` must not send a `statusFilter` param."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", status="all"
    )
    assert "statusFilter" not in captured["params"]


def test_list_runs_recent_returns_mapped_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returned runs are mapped PipelineRun objects; resolved_refs is []."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [_build_payload(55)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_recent(
        _project(), token="t"
    )
    assert resolved_refs == []
    assert len(runs) == 1
    assert runs[0].id == "55"


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_runs_recent_nonpositive_limit_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError without any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        AzureDevOpsProvider().list_runs_recent(
            _project(), token="t", limit=bad_limit,
        )
