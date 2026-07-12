"""Tests for ticket #49 — GitHub vs GitLab provider parity fixes.

Covers the 11 findings beyond what existing tests already exercise:
status vocab + pipeline status kwarg + url canonicalisation + sigil +
atomic add_relation + timestamp normalisation + label sort.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers import gitlab as gitlab_provider
from lib_python_projects.providers import azuredevops as azuredevops_provider
from lib_python_projects.providers.base import (
    normalize_timestamp,
    RelationKind,
    RelationNotFound,
    READ_ONLY_RELATION_KINDS,
    WRITABLE_RELATION_KINDS,
    TicketFilters,
)
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider, _canonical_url
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)


def _github_project(path: str = "Seretos/agent-project-issues") -> ProjectConfig:
    return ProjectConfig(id="github-tests", provider="github", path=path)


def _gitlab_project(path: str = "Seredos/gitlab-tests") -> ProjectConfig:
    return ProjectConfig(id="gitlab-tests", provider="gitlab", path=path)


def _resp(payload, status_code: int = 200, headers: dict | None = None):
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_gitlab_mock(monkeypatch, handler):
    def wrapped(req):
        return handler(req)
    transport = httpx.MockTransport(wrapped)

    def fake_client(project, token):
        return httpx.Client(
            base_url=f"{(project.base_url or 'https://gitlab.com').rstrip('/')}/api/v4",
            headers={"Accept": "application/json"},
            transport=transport,
        )
    monkeypatch.setattr(gitlab_provider, "_client", fake_client)


def _install_github_mock(monkeypatch, handler):
    def wrapped(req):
        return handler(req)
    transport = httpx.MockTransport(wrapped)

    def fake_client(token):
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers={"Accept": "application/vnd.github+json"},
            transport=transport,
        )
    monkeypatch.setattr(github_provider, "_client", fake_client)


# ---------- finding 1: GitLab pipeline status kwarg + tuple return ----------


def test_gitlab_list_runs_for_branch_accepts_status_kwarg(monkeypatch):
    """Was a TypeError crash — see ticket #49 finding 1. Now `status`
    is accepted and maps to GitLab's `scope` param."""
    captured: dict = {}

    def handler(req):
        if "/repository/branches/" in str(req.url):
            return _resp({"commit": {"id": "sha-main"}})
        captured["scope"] = req.url.params.get("scope", "")
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    _, _ = GitLabProvider().list_runs_for_branch(
        _gitlab_project(), "t", "main", status="completed",
    )
    assert captured["scope"] == "finished"


def test_gitlab_list_runs_for_branch_status_all_omits_scope(monkeypatch):
    captured: dict = {}

    def handler(req):
        if "/repository/branches/" in str(req.url):
            return _resp({"commit": {"id": "sha-main"}})
        captured["scope"] = req.url.params.get("scope", None)
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    _, _ = GitLabProvider().list_runs_for_branch(
        _gitlab_project(), "t", "main", status="all",
    )
    # `all` maps to None → no scope query param at all.
    assert captured["scope"] in (None, "")


def test_gitlab_list_runs_for_ticket_returns_tuple(monkeypatch):
    """Was `list[PipelineRun]`, now `(runs, resolved_refs)` to mirror GitHub."""
    def handler(req):
        if req.url.path.endswith("/related_merge_requests"):
            return _resp([{"iid": 7}])
        if "/merge_requests/7/pipelines" in req.url.path:
            return _resp([])
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    runs, refs = GitLabProvider().list_runs_for_ticket(
        _gitlab_project(), "t", "5", status="completed",
    )
    assert runs == []
    assert refs == ["!7"]


# ---------- finding 3 + 4: GitLab URL canonicalisation ----------------------


def test_canonical_url_lowercases_project_path():
    p = _gitlab_project(path="Seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/Seredos/gitlab-tests/-/issues/5", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/5"


def test_canonical_url_rewrites_work_items_to_issues():
    p = _gitlab_project(path="seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/seredos/gitlab-tests/-/work_items/5", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/5"


def test_canonical_url_handles_anchor():
    p = _gitlab_project(path="Seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/Seredos/gitlab-tests/-/issues/5#note_99", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/5#note_99"


def test_canonical_url_combined_lowercase_and_rewrite():
    p = _gitlab_project(path="Seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/Seredos/gitlab-tests/-/work_items/12", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/12"


def test_canonical_url_noop_when_url_empty():
    assert _canonical_url("", _gitlab_project()) == ""


def test_canonical_url_noop_when_path_already_lowercase():
    p = _gitlab_project(path="seredos/gitlab-tests")
    url = "https://gitlab.com/seredos/gitlab-tests/-/issues/5"
    assert _canonical_url(url, p) == url


def test_gitlab_relations_url_canonicalised(monkeypatch):
    """Side-finding on #49 F4 from test-agent live-verify:
    `relations[*].url` from the issue-links and closed_by endpoints
    still showed `/-/work_items/N` because `_fetch_relations` bypassed
    `_canonical_url`. Both code paths now route through it."""
    issue_with_link = {
        "iid": 5, "title": "T", "description": "",
        "state": "opened", "author": {"username": "a"},
        "assignees": [], "labels": [],
        "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    link_row = {
        "iid": 7,
        "title": "Other",
        "state": "opened",
        "link_type": "blocks",
        "references": {"relative": "#7"},
        # Same shape as live GitLab — the link's web_url comes back in
        # the work_items beta family.
        "web_url": "https://gitlab.com/Seredos/gitlab-tests/-/work_items/7",
    }
    closed_by_mr = {
        "iid": 9,
        "title": "Auto-close MR",
        "state": "merged",
        "web_url": "https://gitlab.com/Seredos/gitlab-tests/-/merge_requests/9",
    }

    def handler(req):
        if req.url.path.endswith("/issues/5/notes"):
            return _resp([])
        if req.url.path.endswith("/issues/5/links"):
            return _resp([link_row])
        if req.url.path.endswith("/issues/5/closed_by"):
            return _resp([closed_by_mr])
        if req.url.path.endswith("/issues/5"):
            return _resp(issue_with_link)
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    _ticket, _comments, relations, _trunc = GitLabProvider().get_ticket(
        _gitlab_project(), "t", "5", include_relations=True,
    )
    by_kind = {r.kind: r for r in relations}
    # `blocks` relation URL canonicalised: lowercase path AND `/-/issues/`.
    assert by_kind["blocks"].url == (
        "https://gitlab.com/seredos/gitlab-tests/-/issues/7"
    )
    # `closed_by` MR keeps the merge_requests family but path is lowercased.
    assert by_kind["closed_by"].url == (
        "https://gitlab.com/seredos/gitlab-tests/-/merge_requests/9"
    )


def test_gitlab_map_issue_canonicalises_ticket_url(monkeypatch):
    """End-to-end: a get_ticket response returns a canonicalised URL."""
    issue = {
        "iid": 5, "title": "T", "description": "",
        "state": "opened", "author": {"username": "a"},
        "assignees": [], "labels": [],
        "web_url": "https://gitlab.com/Seredos/gitlab-tests/-/work_items/5",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }

    def handler(req):
        if req.url.path.endswith("/issues/5/notes"):
            return _resp([])
        if req.url.path.endswith("/issues/5"):
            return _resp(issue)
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    ticket, _comments, _rels, _trunc = GitLabProvider().get_ticket(
        _gitlab_project(), "t", "5", include_relations=False,
    )
    assert ticket.url == (
        "https://gitlab.com/seredos/gitlab-tests/-/issues/5"
    )


# ---------- finding 2 follow-up: GitLab add_relation 404 for relates_to ------


def test_gitlab_add_relation_relates_to_uses_numeric_project_id(monkeypatch):
    """Ticket #49 finding 2 follow-up: the issue-links endpoint rejects the
    URL-encoded path for `target_project_id` — we now resolve the project's
    numeric id first and send THAT in the body.
    Note: blocks/blocked_by are no longer supported (ticket #20); this test
    uses relates_to which routes through the same code path."""
    captured: dict = {}
    seen_get_project = []

    def handler(req):
        if req.method == "GET" and req.url.path == "/api/v4/projects/Seredos/gitlab-tests":
            seen_get_project.append(True)
            return _resp({"id": 12345, "path_with_namespace": "Seredos/gitlab-tests"})
        if req.method == "POST" and "/issues/5/links" in req.url.path:
            captured["body"] = json.loads(req.content.decode())
            return _resp({
                "source_issue": {"iid": 5, "title": "S", "state": "opened",
                                 "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5"},
                "target_issue": {"iid": 7, "title": "T", "state": "opened",
                                 "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/7"},
            })
        return _resp({}, status_code=404)

    _install_gitlab_mock(monkeypatch, handler)
    rel = GitLabProvider().add_relation(
        _gitlab_project(), "tok", "5", "relates_to", "#7",
    )
    assert seen_get_project, "Should resolve project numeric id before posting"
    assert captured["body"]["target_project_id"] == 12345  # numeric, not path
    assert captured["body"]["link_type"] == "relates_to"
    assert rel.kind == "relates_to"


def test_gitlab_add_relation_blocks_raises_unsupported(monkeypatch):
    """blocks is now an unsupported kind on GitLab (ticket #20 — license-gated).
    The guard fires before any HTTP call."""
    from lib_python_projects.providers.base import RelationKindUnsupported

    def handler(req):
        raise AssertionError("no HTTP call expected for unsupported kind")

    _install_gitlab_mock(monkeypatch, handler)
    with pytest.raises(RelationKindUnsupported) as exc:
        GitLabProvider().add_relation(
            _gitlab_project(), "tok", "5", "blocks", "#7",
        )
    assert exc.value.kind == "blocks"


def test_gitlab_add_relation_relates_to_propagates_404_from_resolver(monkeypatch):
    """If the project-id resolver 404s, the link write never fires and
    no body is mutated (atomic semantics preserved)."""
    def handler(req):
        if req.method == "GET" and "/api/v4/projects/" in req.url.path:
            return _resp({"message": "Not Found"}, status_code=404)
        if req.method == "POST" and "/issues/5/links" in req.url.path:
            raise AssertionError("POST must not fire when project resolver 404s")
        return _resp({}, status_code=404)

    _install_gitlab_mock(monkeypatch, handler)
    from lib_python_projects.providers.gitlab import GitLabError
    with pytest.raises(GitLabError):
        GitLabProvider().add_relation(
            _gitlab_project(), "tok", "5", "relates_to", "#7",
        )


# ---------- finding 5 + 6: status vocab single source of truth --------------


def test_gitlab_rejects_github_style_status_alias(monkeypatch):
    def handler(req):
        if req.method == "GET":
            return _resp({
                "iid": 5, "title": "T", "description": "",
                "state": "opened", "author": {"username": "a"},
                "assignees": [], "labels": ["ai-generated"],
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="closed:not_planned"):
        GitLabProvider().update_ticket(
            _gitlab_project(), "t", "5", status="closed:not_planned",
        )


def test_github_rejects_bare_closed_alias():
    """`closed` is no longer silently coerced to `closed:completed`
    on GitHub — the agent must use an exact `list_statuses` value."""
    from lib_python_projects.providers.github import _split_github_status
    with pytest.raises(ValueError, match="unsupported status 'closed'"):
        _split_github_status("closed")


def test_gitlab_status_error_mirrors_list_statuses():
    """Per #49 finding 6: the rejection message advertises exactly the
    `list_statuses` vocabulary, not a wider GitHub-style alias set."""
    from lib_python_projects.providers.gitlab import _status_to_state_event
    with pytest.raises(ValueError) as excinfo:
        _status_to_state_event("bogus")
    msg = str(excinfo.value)
    assert "Accepted: open, closed." in msg
    # The GitHub-style aliases must NOT appear in the GitLab error.
    assert "closed:completed" not in msg
    assert "closed:not_planned" not in msg


def test_azuredevops_status_error_matches_github_gitlab_shape(monkeypatch):
    """Per #143: Azure DevOps's status-rejection message must converge on
    the same shape as GitHub/GitLab — provider name, `list_ticket_statuses`
    hint, and a comma-prose `Accepted: ...` list (no Python list repr)."""
    _cache_clear_all()

    def handler(req):
        if "/_apis/wit/workitemtypes/Task/states" in req.url.path:
            return _resp({"value": [{"name": "To Do"}, {"name": "Done"}]})
        if "/_apis/wit/workitemtypes" in req.url.path and req.method == "GET":
            return _resp({"value": [{"name": "Task"}]})
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_azuredevops_mock(monkeypatch, handler)
    with pytest.raises(ValueError) as excinfo:
        AzureDevOpsProvider().create_ticket(
            _ado_project(), "t", title="Test", body="body",
            labels=[], assignees=[], status="Bogus",
        )
    msg = str(excinfo.value)
    assert "unsupported status" in msg
    assert "Azure DevOps" in msg
    assert "list_ticket_statuses" in msg
    assert "Accepted: To Do, Done." in msg
    assert "['" not in msg and "']" not in msg


# ---------- finding 9: marker body trailing-newline asymmetry ---------------


def test_marker_canonical_form_for_empty_body():
    """Empty body on both providers produces the bare marker line —
    no trailing `\\n\\n` to differ across GitHub/GitLab."""
    from lib_python_projects.markers import apply_body_marker
    assert apply_body_marker(None, will_be_ai_generated=True) == "#ai-generated"
    assert apply_body_marker("", will_be_ai_generated=True) == "#ai-generated"


def test_marker_keeps_separator_for_nonempty_body():
    from lib_python_projects.markers import apply_body_marker
    out = apply_body_marker("Hello.", will_be_ai_generated=True)
    assert out == "#ai-generated\n\nHello."


# ---------- finding 10: timestamp precision normalisation -------------------


def test_normalize_timestamp_strips_ms_with_z():
    assert normalize_timestamp("2026-05-20T23:07:59.507Z") == "2026-05-20T23:07:59Z"


def test_normalize_timestamp_strips_ms_with_offset():
    assert normalize_timestamp("2026-05-20T23:07:59.507+02:00") == "2026-05-20T23:07:59+02:00"


def test_normalize_timestamp_passthrough_seconds():
    assert normalize_timestamp("2026-05-20T23:07:48Z") == "2026-05-20T23:07:48Z"


def test_normalize_timestamp_passthrough_empty():
    assert normalize_timestamp("") == ""
    assert normalize_timestamp(None) == ""


def test_normalize_timestamp_passthrough_unknown_shape():
    # Doesn't match the pattern → returned as-is rather than mangled.
    assert normalize_timestamp("nonsense") == "nonsense"


def test_gitlab_ticket_timestamps_are_normalised(monkeypatch):
    issue = {
        "iid": 5, "title": "T", "description": "",
        "state": "opened", "author": {"username": "a"},
        "assignees": [], "labels": [],
        "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
        "created_at": "2026-05-20T23:07:59.507Z",
        "updated_at": "2026-05-20T23:08:01.123Z",
    }

    def handler(req):
        if req.url.path.endswith("/issues/5/notes"):
            return _resp([])
        if req.url.path.endswith("/issues/5"):
            return _resp(issue)
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitLabProvider().get_ticket(
        _gitlab_project(), "t", "5", include_relations=False,
    )
    assert ticket.created_at == "2026-05-20T23:07:59Z"
    assert ticket.updated_at == "2026-05-20T23:08:01Z"


# ---------- finding 11: GitHub label ordering -------------------------------


def test_github_labels_sorted_alphabetically(monkeypatch):
    """Labels come back sorted regardless of API application order."""
    issue = {
        "number": 3,
        "title": "T",
        "body": "",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        # Intentional non-alphabetical order from the API.
        "labels": [
            {"name": "test-label"},
            {"name": "ai-generated"},
            {"name": "bug"},
        ],
        "html_url": "https://github.com/acme/backend/issues/3",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }

    def handler(req):
        if req.url.path.endswith("/issues/3/comments"):
            return _resp([])
        if req.url.path.endswith("/issues/3"):
            return _resp(issue)
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    p = ProjectConfig(id="acme", provider="github", path="acme/backend")
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        p, "t", "3", include_relations=False,
    )
    assert ticket.labels == ["ai-generated", "bug", "test-label"]


# ---------- F20: READ_ONLY_RELATION_KINDS constant ---------------------------


def test_read_only_and_writable_kinds_cover_all_relation_kind_args():
    """The union of READ_ONLY and WRITABLE must equal all RelationKind args."""
    all_kinds = set(RelationKind.__args__)
    covered = set(READ_ONLY_RELATION_KINDS) | set(WRITABLE_RELATION_KINDS)
    assert covered == all_kinds, (
        f"Missing kinds: {all_kinds - covered}; extra kinds: {covered - all_kinds}"
    )


def test_read_only_and_writable_kinds_are_disjoint():
    """No kind should appear in both sets."""
    overlap = set(READ_ONLY_RELATION_KINDS) & set(WRITABLE_RELATION_KINDS)
    assert overlap == set(), f"Kinds in both sets: {overlap}"


# ---------- F4: RelationNotFound class invariants ----------------------------


def test_relation_not_found_is_lookup_error():
    """RelationNotFound must be a LookupError subclass (tool-layer contract)."""
    err = RelationNotFound(kind="blocked_by", ticket_id="5", target="#3")
    assert isinstance(err, LookupError)


def test_relation_not_found_carries_typed_attributes():
    """RelationNotFound exposes .kind, .ticket_id, .target."""
    err = RelationNotFound(kind="child", ticket_id="10", target="#20")
    assert err.kind == "child"
    assert err.ticket_id == "10"
    assert err.target == "#20"


def test_relation_not_found_message_is_descriptive():
    """The str() of RelationNotFound contains the key facts."""
    err = RelationNotFound(kind="blocks", ticket_id="7", target="#99")
    msg = str(err)
    assert "blocks" in msg
    assert "#7" in msg
    assert "#99" in msg


# ---------- F10 regression guard: GitLab relation.state never "opened" -------


def test_gitlab_relation_state_never_raw_opened(monkeypatch):
    """Regression: GitLab link state 'opened' must surface as 'open'."""
    issue_payload = {
        "iid": 5, "title": "T", "description": "",
        "state": "opened", "author": {"username": "a"},
        "assignees": [], "labels": [],
        "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    link_row = {
        "iid": 7, "link_type": "blocks", "title": "Other",
        "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/7",
        "state": "opened",  # raw GitLab value
        "references": {"relative": "#7"},
    }

    def handler(req):
        if req.url.path.endswith("/issues/5/notes"):
            return _resp([])
        if req.url.path.endswith("/issues/5/links"):
            return _resp([link_row])
        if req.url.path.endswith("/issues/5/closed_by"):
            return _resp([])
        if req.url.path.endswith("/issues/5"):
            return _resp(issue_payload)
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    p = _gitlab_project(path="seredos/gitlab-tests")
    _ticket, _comments, relations, _trunc = GitLabProvider().get_ticket(
        p, "t", "5", include_relations=True,
    )
    blocks_rels = [r for r in relations if r.kind == "blocks"]
    assert blocks_rels, "expected a 'blocks' relation"
    for rel in blocks_rels:
        assert rel.state != "opened", (
            f"raw 'opened' leaked through as relation.state — must be 'open'"
        )
        assert rel.state == "open"


# ---------- Issue 2: GitLab add_relation returns populated Relation ----------


def test_gitlab_add_relation_relates_to_returns_populated_relation(monkeypatch):
    """POST /issues/:iid/links returns a nested shape:
    {"source_issue": {...}, "target_issue": {"iid":N,"title":"...","state":"opened","web_url":"..."}}.
    The provider must read title/state/url from target_issue, normalise the
    state, build a canonical URL, and set resolved=True.
    """
    captured: dict = {}

    def handler(req):
        # Project numeric-id resolver (called before the POST).
        if req.method == "GET" and req.url.path.endswith(
            "/projects/seredos%2Fgitlab-tests"
        ) or (req.method == "GET" and "/api/v4/projects/Seredos" in req.url.path):
            return _resp({"id": 99, "path_with_namespace": "seredos/gitlab-tests"})
        if req.method == "GET" and "/api/v4/projects/" in req.url.path and "/issues" not in req.url.path:
            return _resp({"id": 99, "path_with_namespace": "seredos/gitlab-tests"})
        if req.method == "POST" and "/issues/5/links" in req.url.path:
            captured["body"] = json.loads(req.content.decode())
            # The real GitLab shape for this endpoint.
            return _resp({
                "source_issue": {
                    "iid": 5,
                    "title": "Source issue",
                    "state": "opened",
                    "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
                },
                "target_issue": {
                    "iid": 7,
                    "title": "Target issue",
                    "state": "opened",
                    "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/7",
                },
            })
        return _resp({}, status_code=404)

    _install_gitlab_mock(monkeypatch, handler)
    rel = GitLabProvider().add_relation(
        _gitlab_project(), "tok", "5", "relates_to", "#7",
    )
    assert rel.title == "Target issue"
    assert rel.state == "open"   # normalised from "opened"
    assert rel.url != ""
    assert "issues/7" in rel.url
    assert rel.resolved is True


# ---------- Issue #19: cross-provider list_tickets limit validation ----------


def _install_azuredevops_mock(monkeypatch, handler):
    def wrapped(req):
        return handler(req)
    transport = httpx.MockTransport(wrapped)

    def fake_client(project, token):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azuredevops_provider, "_client", fake_client)


def _ado_project() -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path="seredos/azure-tests/azure-tests",
        token_env="AZURE_TOKEN",
    )


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
@pytest.mark.parametrize("provider_name,make_provider,make_project,install_mock", [
    (
        "github",
        lambda: GitHubProvider(),
        lambda: _github_project(),
        "_install_github_mock",
    ),
    (
        "gitlab",
        lambda: GitLabProvider(),
        lambda: _gitlab_project(),
        "_install_gitlab_mock",
    ),
])
def test_list_tickets_nonpositive_limit_raises_on_all_providers(
    monkeypatch,
    bad_limit: int,
    provider_name: str,
    make_provider,
    make_project,
    install_mock: str,
) -> None:
    """All providers must raise ValueError for limit <= 0 without HTTP I/O."""

    def handler(req):
        raise AssertionError(
            f"no HTTP call expected for {provider_name} with limit={bad_limit}"
        )

    # Use the appropriate mock installer.
    if install_mock == "_install_github_mock":
        _install_github_mock(monkeypatch, handler)
    else:
        _install_gitlab_mock(monkeypatch, handler)

    provider = make_provider()
    with pytest.raises(ValueError, match="positive integer"):
        provider.list_tickets(make_project(), "tok", TicketFilters(limit=bad_limit))


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_tickets_nonpositive_limit_raises_azuredevops(
    monkeypatch,
    bad_limit: int,
) -> None:
    """Azure DevOps raises ValueError for limit <= 0 without HTTP I/O."""
    from lib_python_projects.providers.azuredevops import _cache_clear_all
    _cache_clear_all()

    def handler(req):
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_azuredevops_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        AzureDevOpsProvider().list_tickets(
            _ado_project(), "tok", TicketFilters(limit=bad_limit)
        )


# ---------- ticket #30: Comment.updated_at cross-provider parity -------------


def test_comment_dataclass_exposes_updated_at():
    """Comment must have an updated_at field so all three providers return
    a consistent shape. This is a static structural assertion."""
    from lib_python_projects.providers.base import Comment
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(Comment)}
    assert "updated_at" in field_names, (
        "Comment.updated_at field is missing — all three providers "
        "populate it but the dataclass contract doesn't expose it yet."
    )


def test_github_comment_updated_at_populated(monkeypatch):
    """GitHub's _map_comment must populate updated_at from the wire payload."""
    comment_payload = {
        "id": 1,
        "user": {"login": "alice"},
        "body": "hello",
        "html_url": "https://github.com/acme/backend/issues/1#issuecomment-1",
        "created_at": "2026-05-18T10:00:00Z",
        "updated_at": "2026-05-19T12:30:00Z",
    }

    def handler(req):
        if req.url.path.endswith("/issues/1/comments"):
            return _resp([comment_payload])
        return _resp([], 404)

    _install_github_mock(monkeypatch, handler)
    p = ProjectConfig(id="acme", provider="github", path="acme/backend")
    from lib_python_projects.providers.github import GitHubProvider
    comments, _ = GitHubProvider().list_comments(p, "tok", "1")
    assert comments[0].updated_at == "2026-05-19T12:30:00Z"


def test_gitlab_comment_updated_at_populated(monkeypatch):
    """GitLab's _map_note must populate updated_at from the wire payload."""
    note_payload = {
        "id": 99,
        "body": "hi",
        "author": {"username": "bob"},
        "created_at": "2026-05-20T10:00:00.123Z",
        "updated_at": "2026-05-21T11:30:45.456Z",
        "system": False,
    }

    def handler(req):
        if "/issues/3/notes" in req.url.path:
            return _resp([note_payload])
        if "/issues/3" in req.url.path:
            return _resp({
                "iid": 3, "title": "T", "description": "",
                "state": "opened", "author": {"username": "a"},
                "assignees": [], "labels": [],
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/3",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    _ticket, comments, _rels, _trunc = GitLabProvider().get_ticket(
        _gitlab_project(), "tok", "3", include_relations=False,
    )
    assert comments[0].updated_at == "2026-05-21T11:30:45Z"


# ---------- ticket #35: label management cross-provider parity ---------------


def test_all_providers_expose_label_methods():
    """All three providers must expose the four label management methods
    as callables. This is a static structural assertion."""
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider

    label_methods = ("list_labels", "create_label", "update_label", "delete_label")
    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        for method_name in label_methods:
            assert callable(getattr(provider_cls, method_name, None)), (
                f"{provider_cls.__name__} is missing callable {method_name!r}"
            )


# ---------- ticket #70: list_runs_recent cross-provider parity ---------------


def test_all_providers_expose_list_runs_recent():
    """All three providers must expose list_runs_recent as a callable.
    This is a static structural assertion."""
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        assert callable(getattr(provider_cls, "list_runs_recent", None)), (
            f"{provider_cls.__name__} is missing callable 'list_runs_recent'"
        )


# ---------- ticket #69: delete_comment cross-provider parity -----------------


def test_all_providers_expose_delete_comment():
    """All three providers must expose delete_comment as a callable.
    This is a static structural assertion."""
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        assert callable(getattr(provider_cls, "delete_comment", None)), (
            f"{provider_cls.__name__} is missing callable 'delete_comment'"
        )


def test_all_providers_expose_list_pr_reviews():
    """All three providers must expose list_pr_reviews as a callable.
    This is a static structural assertion (ticket #148 finding 1)."""
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        assert callable(getattr(provider_cls, "list_pr_reviews", None)), (
            f"{provider_cls.__name__} is missing callable 'list_pr_reviews'"
        )


def test_pull_request_dataclass_has_reviews_field():
    """`PullRequest.reviews` is a new field (ticket #148) wiring
    `list_pr_reviews` data into `get_pr` on every provider. Static
    structural assertion mirroring `test_bulk_ticket_result_dataclass_fields`."""
    import dataclasses
    from lib_python_projects.providers.base import PullRequest

    field_names = {f.name for f in dataclasses.fields(PullRequest)}
    assert "reviews" in field_names


# ---------- ticket #35: Label dataclass and LabelOperationUnsupported --------


def test_label_dataclass_fields():
    """Label has name, color, description fields via dataclasses.fields."""
    import dataclasses
    from lib_python_projects.providers.base import Label

    field_names = {f.name for f in dataclasses.fields(Label)}
    assert "name" in field_names
    assert "color" in field_names
    assert "description" in field_names


def test_label_dataclass_defaults():
    """Label fields all default to empty string."""
    from lib_python_projects.providers.base import Label

    lbl = Label()
    assert lbl.name == ""
    assert lbl.color == ""
    assert lbl.description == ""


def test_label_operation_unsupported_carries_operation_and_provider():
    """LabelOperationUnsupported attributes survive construction."""
    from lib_python_projects.providers.base import LabelOperationUnsupported

    exc = LabelOperationUnsupported("create_label", "azuredevops")
    assert exc.operation == "create_label"
    assert exc.provider == "azuredevops"


def test_label_operation_unsupported_is_not_implemented_error():
    """LabelOperationUnsupported is a NotImplementedError subclass."""
    from lib_python_projects.providers.base import LabelOperationUnsupported

    assert issubclass(LabelOperationUnsupported, NotImplementedError)


# ---------- ticket #94: list_fields cross-provider parity --------------------


def test_all_providers_expose_list_fields():
    """All three providers must expose list_fields as a callable.
    This is a static structural assertion (mirrors test_all_providers_expose_label_methods)."""
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        assert callable(getattr(provider_cls, "list_fields", None)), (
            f"{provider_cls.__name__} is missing callable 'list_fields'"
        )


def test_github_list_fields_returns_empty():
    """GitHubProvider.list_fields returns [] without raising."""
    result = GitHubProvider().list_fields(_github_project(), None)
    assert result == []


def test_gitlab_list_fields_returns_empty():
    """GitLabProvider.list_fields returns [] without raising."""
    result = GitLabProvider().list_fields(_gitlab_project(), None)
    assert result == []


# ---------- ticket #114/#123: custom_fields cross-provider parity ------------
#
# Non-empty custom_fields on GitHub/GitLab now write real provider-native
# data (ticket #123) — see `tests/test_github_board.py` (Projects v2 field
# read/write) and `tests/test_gitlab_issues.py` (labels/milestone
# read/write) for the detailed behavioural coverage. The tests below only
# assert the cross-provider shape: real reads/writes happen, and the
# `None`/`{}` no-op contract is preserved on both providers.


def test_github_create_ticket_custom_fields_writes_real_data(monkeypatch):
    """Cross-provider parity check (ticket #123): a non-empty custom_fields
    no longer raises on GitHub — it's written via the bound
    github-projects-v2 board's GraphQL mutations. Detailed field-type and
    error-path coverage lives in test_github_board.py."""
    from lib_python_projects import Board, GithubProjectsV2Binding

    board = Board(
        columns=["Todo", "Done"],
        binding=GithubProjectsV2Binding(
            kind="github-projects-v2", owner="acme-org", project_number=7,
        ),
    )
    mutations_seen: list[str] = []

    def handler(req):
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            return _resp({
                "number": 9, "node_id": "issue-node-9",
                "title": "hi", "body": "b", "state": "open",
                "user": {"login": "a"}, "assignees": [],
                "labels": [{"name": "ai-generated"}],
                "html_url": "https://github.com/acme/backend/issues/9",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        if "/labels" in path:
            return _resp({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = json.loads(req.content.decode())
            query, variables = body["query"], body["variables"]
            if "addProjectV2ItemById" in query:
                mutations_seen.append("add")
                return _resp({"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}})
            if "updateProjectV2ItemFieldValue" in query:
                mutations_seen.append("update")
                return _resp({
                    "data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-1"}}}
                })
            if "ProjectV2FieldCommon" in query:
                owner_field = "organization" if "organization(login:" in query else "user"
                return _resp({"data": {owner_field: {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "projectV2(number:$number){id}" in query:
                owner_field = "organization" if "organization(login:" in query else "user"
                return _resp({"data": {owner_field: {"projectV2": {"id": "proj-node-id"}}}})
        raise AssertionError(f"unexpected request {req.method} {path}")

    _install_github_mock(monkeypatch, handler)
    ticket = GitHubProvider().create_ticket(
        ProjectConfig(id="acme", provider="github", path="acme/backend", board=board),
        "t", title="hi", body="b", labels=[], assignees=[],
        custom_fields={"Status": "Done"},
    )
    assert ticket.id == "9"
    assert mutations_seen == ["add", "update"]


def test_github_create_ticket_custom_fields_none_or_empty_is_noop(monkeypatch):
    """custom_fields=None/{} is a silent no-op — POST payload unchanged."""
    captured: list[dict] = []

    def handler(req):
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            captured.append(json.loads(req.content.decode()))
            return _resp({
                "number": 5, "title": "hi", "body": "b", "state": "open",
                "user": {"login": "a"}, "assignees": [],
                "labels": [{"name": "ai-generated"}],
                "html_url": "https://github.com/acme/backend/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        if "/labels" in path:
            return _resp({"name": "ai-generated", "color": "0075ca"})
        return _resp({})

    _install_github_mock(monkeypatch, handler)
    for cf in (None, {}):
        GitHubProvider().create_ticket(
            _github_project(), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields=cf,
        )
    assert len(captured) == 2
    for payload in captured:
        assert "custom_fields" not in payload
        assert payload["title"] == "hi"


def test_github_get_ticket_include_custom_fields_no_board_returns_none(monkeypatch):
    """No github-projects-v2 board configured: include_custom_fields=True
    still leaves ticket.custom_fields None, with no extra HTTP request —
    "not applicable" semantics (ticket #123). Populated-map coverage lives
    in test_github_board.py."""
    seen: list = []

    def handler(req):
        seen.append(req)
        path = req.url.path
        if path.endswith("/issues/3/comments"):
            return _resp([])
        if path.endswith("/issues/3"):
            return _resp({
                "number": 3, "title": "T", "body": "", "state": "open",
                "user": {"login": "a"}, "assignees": [], "labels": [],
                "html_url": "https://github.com/acme/backend/issues/3",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    p = ProjectConfig(id="acme", provider="github", path="acme/backend")
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        p, "t", "3", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields is None
    assert len(seen) == 2, "no extra HTTP request beyond issue GET + comments GET"


def test_gitlab_create_ticket_custom_fields_writes_real_data(monkeypatch):
    """Cross-provider parity check (ticket #123): a non-empty custom_fields
    no longer raises on GitLab — `labels` replaces the positional labels
    arg and `milestone` resolves to a milestone_id. Detailed key-rejection
    and error-path coverage lives in test_gitlab_issues.py."""
    captured: dict = {}

    def handler(req):
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _resp([])
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            return _resp([{"id": 9, "title": "v2.0"}])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _resp({
                "iid": 6, "title": "hi", "description": "b", "state": "opened",
                "author": {"username": "a"}, "assignees": [],
                "labels": ["from-custom-fields", "ai-generated"],
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/6",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    ticket = GitLabProvider().create_ticket(
        _gitlab_project(), "t", title="hi", body="b", labels=[], assignees=[],
        custom_fields={"labels": ["from-custom-fields"], "milestone": "v2.0"},
    )
    assert ticket.id == "6"
    sent_labels = captured["body"]["labels"].split(",")
    assert "from-custom-fields" in sent_labels
    assert "ai-generated" in sent_labels
    assert captured["body"]["milestone_id"] == 9


def test_gitlab_create_ticket_custom_fields_none_or_empty_is_noop(monkeypatch):
    """custom_fields=None/{} is a silent no-op — POST payload unchanged."""
    captured: list[dict] = []

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured.append(json.loads(req.content.decode()))
            return _resp({
                "iid": 5, "title": "hi", "description": "b", "state": "opened",
                "author": {"username": "a"}, "assignees": [],
                "labels": ["ai-generated"],
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    for cf in (None, {}):
        GitLabProvider().create_ticket(
            _gitlab_project(), "t", title="hi", body="b", labels=[], assignees=[],
            custom_fields=cf,
        )
    assert len(captured) == 2
    for payload in captured:
        assert "custom_fields" not in payload
        assert payload["title"] == "hi"


def test_gitlab_get_ticket_include_custom_fields_returns_labels_and_milestone(
    monkeypatch,
):
    """Cross-provider parity check (ticket #123): include_custom_fields=True
    populates a real `{"labels": [...], "milestone": ...}` map from the
    issue JSON already fetched, with no extra HTTP request. Milestone-unset
    and key-rejection coverage lives in test_gitlab_issues.py."""
    seen: list = []

    def handler(req):
        seen.append(req)
        path = req.url.path
        if path.endswith("/issues/5/notes"):
            return _resp([])
        if path.endswith("/issues/5"):
            return _resp({
                "iid": 5, "title": "T", "description": "", "state": "opened",
                "author": {"username": "a"}, "assignees": [],
                "labels": ["bug"], "milestone": {"id": 9, "title": "v2.0"},
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitLabProvider().get_ticket(
        _gitlab_project(), "t", "5", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields == {"labels": ["bug"], "milestone": "v2.0"}
    assert ticket.milestone == "v2.0"
    assert len(seen) == 2, "no extra HTTP request beyond issue GET + notes GET"


# ---------- ticket #136: Review shape contract + relation resolution -------


def _azuredevops_project() -> ProjectConfig:
    return ProjectConfig(
        id="azure-parity-tests",
        provider="azuredevops",
        path="seredos/azure-tests/azure-tests",
        token_env="AZURE_TOKEN",
    )


def _install_azuredevops_mock(monkeypatch, handler):
    def wrapped(req):
        return handler(req)
    transport = httpx.MockTransport(wrapped)

    def fake_client(project, token):
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(
            base_url=base, headers={"Accept": "application/json"}, transport=transport,
        )
    monkeypatch.setattr(azuredevops_provider, "_client", fake_client)


def test_review_author_truthy_body_url_str_or_none_across_providers(monkeypatch):
    """`Review` shape contract (ticket #136): `author` is a truthy, non-empty
    string for a known actor on every provider. `body`/`url` are `str | None`
    everywhere, but only GitLab may legitimately emit `None` on a bare
    approve (no note posted) — GitHub and Azure DevOps must still emit
    `str` (falling back to `""`) for the same case, so existing consumers
    that don't expect `None` from those two providers keep working."""

    # --- GitHub: bare approve, no body -> author/body/url stay str ---
    def gh_handler(req):
        if req.method == "POST" and req.url.path.endswith("/reviews"):
            return _resp({
                "id": 1,
                "user": {"login": "octocat"},
                "body": None,
                "html_url": "https://github.com/acme/backend/pull/7#pullrequestreview-1",
                "submitted_at": "2024-01-01T00:00:00Z",
            })
        return _resp({}, 404)

    _install_github_mock(monkeypatch, gh_handler)
    gh_review = GitHubProvider().submit_pr_review(
        _github_project(), token="t", pr_id="7", state="approve",
    )
    assert gh_review.author == "octocat" and gh_review.author
    assert isinstance(gh_review.body, str)
    assert isinstance(gh_review.url, str) and gh_review.url

    # --- GitLab: bare approve, no body -> body/url are None; author is
    # still populated (via GET /user), not the empty-string bug from #136.
    def gl_handler(req):
        if req.method == "POST" and req.url.path.endswith("/approve"):
            return _resp({"iid": 7, "web_url": "u", "updated_at": "2024-01-01T00:00:00Z"})
        if req.method == "GET" and req.url.path.endswith("/user"):
            return _resp({"id": 1, "username": "gitlab-actor"})
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, gl_handler)
    gl_review = GitLabProvider().submit_pr_review(
        _gitlab_project(), "t", "7", state="approve",
    )
    assert gl_review.author == "gitlab-actor" and gl_review.author
    assert gl_review.body is None
    assert gl_review.url is None

    # --- Azure DevOps: bare approve, no body -> author/body/url stay str ---
    _cache_clear_all()
    repo_id = "da0d7da0-6a8c-4958-aad3-be17cbf806eb"

    def ado_handler(req):
        path = req.url.path
        if path.endswith("/_apis/git/repositories") and req.method == "GET":
            return _resp({"value": [{"id": repo_id, "name": "azure-tests"}]})
        if path.endswith("/_apis/connectionData"):
            return _resp({
                "authenticatedUser": {"id": "user-guid", "displayName": "Azure Actor"},
            })
        if req.method == "PUT" and "/reviewers/user-guid" in path:
            return _resp({"id": "user-guid", "displayName": "Azure Actor", "vote": 10})
        return _resp({}, 404)

    _install_azuredevops_mock(monkeypatch, ado_handler)
    ado_review = AzureDevOpsProvider().submit_pr_review(
        _azuredevops_project(), token="t", pr_id="7", state="approve",
    )
    assert ado_review.author == "Azure Actor" and ado_review.author
    assert isinstance(ado_review.body, str)
    assert isinstance(ado_review.url, str) and ado_review.url


def test_all_providers_expose_bulk_update_tickets():
    """All three providers must expose bulk_update_tickets as a callable
    (ticket #149). This is a static structural assertion (mirrors
    test_all_providers_expose_label_methods)."""
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        assert callable(getattr(provider_cls, "bulk_update_tickets", None)), (
            f"{provider_cls.__name__} is missing callable 'bulk_update_tickets'"
        )


def test_bulk_ticket_result_dataclass_fields():
    """BulkTicketResult has ticket_id, ticket, error fields (ticket #149)."""
    import dataclasses
    from lib_python_projects.providers.base import BulkTicketResult

    field_names = {f.name for f in dataclasses.fields(BulkTicketResult)}
    assert "ticket_id" in field_names
    assert "ticket" in field_names
    assert "error" in field_names


def test_resolvable_duplicate_of_never_emitted_with_unresolved_sentinel(monkeypatch):
    """Ticket #136: when a `duplicate_of` target is independently resolvable
    (GitHub timeline / GitLab issue-links both already fetched its real
    metadata), the surviving relation must not be the unresolved sentinel
    (`resolved=False`, empty title/state) on either provider — the real
    metadata must not be discarded in favor of an earlier body-scan stub."""

    # --- GitHub ---
    def gh_handler(req):
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _resp({
                "number": 42, "title": "Issue 42", "body": "Duplicate of #9",
                "state": "open", "user": {"login": "alice"}, "assignees": [],
                "labels": [], "html_url": "https://github.com/acme/backend/issues/42",
                "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z",
            })
        if path == "/repos/acme/backend/issues/42/comments":
            return _resp([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _resp([])
        if path == "/repos/acme/backend/issues/42/timeline":
            canonical = {
                "number": 9, "title": "Canonical", "state": "open",
                "html_url": "https://github.com/acme/backend/issues/9",
                "repository": {"full_name": "acme/backend"},
            }
            return _resp([{
                "event": "marked_as_duplicate",
                "canonical": canonical,
                "dupe": {"number": 42},
            }])
        if "/dependencies/" in path:
            return _resp([])
        return _resp({}, 404)

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_github_mock(monkeypatch, gh_handler)
    _, _, gh_relations, _ = GitHubProvider().get_ticket(
        _github_project("acme/backend"), token="t", ticket_id="42",
    )
    gh_dup = next(r for r in gh_relations if r.kind == "duplicate_of" and r.ticket_id == "#9")
    assert gh_dup.resolved is True
    assert gh_dup.title and gh_dup.state

    # --- GitLab ---
    def gl_handler(req):
        url = str(req.url)
        if url.endswith("issues/5"):
            return _resp({
                "iid": 5, "title": "Issue 5", "description": "Duplicate of #1",
                "state": "opened", "author": {"username": "a"}, "assignees": [],
                "labels": [], "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
                "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z",
            })
        if "/issues/5/links" in url:
            return _resp([{
                "iid": 1, "link_type": "relates_to", "title": "Real target",
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/1",
                "state": "opened", "references": {"relative": "#1"},
            }])
        if "/issues/5/closed_by" in url or "/issues/5/notes" in url:
            return _resp([])
        return _resp([], 404)

    _install_gitlab_mock(monkeypatch, gl_handler)
    _, _, gl_relations, _ = GitLabProvider().get_ticket(_gitlab_project(), "t", "5")
    gl_dup = next(r for r in gl_relations if r.kind == "duplicate_of" and r.ticket_id == "#1")
    assert gl_dup.resolved is True
    assert gl_dup.title and gl_dup.state


# ---------- ticket #150: idempotency-key structural parity -------------------


def test_ticket_and_pull_request_expose_idempotent_replay_field():
    """`idempotent_replay` must exist (defaulting to False) on both
    result dataclasses so a replayed create_ticket/create_pr call can be
    told apart from a fresh one."""
    import dataclasses
    from lib_python_projects.providers.base import PullRequest, Ticket

    ticket_fields = {f.name for f in dataclasses.fields(Ticket)}
    pr_fields = {f.name for f in dataclasses.fields(PullRequest)}
    assert "idempotent_replay" in ticket_fields
    assert "idempotent_replay" in pr_fields


def test_all_providers_create_ticket_accepts_idempotency_key_keyword_only():
    """`create_ticket` on all three providers must accept
    `idempotency_key` as a keyword-only parameter defaulting to None,
    without disturbing any existing positional parameter (ticket #150)."""
    import inspect
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        sig = inspect.signature(provider_cls.create_ticket)
        assert "idempotency_key" in sig.parameters, (
            f"{provider_cls.__name__}.create_ticket is missing 'idempotency_key'"
        )
        param = sig.parameters["idempotency_key"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{provider_cls.__name__}.create_ticket.idempotency_key must be keyword-only"
        )
        assert param.default is None
        # Existing positional params keep their historical order/kind.
        assert sig.parameters["title"].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY,
        )


def test_all_providers_create_pr_accepts_idempotency_key_keyword_only():
    """`create_pr` on all three providers must accept `idempotency_key`
    as a keyword-only parameter defaulting to None (ticket #150)."""
    import inspect
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        sig = inspect.signature(provider_cls.create_pr)
        assert "idempotency_key" in sig.parameters, (
            f"{provider_cls.__name__}.create_pr is missing 'idempotency_key'"
        )
        param = sig.parameters["idempotency_key"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{provider_cls.__name__}.create_pr.idempotency_key must be keyword-only"
        )
        assert param.default is None
        assert sig.parameters["head"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD


# ---------- ticket #151: cross-provider ticket hierarchy / milestone parity -


def test_ticket_exposes_parent_id_and_milestone_fields():
    """`parent_id`/`milestone` are real `Ticket` fields, both defaulting to
    `None`, on the single shared dataclass every provider returns."""
    import dataclasses
    from lib_python_projects.providers.base import Ticket

    fields = {f.name: f for f in dataclasses.fields(Ticket)}
    assert "parent_id" in fields
    assert "milestone" in fields
    assert fields["parent_id"].default is None
    assert fields["milestone"].default is None


def test_all_providers_create_ticket_and_update_ticket_accept_milestone_keyword_only():
    """`milestone=` exists as a keyword-only parameter on `create_ticket`
    AND `update_ticket` for all three providers (ticket #151), defaulting
    to each module's private `_UNSET` sentinel — not `None` — so "omitted"
    and "explicitly clear" stay distinguishable across every provider."""
    import inspect
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        for method_name in ("create_ticket", "update_ticket"):
            sig = inspect.signature(getattr(provider_cls, method_name))
            assert "milestone" in sig.parameters, (
                f"{provider_cls.__name__}.{method_name} is missing 'milestone'"
            )
            param = sig.parameters["milestone"]
            assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{provider_cls.__name__}.{method_name}.milestone must be keyword-only"
            )
            # The sentinel is intentionally NOT None — None is the "clear"
            # value, and must stay distinguishable from "not provided".
            assert param.default is not None
            assert param.default is not inspect.Parameter.empty


def test_all_three_providers_round_trip_parent_id_and_milestone(monkeypatch):
    """Live (mocked) round-trip across all three providers: each exposes
    `parent_id`/`milestone` on `get_ticket` and accepts `milestone=` on
    `create_ticket`, with consistent `None`-vs-value / sentinel semantics."""
    from lib_python_projects import Board, GithubProjectsV2Binding

    # ---- GitHub: milestone via the bound board's iteration field, parent
    # via the existing sub-issues `parent` field. ----
    github_board = Board(
        columns=["Todo"],
        binding=GithubProjectsV2Binding(
            kind="github-projects-v2", owner="acme-org", project_number=7,
            iteration_field="Sprint",
        ),
    )
    github_project = ProjectConfig(
        id="gh", provider="github", path="acme/backend", board=github_board,
    )

    def github_handler(req):
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _resp({
                "number": 42, "title": "T", "body": "", "state": "open",
                "user": {"login": "a"}, "assignees": [], "labels": [],
                "html_url": "https://github.com/acme/backend/issues/42",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "parent": {
                    "number": 7, "title": "Epic", "state": "open",
                    "html_url": "https://github.com/acme/backend/issues/7",
                    "repository": {"full_name": "acme/backend"},
                },
            })
        if path == "/repos/acme/backend/issues/42/comments":
            return _resp([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _resp([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _resp([])
        if "/dependencies/" in path:
            return _resp([])
        if path == "/graphql":
            body = json.loads(req.content.decode("utf-8"))
            assert body["variables"] == {
                "owner": "acme", "repo": "backend", "number": 42,
            }
            return _resp({
                "data": {"repository": {"issue": {"projectItems": {"nodes": [
                    {
                        "project": {"number": 7},
                        "fieldValues": {"nodes": [
                            {"title": "Sprint 5", "field": {"name": "Sprint"}},
                        ]},
                    }
                ]}}}}
            })
        raise AssertionError(f"unexpected GitHub request: {req.method} {path}")

    _install_github_mock(monkeypatch, github_handler)
    gh_ticket, _c, gh_rel, _t = GitHubProvider().get_ticket(
        github_project, "t", "42",
    )
    assert gh_ticket.parent_id == "#7"
    assert gh_ticket.milestone == "Sprint 5"
    assert [r.kind for r in gh_rel if r.kind == "parent"] == ["parent"]

    # ---- GitLab: milestone via the native issue milestone; parent via
    # Work Items GraphQL hierarchyWidget. ----
    def gitlab_handler(req):
        url = str(req.url)
        if "/issues/5/notes" in url or "/issues/5/links" in url or "/issues/5/closed_by" in url:
            return _resp([])
        if url.endswith("issues/5"):
            return _resp({
                "iid": 5, "title": "T", "description": "", "state": "opened",
                "author": {"username": "a"}, "assignees": [], "labels": [],
                "milestone": {"id": 9, "title": "v2.0"},
                "web_url": "https://gitlab.com/acme/backend/-/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        if url.endswith("/api/graphql"):
            body = json.loads(req.content.decode("utf-8"))
            iid = int(body["variables"]["iid"])
            assert iid == 5
            return _resp({
                "data": {"project": {"workItems": {"nodes": [{
                    "id": "gid://gitlab/WorkItem/5", "iid": 5, "title": "T",
                    "webUrl": "https://gitlab.com/acme/backend/-/issues/5",
                    "state": "OPEN",
                    "widgets": [{"parent": {
                        "id": "gid://gitlab/WorkItem/3", "iid": 3,
                        "title": "Epic", "webUrl": "https://gitlab.com/acme/backend/-/issues/3",
                        "state": "OPEN",
                    }}],
                }]}}}
            })
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, gitlab_handler)
    gl_ticket, _c, gl_rel, _t = GitLabProvider().get_ticket(
        _gitlab_project("acme/backend"), "t", "5",
    )
    assert gl_ticket.parent_id == "#3"
    assert gl_ticket.milestone == "v2.0"
    assert [r.kind for r in gl_rel if r.kind == "parent"] == ["parent"]

    # ---- Azure DevOps: milestone via System.IterationPath; parent via
    # System.LinkTypes.Hierarchy-Reverse. ----
    def ado_handler(req):
        path = req.url.path
        if "/_apis/wit/workitems/5" in path and "/comments" not in path:
            return _resp({
                "id": 5,
                "fields": {
                    "System.Title": "T", "System.Description": "",
                    "System.State": "New", "System.IterationPath": "Proj\\Sprint 9",
                    "System.CreatedDate": "2024-01-01T00:00:00Z",
                    "System.ChangedDate": "2024-01-01T00:00:00Z",
                },
                "relations": [{
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": "https://dev.azure.com/seredos/_apis/wit/workItems/3",
                }],
            })
        if path.endswith("/_apis/wit/workItems/5/comments"):
            return _resp({"comments": [], "continuationToken": None})
        if path.endswith("/_apis/wit/workitemsbatch"):
            ids = json.loads(req.content.decode("utf-8"))["ids"]
            return _resp({
                "value": [
                    {"id": wid, "fields": {
                        "System.Title": "Epic", "System.State": "Active",
                    }}
                    for wid in ids
                ]
            })
        raise AssertionError(f"unexpected ADO request: {req.method} {path}")

    _install_azuredevops_mock(monkeypatch, ado_handler)
    ado_ticket, _c, ado_rel, _t = AzureDevOpsProvider().get_ticket(
        _azuredevops_project(), token="t", ticket_id="5",
    )
    assert ado_ticket.parent_id == "#3"
    assert ado_ticket.milestone == "Proj\\Sprint 9"
    assert [r.kind for r in ado_rel if r.kind == "parent"] == ["parent"]

    # ---- Consistent format across all three: parent_id is "#N". ----
    assert gh_ticket.parent_id.startswith("#")
    assert gl_ticket.parent_id.startswith("#")
    assert ado_ticket.parent_id.startswith("#")


# ---------- ticket #152: FailingJob.annotations shape parity ----------------


def test_all_providers_populate_annotations_as_failure_annotation_list(monkeypatch):
    """Cross-provider parity check (ticket #152): every provider's
    `get_run(..., include_failure_excerpt=True)` on a failed run must
    populate each `FailingJob.annotations` as a `list[FailureAnnotation]`
    (possibly empty) — never raw provider dicts, on any of the three
    providers."""
    from lib_python_projects.providers.base import FailureAnnotation

    # --- GitHub: one failed job, a check-run annotation present. ---
    gh_run_id = 5001
    gh_job_id = 6001

    def gh_handler(req):
        path = req.url.path
        if path == f"/repos/Seretos/agent-project-issues/actions/runs/{gh_run_id}":
            return _resp({
                "id": gh_run_id, "name": "CI", "head_sha": "sha1",
                "head_branch": "main", "event": "push", "status": "completed",
                "conclusion": "failure",
                "html_url": f"https://github.com/Seretos/agent-project-issues/actions/runs/{gh_run_id}",
                "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:01:00Z",
                "run_attempt": 1,
            })
        if path == f"/repos/Seretos/agent-project-issues/actions/runs/{gh_run_id}/jobs":
            return _resp({
                "jobs": [{
                    "id": gh_job_id, "name": "build", "conclusion": "failure",
                    "html_url": "https://github.com/x/jobs/6001",
                    "check_run_url": "https://api.github.com/repos/Seretos/agent-project-issues/check-runs/9",
                    "steps": [{"name": "Run make", "conclusion": "failure", "number": 1}],
                }]
            })
        if path == "/repos/Seretos/agent-project-issues/check-runs/9/annotations":
            return _resp([{
                "path": "x.py", "start_line": 1, "annotation_level": "failure",
                "message": "boom",
            }])
        raise AssertionError(f"unexpected GH request: {req.url}")

    _install_github_mock(monkeypatch, gh_handler)
    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log",
        lambda token, url, *, max_bytes=256 * 1024: "some log text",
    )
    gh_run = GitHubProvider().get_run(
        _github_project(), token="t", run_id=str(gh_run_id),
    )
    assert gh_run.failure is not None
    for job in gh_run.failure.failing_jobs:
        assert isinstance(job.annotations, list)
        for ann in job.annotations:
            assert isinstance(ann, FailureAnnotation)
    assert len(gh_run.failure.failing_jobs) == 1
    assert len(gh_run.failure.failing_jobs[0].annotations) == 1

    # --- GitLab: one failed job, annotations always [] (deliberate). ---
    def gl_handler(req):
        url = str(req.url)
        if "/pipelines/200/jobs" in url:
            return _resp([
                {"id": 1, "name": "test", "status": "failed", "stage": "test", "web_url": "u"},
            ])
        if "/pipelines/200" in url and "/jobs" not in url:
            return _resp({
                "id": 200, "ref": "main", "sha": "abc", "source": "push",
                "status": "failed",
                "web_url": "https://gitlab.com/x/-/pipelines/200",
                "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:05:00Z",
            })
        if "/jobs/1/trace" in url:
            return httpx.Response(200, content=b"trace text", headers={"Content-Type": "text/plain"})
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, gl_handler)
    gl_run = GitLabProvider().get_run(
        _gitlab_project(), "t", "200", include_failure_excerpt=True,
    )
    assert gl_run.failure is not None
    for job in gl_run.failure.failing_jobs:
        assert isinstance(job.annotations, list)
        for ann in job.annotations:
            assert isinstance(ann, FailureAnnotation)
    assert len(gl_run.failure.failing_jobs) == 1
    assert gl_run.failure.failing_jobs[0].annotations == []

    # --- Azure DevOps: one failed timeline record with a structured issue. ---
    from lib_python_projects.providers.azuredevops import _cache_clear_all
    _cache_clear_all()

    def ado_handler(req):
        path = req.url.path
        if path.endswith("/_apis/build/builds/300"):
            return _resp({
                "id": 300, "status": "completed", "result": "failed",
                "definition": {"name": "CI"}, "sourceBranch": "refs/heads/main",
                "sourceVersion": "abc", "reason": "manual",
                "queueTime": "2024-01-01T00:00:00Z", "finishTime": "2024-01-01T00:05:00Z",
            })
        if path.endswith("/_apis/build/builds/300/timeline"):
            return _resp({
                "records": [{
                    "id": "r1", "type": "Job", "name": "Build", "result": "failed",
                    "issues": [{
                        "type": "error", "message": "compile error",
                        "data": {"sourcePath": "a.cs", "lineNumber": "1"},
                    }],
                }]
            })
        raise AssertionError(f"unexpected ADO request: {req.url}")

    _install_azuredevops_mock(monkeypatch, ado_handler)
    ado_run = AzureDevOpsProvider().get_run(
        _ado_project(), token="t", run_id="300", include_failure_excerpt=True,
    )
    assert ado_run.failure is not None
    for job in ado_run.failure.failing_jobs:
        assert isinstance(job.annotations, list)
        for ann in job.annotations:
            assert isinstance(ann, FailureAnnotation)
    assert len(ado_run.failure.failing_jobs) == 1
    assert len(ado_run.failure.failing_jobs[0].annotations) == 1


# ---------- ticket #153: per-project auto_labels cross-provider parity ------
#
# Detailed per-provider coverage (label-name resolution, role-based
# color/description, create/update paths) lives in test_github_labels.py,
# test_gitlab_issues.py, and test_azuredevops_tickets.py. These tests only
# assert the cross-provider *shape*: all three providers apply the same
# configured `auto_labels` names — both as the applied label/tag and as
# the body-marker prefix — instead of the literal 'ai-generated'/
# 'ai-modified' defaults, on create_ticket.


def _custom_auto_labels() -> "AutoLabels":
    from lib_python_projects import AutoLabels
    return AutoLabels(ai_generated="robot-made", ai_modified="robot-touched")


def test_github_create_ticket_honors_custom_auto_labels(monkeypatch):
    project = ProjectConfig(
        id="github-tests", provider="github", path="Seretos/agent-project-issues",
        auto_labels=_custom_auto_labels(),
    )
    captured: dict = {}

    def handler(req):
        path = req.url.path
        if req.method == "POST" and "/labels" in path:
            return _resp({"name": "robot-made", "color": "0e8a16"}, status_code=201)
        if req.method == "POST" and path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _resp({
                "number": 9, "title": "hi", "body": captured["body"]["body"],
                "state": "open", "user": {"login": "a"}, "assignees": [],
                "labels": [{"name": "robot-made"}],
                "html_url": "https://github.com/acme/backend/issues/9",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp({})

    _install_github_mock(monkeypatch, handler)
    GitHubProvider().create_ticket(
        project, "t", title="hi", body="B", labels=[], assignees=[],
    )
    assert "robot-made" in captured["body"]["labels"]
    assert "ai-generated" not in captured["body"]["labels"]
    assert captured["body"]["body"].startswith("#robot-made")


def test_gitlab_create_ticket_honors_custom_auto_labels(monkeypatch):
    project = ProjectConfig(
        id="gitlab-tests", provider="gitlab", path="Seredos/gitlab-tests",
        auto_labels=_custom_auto_labels(),
    )
    captured: dict = {}

    def handler(req):
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _resp([])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _resp({
                "iid": 6, "title": "hi", "description": captured["body"]["description"],
                "state": "opened", "author": {"username": "a"}, "assignees": [],
                "labels": ["robot-made"],
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/6",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        project, "t", title="hi", body="B", labels=[], assignees=[],
    )
    assert "robot-made" in captured["body"]["labels"]
    assert "ai-generated" not in captured["body"]["labels"]
    assert captured["body"]["description"].startswith("#robot-made")


def test_azuredevops_create_ticket_honors_custom_auto_labels(monkeypatch):
    from lib_python_projects.providers.azuredevops import _cache_clear_all
    _cache_clear_all()
    project = ProjectConfig(
        id="azure-tests", provider="azuredevops",
        path="seredos/azure-tests/azure-tests", token_env="AZURE_TOKEN",
        auto_labels=_custom_auto_labels(),
    )
    captured: dict = {}

    def handler(req):
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _resp({"value": [{"name": "Issue"}]})
        if "/_apis/wit/workitems/$Issue" in path:
            captured["patch"] = json.loads(req.content.decode("utf-8"))
            fields = {
                "System.Title": "hi", "System.Description": "<p>B</p>",
                "System.State": "To Do", "System.WorkItemType": "Issue",
                "System.Tags": "robot-made",
                "System.CreatedDate": "2024-01-01T00:00:00Z",
                "System.ChangedDate": "2024-01-01T00:00:00Z",
            }
            return _resp({"id": 7, "fields": fields})
        raise AssertionError(f"unexpected {path}")

    _install_azuredevops_mock(monkeypatch, handler)
    AzureDevOpsProvider().create_ticket(
        project, token="t", title="hi", body="B", labels=[], assignees=[],
    )
    patch = captured["patch"]
    desc_op = next(op for op in patch if op["path"] == "/fields/System.Description")
    tag_op = next(op for op in patch if op["path"] == "/fields/System.Tags")
    assert "#robot-made" in desc_op["value"]
    assert "#ai-generated" not in desc_op["value"]
    assert "robot-made" in tag_op["value"]
    assert "ai-generated" not in tag_op["value"]


# ---------- ticket #168: get_step_log cross-provider parity ------------------


def test_all_providers_expose_get_step_log():
    """All three providers must expose get_step_log as a callable.
    This is a static structural assertion."""
    from lib_python_projects.providers.github import GitHubProvider
    from lib_python_projects.providers.gitlab import GitLabProvider
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider

    for provider_cls in (GitHubProvider, GitLabProvider, AzureDevOpsProvider):
        assert callable(getattr(provider_cls, "get_step_log", None)), (
            f"{provider_cls.__name__} is missing callable 'get_step_log'"
        )


def test_failing_job_dataclass_exposes_job_id():
    """FailingJob must have a job_id field so callers can round-trip from
    get_run's failure excerpt into get_step_log. This is a static
    structural assertion."""
    from lib_python_projects.providers.base import FailingJob
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(FailingJob)}
    assert "job_id" in field_names, (
        "FailingJob.job_id field is missing — all three providers must "
        "populate it so callers can fetch a job's full log via "
        "Provider.get_step_log (ticket #168)."
    )
