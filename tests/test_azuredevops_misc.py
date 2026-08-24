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
    _RELATION_WRITE,
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
    # kind="child" writes Hierarchy-Reverse (ticket #171: write table is the
    # inverse of the read table so add_relation(X, "child", Y) means X is Y's
    # child).
    assert op["value"]["rel"] == "System.LinkTypes.Hierarchy-Reverse"
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
                        # kind="child" writes/matches Hierarchy-Reverse
                        # (ticket #171 write table).
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
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
                        # kind="child" writes/matches Hierarchy-Reverse
                        # (ticket #171 write table).
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
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
                        # kind="child" writes/matches Hierarchy-Reverse
                        # (ticket #171 write table).
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
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
                        # Different rel type — not a match (kind="child"
                        # writes/matches Hierarchy-Reverse per the ticket
                        # #171 write table, so Hierarchy-Forward here must
                        # NOT trip the duplicate guard).
                        "rel": "System.LinkTypes.Hierarchy-Forward",
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


# ---------- Ticket #171: parent/child write direction was inverted -----------


def test_add_relation_parent_emits_hierarchy_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(ticket_id='5', kind='parent', target='9'): 5 is the
    parent of 9, so the emitted native link must be Hierarchy-Forward (the
    pre-fix code emitted Hierarchy-Reverse for 'parent')."""

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({"id": 5, "relations": []})
        if req.method == "PATCH" and "/workitems/5" in req.url.path:
            captured["patch"] = json.loads(req.content.decode("utf-8"))
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
        _project(), token="t", ticket_id="5", kind="parent", target="9"
    )
    op = captured["patch"][0]
    assert op["value"]["rel"] == "System.LinkTypes.Hierarchy-Forward"
    assert rel.kind == "parent"


def test_add_relation_child_emits_hierarchy_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(ticket_id='5', kind='child', target='9'): 5 is the
    child of 9, so the emitted native link must be Hierarchy-Reverse (the
    pre-fix code emitted Hierarchy-Forward for 'child')."""

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({"id": 5, "relations": []})
        if req.method == "PATCH" and "/workitems/5" in req.url.path:
            captured["patch"] = json.loads(req.content.decode("utf-8"))
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
    op = captured["patch"][0]
    assert op["value"]["rel"] == "System.LinkTypes.Hierarchy-Reverse"
    assert rel.kind == "child"


def test_remove_relation_parent_matches_hierarchy_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_relation(kind='parent') must match/remove a native
    Hierarchy-Forward link, mirroring add_relation('parent')."""

    captured: dict = {}

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
        if req.method == "PATCH" and "/workitems/5" in req.url.path:
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            return _json({"id": 5})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().remove_relation(
        _project(), token="t", ticket_id="5", kind="parent", target="9"
    )
    assert result == {"removed": True}
    op = captured["patch"][0]
    assert op["op"] == "remove"
    assert op["path"] == "/relations/0"


def test_remove_relation_child_matches_hierarchy_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_relation(kind='child') must match/remove a native
    Hierarchy-Reverse link, mirroring add_relation('child')."""

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/workitems/5" in req.url.path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
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
    assert result == {"removed": True}
    op = captured["patch"][0]
    assert op["op"] == "remove"
    assert op["path"] == "/relations/0"


def test_ado_read_mapping_unchanged_after_write_flip() -> None:
    """The read basis (`_RELATION_FORWARD` / `_ado_rel_to_kind`) must stay
    untouched by the write-direction fix, while `_RELATION_WRITE` carries
    the corrected (flipped) parent/child mapping."""
    assert _ado_rel_to_kind("System.LinkTypes.Hierarchy-Reverse") == "parent"
    assert _ado_rel_to_kind("System.LinkTypes.Hierarchy-Forward") == "child"
    assert _RELATION_FORWARD["parent"] == "System.LinkTypes.Hierarchy-Reverse"
    assert _RELATION_FORWARD["child"] == "System.LinkTypes.Hierarchy-Forward"
    assert _RELATION_WRITE["parent"] == "System.LinkTypes.Hierarchy-Forward"
    assert _RELATION_WRITE["child"] == "System.LinkTypes.Hierarchy-Reverse"


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


# ---------- ticket #152: structured annotations (_normalize_az_issues) ------


def test_get_run_failure_context_normalizes_timeline_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed timeline record carrying an `issues` array with
    `data.sourcePath`/`lineNumber` (and a case-variant key form,
    `sourcepath`) must come back on `FailingJob.annotations` as mapped
    `FailureAnnotation`s (ticket #152)."""
    from lib_python_projects.providers.base import FailureAnnotation

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
                        "issues": [
                            {
                                "type": "error",
                                "message": "compile error",
                                "data": {
                                    "sourcePath": "src/main.cs",
                                    "lineNumber": "42",
                                },
                            },
                            {
                                "type": "warning",
                                "message": "unused variable",
                                # Case-variant key form.
                                "data": {
                                    "sourcepath": "src/other.cs",
                                    "linenumber": 7,
                                },
                            },
                        ],
                    },
                ]
            })
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().get_run(
        _project(), token="t", run_id="101", include_failure_excerpt=True
    )
    assert run.failure is not None
    assert len(run.failure.failing_jobs) == 1
    job = run.failure.failing_jobs[0]
    assert job.annotations == [
        FailureAnnotation(
            step="Build", message="compile error",
            file="src/main.cs", line=42, severity="error",
        ),
        FailureAnnotation(
            step="Build", message="unused variable",
            file="src/other.cs", line=7, severity="warning",
        ),
    ]


class TestNormalizeAzIssues:
    """Table-driven unit tests for `_normalize_az_issues` — a pure
    function, no HTTP involved (ticket #152)."""

    def test_no_issues_key_returns_empty_list(self) -> None:
        from lib_python_projects.providers.azuredevops import _normalize_az_issues

        assert _normalize_az_issues({"name": "Build"}) == []

    def test_empty_issues_list_returns_empty_list(self) -> None:
        from lib_python_projects.providers.azuredevops import _normalize_az_issues

        assert _normalize_az_issues({"name": "Build", "issues": []}) == []

    def test_non_numeric_line_number_yields_none(self) -> None:
        from lib_python_projects.providers.azuredevops import _normalize_az_issues

        rec = {
            "name": "Build",
            "issues": [
                {
                    "type": "error",
                    "message": "boom",
                    "data": {"sourcePath": "a.cs", "lineNumber": "not-a-number"},
                },
            ],
        }
        out = _normalize_az_issues(rec)
        assert len(out) == 1
        assert out[0].line is None
        assert out[0].file == "a.cs"

    def test_missing_data_yields_none_file_and_line(self) -> None:
        from lib_python_projects.providers.azuredevops import _normalize_az_issues

        rec = {"name": "Build", "issues": [{"type": "error", "message": "boom"}]}
        out = _normalize_az_issues(rec)
        assert out[0].file is None
        assert out[0].line is None
        assert out[0].message == "boom"
        assert out[0].step == "Build"

    def test_missing_message_defaults_to_empty_string(self) -> None:
        from lib_python_projects.providers.azuredevops import _normalize_az_issues

        rec = {"name": "Build", "issues": [{"type": "error"}]}
        out = _normalize_az_issues(rec)
        assert out[0].message == ""

    def test_step_uses_record_name(self) -> None:
        from lib_python_projects.providers.azuredevops import _normalize_az_issues

        rec = {"name": "Lint", "issues": [{"message": "x"}, {"message": "y"}]}
        out = _normalize_az_issues(rec)
        assert [a.step for a in out] == ["Lint", "Lint"]


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
        if req.url.path.endswith("/_apis/build/definitions"):
            # ticket #209: is_ci_configured probe — report CI as
            # configured so no "no-ci" sentinel lands in resolved_refs.
            return _json({"value": [{"id": 1, "name": "CI"}]})
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


def test_list_runs_for_ticket_limit_applied_after_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #200 fix-pass regression: a matching build outside the
    naive first-`limit` slice of `build_ids` must still be found.

    Three `build_ids` are walked (10, 11, 12) with `limit=2`. Only build
    12 — the third, i.e. NOT in the naive `build_ids[:2]` slice — matches
    `workflow="release"`; builds 10/11 are a different definition. If
    `list_runs_for_ticket` truncated `build_ids` to `limit` BEFORE
    fetching/filtering (the bug), build 12 would never be fetched and the
    result would be falsely empty. The fix fetches/maps every build id
    and lets `apply_run_filters` apply both the `workflow` filter and the
    final `limit` slice.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/5" in path:
            return _json({
                "id": 5,
                "relations": [
                    {
                        "rel": "ArtifactLink",
                        "url": "vstfs:///Build/Build/10",
                        "attributes": {"name": "Build"},
                    },
                    {
                        "rel": "ArtifactLink",
                        "url": "vstfs:///Build/Build/11",
                        "attributes": {"name": "Build"},
                    },
                    {
                        "rel": "ArtifactLink",
                        "url": "vstfs:///Build/Build/12",
                        "attributes": {"name": "Build"},
                    },
                ],
            })
        if path.endswith("/_apis/build/builds/10"):
            return _json(_build_payload(10, definition={"name": "CI"}))
        if path.endswith("/_apis/build/builds/11"):
            return _json(_build_payload(11, definition={"name": "CI"}))
        if path.endswith("/_apis/build/builds/12"):
            return _json(_build_payload(12, definition={"name": "release"}))
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_ticket(
        _project(), token="t", ticket_id="5", limit=2, workflow="release",
    )
    assert [r.id for r in runs] == ["12"]
    assert resolved_refs == ["build/10", "build/11", "build/12"]


def test_list_runs_for_ticket_404_names_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 (epic #224 / ticket #219/#220.3): a genuine 404 on the work
    item fetch (the ticket itself doesn't exist) keeps raising — shape
    unchanged — but the message must be normalized to the canonical
    `ticket '<project>#<id>' not found` shape rather than leaking the
    raw provider text."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/5" in path:
            return _json(
                {"message": "TF401232: Work item 5 does not exist."},
                status_code=404,
            )
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().list_runs_for_ticket(
            _project(), token="t", ticket_id="5", limit=10
        )
    assert exc.value.status == 404
    assert "ticket 'azure-tests#5' not found" in exc.value.message


def test_list_runs_for_ticket_project_404_not_relabelled_as_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Epic #224 review round 2, blocking finding 2: `list_runs_for_ticket`'s
    work-item GET is built from `_project_scope(project)` with no preceding
    existence check — unlike `get_pr`/`update_pr`/`merge_pr`, which resolve
    the repository id first and so structurally cannot conflate a repo-404
    with a PR-404. A genuine project-level 404 (ADO's real wording:
    `"TF200016: The following project does not exist"` — see
    `tests/test_azuredevops_tickets.py`'s
    `test_list_tickets_area_path_non_area_404_still_raises`) must surface
    as itself, never relabelled `ticket '...' not found` — the sibling
    case right above this one (a genuine work-item 404) is the positive
    counterpart proving a real ticket-404 still gets relabelled."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/_apis/wit/workitems/5" in path:
            return _json(
                {"message": "TF200016: The following project does not exist"},
                status_code=404,
            )
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().list_runs_for_ticket(
            _project(), token="t", ticket_id="5", limit=10
        )
    assert exc.value.status == 404
    assert "ticket" not in exc.value.message
    assert "TF200016" in exc.value.message


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
        if req.url.path.endswith("/_apis/build/definitions"):
            # ticket #209: is_ci_configured probe — report CI as
            # configured so no "no-ci" sentinel lands in resolved_refs.
            return _json({"value": [{"id": 1, "name": "CI"}]})
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
        if req.url.path.endswith("/_apis/build/definitions"):
            # ticket #209: is_ci_configured probe — report CI as
            # configured so no "no-ci" sentinel lands in resolved_refs.
            return _json({"value": [{"id": 1, "name": "CI"}]})
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
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
        if req.url.path.endswith("/_apis/build/definitions"):
            # ticket #209: is_ci_configured probe — report CI as
            # configured so no "no-ci" sentinel lands in resolved_refs.
            return _json({"value": [{"id": 1, "name": "CI"}]})
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
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
        if req.url.path.endswith("/_apis/build/definitions"):
            # ticket #209: is_ci_configured probe — report CI as
            # configured so no "no-ci" sentinel lands in resolved_refs.
            return _json({"value": [{"id": 1, "name": "CI"}]})
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
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


# ---------- ticket #168: get_step_log -----------------------------------------


def test_get_step_log_returns_full_unbounded_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_step_log must return the FULL log body, not the [-120:] tail
    slice the failure-excerpt path applies."""
    lines = [f"line-{i}" for i in range(200)]
    full_log_text = "\n".join(lines)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/build/builds/101/logs/5"):
            return httpx.Response(
                status_code=200,
                content=full_log_text.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().get_step_log(
        _project(), token="t", run_id="101", job_id="5"
    )
    assert result == full_log_text
    assert len(result.splitlines()) == 200
    assert "line-0" in result  # no [-120:] slicing — the head must survive


def test_get_step_log_404_raises_azuredevops_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "not found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_step_log(
            _project(), token="t", run_id="101", job_id="5"
        )
    assert exc.value.status == 404


def test_get_step_log_empty_body_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200, content=b"", headers={"Content-Type": "text/plain"},
        )

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().get_step_log(
        _project(), token="t", run_id="101", job_id="5"
    )
    assert result == ""


def test_get_step_log_non_numeric_run_id_raises_404_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for non-numeric id")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_step_log(
            _project(), token="t", run_id="not-a-number", job_id="5"
        )
    assert exc.value.status == 404
    assert "not-a-number" in exc.value.message


def test_get_run_to_get_step_log_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FailingJob.job_id from get_run(include_failure_excerpt=True) carries
    the build *log* id (`rec["log"]["id"]`), and must be usable, unmodified,
    as get_step_log's job_id — hitting the same logs endpoint."""
    full_log_text = "full raw build log\nline 2\n"

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
                ]
            })
        if path.endswith("/_apis/build/builds/101/logs/5"):
            return httpx.Response(
                status_code=200,
                content=full_log_text.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().get_run(
        _project(), token="t", run_id="101", include_failure_excerpt=True
    )
    assert run.failure is not None
    assert len(run.failure.failing_jobs) == 1
    resolved_job_id = run.failure.failing_jobs[0].job_id
    assert resolved_job_id == "5"

    result = AzureDevOpsProvider().get_step_log(
        _project(), token="t", run_id="101", job_id=resolved_job_id
    )
    assert result == full_log_text


# ---------- ticket #200 -- run-listing filters (workflow/event/since) -------


def test_list_runs_recent_pushes_reason_filter_and_min_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": [
                _build_payload(1, reason="manual", queueTime="2026-08-21T09:30:00Z"),
            ]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, _ = AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", event="manual", since="2026-08-21T09:00:00Z",
    )
    assert captured["params"]["reasonFilter"] == "manual"
    assert captured["params"]["minTime"] == "2026-08-21T09:00:00Z"
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_workflow_numeric_id_pushes_definitions_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            raise AssertionError("numeric workflow must not trigger a definitions lookup")
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": [_build_payload(1, definition={"name": "CI"})]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, _ = AzureDevOpsProvider().list_runs_recent(_project(), token="t", workflow="42")
    assert captured["params"]["definitions"] == "42"
    # Numeric workflow has no run.name equivalent — apply_run_filters
    # must NOT re-reject the already-scoped result client-side.
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_workflow_name_resolves_to_definition_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            assert req.url.params.get("name") == "Release"
            return _json({"value": [{"id": 7, "name": "Release"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": [_build_payload(1, definition={"name": "Release"})]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, _ = AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", workflow="Release",
    )
    assert captured["params"]["definitions"] == "7"
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_workflow_unresolved_name_falls_back_client_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": []})  # no match
        if req.url.path.endswith("/_apis/build/builds"):
            assert "definitions" not in req.url.params
            return _json({"value": [
                _build_payload(1, definition={"name": "Release"}),
                _build_payload(2, definition={"name": "CI"}),
            ]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, _ = AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", workflow="Release",
    )
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_bare_workflow_filter_sees_full_raw_page_before_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 finding 1: when `_resolve_definition_id` can't resolve
    `workflow` to a build-definition id, filtering falls back to purely
    client-side matching against `run.name` — so the raw `$top` page
    fetched must not be sized to the caller's `limit`, or a genuine
    match positioned beyond the first `limit` raw results is silently
    missed, because the server already truncated the page before
    `apply_run_filters` ever saw it. Unlike most mocks in this file,
    this one actually honors `$top` (mirroring the real Azure DevOps
    API) — that's what let this bug through the existing tests
    unnoticed.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": []})  # unresolved -> client-side only
        if req.url.path.endswith("/_apis/build/builds"):
            top = int(req.url.params.get("$top", "30"))
            all_builds = [
                _build_payload(1, definition={"name": "CI"}),
                _build_payload(2, definition={"name": "Release"}),
                _build_payload(3, definition={"name": "CI"}),
            ]
            return _json({"value": all_builds[:top]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, _ = AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", workflow="Release", limit=1,
    )
    assert [r.id for r in runs] == ["2"]


def test_list_runs_recent_bare_workflow_filter_caps_raw_fetch_at_max_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 finding 1: `resolve_fetch_page_size` intentionally caps
    the raw `$top` fetch at `max_page` (200) even when the caller's
    `limit` exceeds it — an accepted, documented limitation (see its
    docstring), not something this round attempts to lift. This is a
    regression guard for that cap staying in place: `limit=500` combined
    with an unresolvable (client-side-only) `workflow` name must still
    only request/consider the first 200 raw, server-ordered builds —
    never 500.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": []})  # unresolved -> client-side only
        if req.url.path.endswith("/_apis/build/builds"):
            captured["top"] = int(req.url.params.get("$top", "30"))
            return _json({"value": [_build_payload(1, definition={"name": "CI"})]})
        if req.url.path.endswith("/_apis/git/repositories"):
            # ticket #209: the run matched nothing client-side, so
            # `list_runs_recent` falls through to the `is_ci_configured`
            # probe, which resolves the repository id first.
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", workflow="NoSuchDefinition", limit=500,
    )
    assert captured["top"] == 200


def test_list_runs_recent_resolved_workflow_name_still_reapplies_client_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 finding 2, considered and rejected: it's tempting to skip
    the client-side `run.name` re-check once a name-shaped `workflow`
    resolves to a `definitions=` id, on the theory that the server-side
    filter already scoped the query exactly. But `apply_run_filters` is
    documented as the final, *always*-authoritative pass specifically so
    a provider-native filter the server ignored still produces a correct
    result — this test is the concrete case: the mocked `/builds`
    endpoint returns runs from every definition regardless of the
    `definitions=` param sent (representing a server that didn't apply
    the filter), so only the client-side re-check narrows the page down
    to the requested workflow. Guards against reintroducing round-3
    finding 2's proposed (and rejected) optimization.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": [{"id": 42, "name": "Release"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [
                _build_payload(1, definition={"name": "CI"}),
                _build_payload(2, definition={"name": "Release"}),
            ]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, _ = AzureDevOpsProvider().list_runs_recent(
        _project(), token="t", workflow="Release", limit=5,
    )
    assert [r.id for r in runs] == ["2"]


def test_list_runs_for_branch_accepts_filter_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 1, "value": [{"name": "refs/heads/main"}]})
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": [{"id": 7, "name": "Release"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [
                _build_payload(1, definition={"name": "Release"}),
                _build_payload(2, definition={"name": "CI"}),
            ]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, refs = AzureDevOpsProvider().list_runs_for_branch(
        _project(), token="t", ref="main", workflow="Release",
    )
    assert refs == ["main"]
    assert [r.id for r in runs] == ["1"]


def test_list_runs_for_commit_limit_applied_after_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": [{"id": 7, "name": "Release"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [
                _build_payload(1, sourceVersion="deadbeef", definition={"name": "CI"}),
                _build_payload(2, sourceVersion="deadbeef", definition={"name": "Release"}),
                _build_payload(3, sourceVersion="deadbeef", definition={"name": "Release"}),
            ]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, refs = AzureDevOpsProvider().list_runs_for_commit(
        _project(), token="t", sha="deadbeef", workflow="Release", limit=1,
    )
    assert refs == ["deadbeef"]
    assert [r.id for r in runs] == ["2"]


# ---------- ticket #200 -- trigger_pipeline / wait_for_run -------------------


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(azure_mod, "_trigger_sleep", lambda seconds: None)


def test_trigger_pipeline_numeric_workflow_posts_and_round_trips_get_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith("/_apis/build/builds"):
            post_calls.append(req)
            return _json(_build_payload(555, reason="manual"))
        if path.endswith("/_apis/build/builds/555"):
            return _json(_build_payload(555, reason="manual"))
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().trigger_pipeline(
        _project(), token="t", workflow="42", ref="main",
    )
    assert len(post_calls) == 1
    assert run is not None
    assert run.id == "555"
    body = json.loads(post_calls[0].content)
    assert body["definition"]["id"] == 42
    assert body["sourceBranch"] == "refs/heads/main"


def test_trigger_pipeline_wait_false_returns_mapped_run_without_get_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return _json(_build_payload(555, reason="manual"))

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().trigger_pipeline(
        _project(), token="t", workflow="42", ref="main", wait=False,
    )
    assert run is not None
    assert run.id == "555"
    assert len(calls) == 1  # only the POST — no follow-up get_run


def test_trigger_pipeline_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Bad Request"}, status_code=400)

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError):
        AzureDevOpsProvider().trigger_pipeline(_project(), token="t", workflow="42", ref="main")


def test_trigger_pipeline_unresolvable_workflow_name_raises_404_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": []})
        raise AssertionError(f"no build POST expected: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError):
        AzureDevOpsProvider().trigger_pipeline(
            _project(), token="t", workflow="does-not-exist", ref="main",
        )


def test_trigger_pipeline_empty_workflow_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty workflow")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError):
        AzureDevOpsProvider().trigger_pipeline(_project(), token="t", workflow="")


def test_trigger_pipeline_empty_ref_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty ref")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError):
        AzureDevOpsProvider().trigger_pipeline(_project(), token="t", workflow="42", ref="")


def test_wait_for_run_standalone_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_sleep(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 1, "value": [{"name": "refs/heads/main"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [_build_payload(1, queueTime="2026-08-21T10:05:00Z")]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", ref="main",
    )
    assert run is not None
    assert run.id == "1"


def test_wait_for_run_two_post_since_runs_oldest_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sleep(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 1, "value": [{"name": "refs/heads/main"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [
                _build_payload(1, queueTime="2026-08-21T10:00:05Z"),
                _build_payload(2, queueTime="2026-08-21T10:00:01Z"),
            ]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", ref="main",
    )
    assert run is not None
    assert run.id == "2"


def test_wait_for_run_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_sleep(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 1, "value": [{"name": "refs/heads/main"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", ref="main", timeout=0.05,
    )
    assert run is None


def test_wait_for_run_without_since_raises_type_error() -> None:
    with pytest.raises(TypeError):
        AzureDevOpsProvider().wait_for_run(_project(), token="t", ref="main")


# ---------- ticket #209 -- CI workflow discovery -----------------------------


def test_list_workflows_maps_build_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    """`list_workflows` maps `/_apis/build/definitions` (scoped to this
    repository) into `Workflow` objects."""
    from lib_python_projects.providers.base import Workflow

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if req.url.path.endswith("/_apis/build/definitions"):
            assert req.url.params.get("repositoryId") == "repo-guid"
            assert req.url.params.get("repositoryType") == "TfsGit"
            return _json({"value": [{
                "id": 7, "name": "CI", "path": "\\",
                "queueStatus": "enabled",
                "_links": {"web": {"href": "https://dev.azure.com/x/_build?definitionId=7"}},
            }]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    workflows = AzureDevOpsProvider().list_workflows(_project(), token="t")
    assert workflows == [Workflow(
        id="7", name="CI", path="\\", state="enabled",
        url="https://dev.azure.com/x/_build?definitionId=7",
        dispatch_target="7",
    )]


def test_list_workflows_empty_on_no_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    assert AzureDevOpsProvider().list_workflows(_project(), token="t") == []


def test_is_ci_configured_true_and_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler_configured(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        return _json({"value": [{"id": 7, "name": "CI"}]})

    _install_mock(monkeypatch, handler_configured)
    assert AzureDevOpsProvider().is_ci_configured(_project(), token="t") is True

    def handler_not_configured(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        return _json({"value": []})

    _install_mock(monkeypatch, handler_not_configured)
    assert AzureDevOpsProvider().is_ci_configured(_project(), token="t") is False


def test_list_runs_for_branch_appends_no_ci_sentinel_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test (ticket #209): branch exists, no builds, and the
    repository has no build definitions at all → the uniform
    `NO_CI_SENTINEL` is appended as the last element of resolved_refs."""
    from lib_python_projects.providers.base import NO_CI_SENTINEL

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in req.url.path:
            return _json({"count": 1, "value": [{"name": "refs/heads/main"}]})
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": []})
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_branch(
        _project(), token="t", ref="main",
    )
    assert runs == []
    assert resolved_refs == ["main", NO_CI_SENTINEL]


def test_list_runs_recent_appends_no_ci_sentinel_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test (ticket #209): no builds at all, and no build
    definitions configured → `([], [NO_CI_SENTINEL])`."""
    from lib_python_projects.providers.base import NO_CI_SENTINEL

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": []})
        if req.url.path.endswith("/_apis/build/definitions"):
            return _json({"value": []})
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_recent(_project(), token="t")
    assert runs == []
    assert resolved_refs == [NO_CI_SENTINEL]


def test_wait_for_run_never_probes_ci_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard (ticket #209): `wait_for_run` must poll through
    the unprobed helper — a strict handler that raises on the
    `/_apis/build/definitions` probe path, combined with a timeout that
    forces several empty polls, proves the probe is never hit. (The repo
    id lookup for the branch-existence check is a separate concern and
    is not exercised here since `ref` is omitted.)"""
    _no_sleep(monkeypatch)
    poll_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            poll_count["n"] += 1
            return _json({"value": []})
        raise AssertionError(f"unexpected request (probe?): {req.url.path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", timeout=0.05,
    )
    assert run is None
    assert poll_count["n"] >= 1


def test_dispatch_target_round_trips_into_trigger_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The numeric definition id `dispatch_target` from `list_workflows`
    works verbatim as the `workflow` argument to `trigger_pipeline` —
    unique, so it avoids the name-resolution 404s a bare display name
    could hit."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if path.endswith("/_apis/build/definitions"):
            return _json({"value": [{"id": 7, "name": "CI"}]})
        if req.method == "POST" and path.endswith("/_apis/build/builds"):
            body = json.loads(req.content)
            assert body["definition"]["id"] == 7
            return _json(_build_payload(555, reason="manual"))
        if path.endswith("/_apis/build/builds/555"):
            return _json(_build_payload(555, reason="manual"))
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    workflows = AzureDevOpsProvider().list_workflows(_project(), token="t")
    assert workflows[0].dispatch_target == "7"

    run = AzureDevOpsProvider().trigger_pipeline(
        _project(), token="t", workflow=workflows[0].dispatch_target, ref="main",
    )
    assert run is not None
    assert run.id == "555"


def test_wait_for_run_matches_tag_ref_via_refs_tags_prefix_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 finding 3: Azure DevOps's `sourceBranch` keeps a tag's
    `refs/tags/` prefix in place — `_map_build_run` only strips
    `refs/heads/` (see its `branch = ... .removeprefix("refs/heads/")`),
    so `run.branch` here is `"refs/tags/v1.2.3"` while `wait_for_run` is
    called with the bare `"v1.2.3"` ref. The two sides genuinely differ
    in shape, unlike the existing GitHub/GitLab indirect tag tests (which
    compare the same bare string on both sides and so never actually
    exercised `run_matches_ref`'s prefix-stripping). ADO is the provider
    most likely to need this exercised end-to-end.
    """
    _no_sleep(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": [
                _build_payload(
                    9, sourceBranch="refs/tags/v1.2.3",
                    queueTime="2026-08-21T10:05:00Z",
                ),
            ]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    run = AzureDevOpsProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", ref="v1.2.3",
    )
    assert run is not None
    assert run.id == "9"


# ---------- ticket #200 -- get_ref -------------------------------------------


def _refs_response(entries: list[dict]) -> httpx.Response:
    return _json({"count": len(entries), "value": entries})


def test_get_ref_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        path = req.url.path
        if "/_apis/git/repositories/repo-guid/refs" in path:
            filt = req.url.params.get("filter")
            if filt == "heads/main":
                return _refs_response([{"name": "refs/heads/main", "objectId": "branchsha1"}])
            return _refs_response([])
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ref = AzureDevOpsProvider().get_ref(_project(), token="t", ref="main")
    assert ref is not None
    assert ref.kind == "branch"
    assert ref.sha == "branchsha1"


def test_get_ref_annotated_tag_uses_peeled_object_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            filt = req.url.params.get("filter")
            if filt == "heads/v2.0.0":
                return _refs_response([])
            if filt == "tags/v2.0.0":
                return _refs_response([{
                    "name": "refs/tags/v2.0.0",
                    "objectId": "tagobjsha1",
                    "peeledObjectId": "commitsha2",
                }])
            return _refs_response([])
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ref = AzureDevOpsProvider().get_ref(_project(), token="t", ref="v2.0.0")
    assert ref is not None
    assert ref.kind == "tag"
    # sha must be the *peeled commit* sha, not the tag object's own sha.
    assert ref.sha == "commitsha2"


def test_get_ref_lightweight_tag_uses_object_id_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            filt = req.url.params.get("filter")
            if filt == "heads/v1.0.0":
                return _refs_response([])
            if filt == "tags/v1.0.0":
                return _refs_response([{
                    "name": "refs/tags/v1.0.0", "objectId": "commitsha1",
                }])
            return _refs_response([])
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ref = AzureDevOpsProvider().get_ref(_project(), token="t", ref="v1.0.0")
    assert ref is not None
    assert ref.kind == "tag"
    assert ref.sha == "commitsha1"


def test_get_ref_commit_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "c" * 40

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            return _refs_response([])
        if f"/_apis/git/repositories/repo-guid/commits/{sha}" in path:
            return _json({"commitId": sha})
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ref = AzureDevOpsProvider().get_ref(_project(), token="t", ref=sha)
    assert ref is not None
    assert ref.kind == "commit"
    assert ref.sha == sha


def test_get_ref_unknown_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            return _refs_response([])
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    ref = AzureDevOpsProvider().get_ref(_project(), token="t", ref="does-not-exist")
    assert ref is None


def test_get_ref_branch_shadows_same_named_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            filt = req.url.params.get("filter")
            if filt == "heads/shared":
                return _refs_response([{
                    "name": "refs/heads/shared", "objectId": "branchsha-shared",
                }])
            raise AssertionError("tag lookup must not fire when the branch matched")
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    ref = AzureDevOpsProvider().get_ref(_project(), token="t", ref="shared")
    assert ref is not None
    assert ref.kind == "branch"
    assert ref.sha == "branchsha-shared"


# ---------- ticket #200 -- list_releases -------------------------------------


def test_list_releases_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            return _refs_response([])
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    releases = AzureDevOpsProvider().list_releases(_project(), token="t")
    assert releases == []


def test_list_releases_annotated_tag_maps_message_and_date_hard_false_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            return _refs_response([{
                "name": "refs/tags/v1.0.0",
                "objectId": "tagobjsha1",
                "peeledObjectId": "commitsha3",
            }])
        if "/_apis/git/repositories/repo-guid/annotatedtags/tagobjsha1" in path:
            return _json({
                "objectId": "tagobjsha1",
                "name": "v1.0.0",
                "message": "Release notes here",
                "taggedBy": {"date": "2026-01-01T00:00:00Z"},
                "url": "https://dev.azure.com/annotatedtags/tagobjsha1",
            })
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    releases = AzureDevOpsProvider().list_releases(_project(), token="t")
    assert len(releases) == 1
    rel = releases[0]
    assert rel.tag == "v1.0.0"
    assert rel.sha == "commitsha3"
    assert rel.body == "Release notes here"
    assert rel.created_at == "2026-01-01T00:00:00Z"
    assert rel.published_at == "2026-01-01T00:00:00Z"
    # Not representable on Azure DevOps — always False.
    assert rel.draft is False
    assert rel.prerelease is False


def test_list_releases_lightweight_tag_has_empty_body_and_hard_false_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            return _refs_response([{
                "name": "refs/tags/v0.9.0", "objectId": "commitsha4",
            }])
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    releases = AzureDevOpsProvider().list_releases(_project(), token="t")
    assert len(releases) == 1
    rel = releases[0]
    assert rel.tag == "v0.9.0"
    assert rel.sha == "commitsha4"
    assert rel.body == ""
    assert rel.draft is False
    assert rel.prerelease is False


def test_list_releases_sorted_most_recent_first_before_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 finding 2: the docstring promises "most recent first",
    but the implementation used to slice `tag_refs[:limit]` *before*
    fetching/sorting by tag date — so when the API's raw ref order
    doesn't happen to already be date-sorted (it is not guaranteed to
    be), an older tag could be kept over a newer one. Here the API
    lists the older tag first; with `limit=1`, only the genuinely most
    recent release must survive.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json({"value": [{"id": "repo-guid", "name": "azure-tests"}]})
        if "/_apis/git/repositories/repo-guid/refs" in path:
            return _refs_response([
                {
                    "name": "refs/tags/v1.0.0",
                    "objectId": "tagobjsha1",
                    "peeledObjectId": "commitsha1",
                },
                {
                    "name": "refs/tags/v2.0.0",
                    "objectId": "tagobjsha2",
                    "peeledObjectId": "commitsha2",
                },
            ])
        if "/_apis/git/repositories/repo-guid/annotatedtags/tagobjsha1" in path:
            return _json({
                "objectId": "tagobjsha1", "name": "v1.0.0",
                "message": "old", "taggedBy": {"date": "2026-01-01T00:00:00Z"},
                "url": "https://dev.azure.com/annotatedtags/tagobjsha1",
            })
        if "/_apis/git/repositories/repo-guid/annotatedtags/tagobjsha2" in path:
            return _json({
                "objectId": "tagobjsha2", "name": "v2.0.0",
                "message": "new", "taggedBy": {"date": "2026-06-01T00:00:00Z"},
                "url": "https://dev.azure.com/annotatedtags/tagobjsha2",
            })
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    releases = AzureDevOpsProvider().list_releases(_project(), token="t", limit=1)
    assert [r.tag for r in releases] == ["v2.0.0"]
