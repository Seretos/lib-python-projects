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
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider, _basic_auth_header


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
