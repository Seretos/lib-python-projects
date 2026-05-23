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
)
from lib_python_projects.providers.base import RelationKindUnsupported

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
    # provider level. GitHub does the same; Azure now matches.
    with pytest.raises(LookupError) as exc:
        AzureDevOpsProvider().remove_relation(
            _project(), token="t", ticket_id="5", kind="child", target="9"
        )
    msg = str(exc.value)
    assert "child" in msg
    assert "#5" in msg
    assert "#9" in msg


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
        if req.url.path.endswith("/_apis/build/builds"):
            captured["params"] = dict(req.url.params)
            return _json({"value": [_build_payload(101)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    runs = AzureDevOpsProvider().list_runs_for_branch(
        _project(), token="t", ref="main", limit=5
    )
    assert captured["params"]["branchName"] == "refs/heads/main"
    assert len(runs) == 1
    assert runs[0].id == "101"
    assert runs[0].conclusion == "success"


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
    runs = AzureDevOpsProvider().list_runs_for_commit(
        _project(), token="t", sha="abc", limit=10
    )
    assert sorted(r.id for r in runs) == ["1", "3"]


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
    """No builds → no resolved_refs (tool layer triggers the hint)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _json({"value": []})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = AzureDevOpsProvider().list_runs_for_tag(
        _project(), token="t", tag="v1.0", limit=5
    )
    assert runs == []
    assert resolved_refs == []


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
