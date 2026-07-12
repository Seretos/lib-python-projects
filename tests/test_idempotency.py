"""Tests for idempotency keys on create_ticket / create_pr (ticket #150).

Covers:
- the `_idempotency` store module directly (TTL default/env-var/invalid
  fallback, namespacing, conflict detection) — fast, precise unit coverage
- per-provider regression coverage: a retried `create_ticket`/`create_pr`
  call with the same `idempotency_key` fires the create POST only once
  and returns `idempotent_replay=True` on the second call
- cross-cutting behavioural contracts (backward-compat no-op, non-core
  args ignored, conflict raises + no second POST, failure isn't cached)
  exercised through the GitHub provider as the representative surface
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import _idempotency
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)
from lib_python_projects.providers.base import IdempotencyConflict
from lib_python_projects.providers.github import GitHubError, GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider


@pytest.fixture(autouse=True)
def _fresh_idempotency_store() -> None:
    """Drain the module-level idempotency store before every test."""
    _idempotency.clear_idempotency_cache()


@pytest.fixture(autouse=True)
def _clear_ado_caches() -> None:
    _cache_clear_all()


# ---------- helpers: projects -------------------------------------------------


def _github_project(id: str = "acme") -> ProjectConfig:
    return ProjectConfig(
        id=id, provider="github", path="acme/backend", token_env="GITHUB_TOKEN_ACME",
    )


def _gitlab_project(id: str = "acme-gl") -> ProjectConfig:
    return ProjectConfig(
        id=id, provider="gitlab", path="acme/backend", token_env="GITLAB_TOKEN_ACME",
    )


def _ado_project(id: str = "azure-tests", default_type: str = "Issue") -> ProjectConfig:
    return ProjectConfig(
        id=id,
        provider="azuredevops",
        path="seredos/azure-tests/azure-tests",
        token_env="AZURE_TOKEN",
        default_work_item_type=default_type,
    )


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


# ---------- helpers: mock transport installers ---------------------------------


def _install_github_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def wrapped(req: httpx.Request) -> httpx.Response:
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(base_url=github_mod.API_BASE, headers=headers, transport=transport)

    monkeypatch.setattr(github_mod, "_client", fake_client)


def _install_gitlab_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def wrapped(req: httpx.Request) -> httpx.Response:
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(base_url=gitlab_mod._base_url(project), headers=headers, transport=transport)

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)


def _install_ado_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def wrapped(req: httpx.Request) -> httpx.Response:
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)


# ---------- helpers: response payloads ------------------------------------------


def _gh_issue_payload(number: int = 1) -> dict:
    return {
        "number": number, "state": "open", "title": "T", "body": "B",
        "user": {"login": "bot"}, "assignees": [], "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
    }


def _gh_pr_payload(number: int = 1) -> dict:
    return {
        "number": number, "state": "open", "title": "T", "body": "B",
        "user": {"login": "bot"}, "assignees": [], "requested_reviewers": [],
        "labels": [],
        "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "acme/backend"}},
        "base": {"ref": "main", "sha": "def"},
        "draft": False, "merged": False, "mergeable": None,
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
    }


def _gl_issue_payload(iid: int = 1) -> dict:
    return {
        "iid": iid, "title": "T", "description": "B", "state": "opened",
        "author": {"username": "a"}, "assignees": [], "labels": [],
        "web_url": f"https://gitlab.com/acme/backend/-/issues/{iid}",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
    }


def _gl_mr_payload(iid: int = 1) -> dict:
    return {
        "iid": iid, "title": "T", "description": "B", "state": "opened",
        "draft": False, "author": {"username": "a"}, "assignees": [],
        "reviewers": [], "labels": [],
        "source_branch": "feat/x", "target_branch": "main", "sha": "abc123",
        "web_url": f"https://gitlab.com/acme/backend/-/merge_requests/{iid}",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "detailed_merge_status": "mergeable",
    }


def _ado_work_item_payload(work_item_id: int = 1) -> dict:
    return {
        "id": work_item_id,
        "fields": {
            "System.Title": "T", "System.Description": "<p>B</p>",
            "System.State": "To Do", "System.WorkItemType": "Issue",
            "System.Tags": "",
            "System.CreatedDate": "2026-05-18T10:00:00Z",
            "System.ChangedDate": "2026-05-18T11:00:00Z",
        },
    }


def _ado_pr_payload(pr_id: int = 1) -> dict:
    return {
        "pullRequestId": pr_id, "title": "T", "description": "<p>B</p>",
        "status": "active", "isDraft": False, "createdBy": {"displayName": "Alice"},
        "reviewers": [], "labels": [],
        "sourceRefName": "refs/heads/feat/x", "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": "abc"},
        "lastMergeTargetCommit": {"commitId": "def"},
        "creationDate": "2026-05-18T10:00:00Z",
        "repository": {"name": "azure-tests"},
    }


REPO_ID = "da0d7da0-6a8c-4958-aad3-be17cbf806eb"


def _ado_repos_response() -> httpx.Response:
    return _json({"value": [{"id": REPO_ID, "name": "azure-tests", "defaultBranch": "refs/heads/main"}]})


def _gh_pr_labels_shard(req: httpx.Request) -> httpx.Response | None:
    """Shared shard for GitHub create_pr's two distinct `/labels`-ish
    calls: the ai-generated ensure-label GET/POST (single object) vs.
    the "apply labels to the PR" POST on `/issues/{n}/labels` (a list)."""
    path = req.url.path
    if path.endswith("/labels") and "/issues/" in path:
        return _json([{"name": "ai-generated"}])
    if "/labels" in path:
        return _json({"name": "ai-generated", "color": "0075ca"})
    return None


def _ado_pr_shards(req: httpx.Request, pr_id: int = 3) -> httpx.Response | None:
    """Shared shard for ADO create_pr's repo-resolve + label + refetch calls."""
    path = req.url.path
    if path.endswith("/_apis/git/repositories"):
        return _ado_repos_response()
    if req.method == "POST" and path.endswith(f"/pullrequests/{pr_id}/labels"):
        return _json({})
    if req.method == "GET" and path.endswith(f"/pullrequests/{pr_id}/labels"):
        return _json({"value": []})
    if req.method == "GET" and path.endswith(f"/pullrequests/{pr_id}"):
        return _json(_ado_pr_payload(pr_id))
    if req.method == "GET" and path.endswith(f"/pullrequests/{pr_id}/threads"):
        return _json({"value": []})
    return None


# =================================================================================
# Regression: per-provider dup-avoidance for create_ticket / create_pr
# =================================================================================


def test_github_create_ticket_idempotent_replay_avoids_duplicate_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(9), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    t1 = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    t2 = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    assert post_count == 1
    assert t1.idempotent_replay is False
    assert t2.idempotent_replay is True
    assert t2.id == t1.id == "9"


def test_github_create_pr_idempotent_replay_avoids_duplicate_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        shard = _gh_pr_labels_shard(req)
        if shard is not None:
            return shard
        if path.endswith("/pulls") and req.method == "POST":
            post_count += 1
            return _json(_gh_pr_payload(11), status_code=201)
        return _json([])

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    pr1 = GitHubProvider().create_pr(
        p, token="t", title="hi", body="b", head="feature", base="main",
        idempotency_key="key-pr",
    )
    pr2 = GitHubProvider().create_pr(
        p, token="t", title="hi", body="b", head="feature", base="main",
        idempotency_key="key-pr",
    )
    assert post_count == 1
    assert pr1.idempotent_replay is False
    assert pr2.idempotent_replay is True
    assert pr2.number == pr1.number == 11


def test_gitlab_create_ticket_idempotent_replay_avoids_duplicate_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if req.method == "POST" and req.url.path.endswith("/issues"):
            post_count += 1
            return _json(_gl_issue_payload(6))
        return _json([])

    _install_gitlab_mock(monkeypatch, handler)
    p = _gitlab_project()
    t1 = GitLabProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    t2 = GitLabProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    assert post_count == 1
    assert t1.idempotent_replay is False
    assert t2.idempotent_replay is True
    assert t2.id == t1.id == "6"


def test_gitlab_create_pr_idempotent_replay_avoids_duplicate_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if req.method == "POST" and req.url.path.endswith("/merge_requests"):
            post_count += 1
            return _json(_gl_mr_payload(4))
        return _json([])

    _install_gitlab_mock(monkeypatch, handler)
    p = _gitlab_project()
    pr1 = GitLabProvider().create_pr(
        p, token="t", title="hi", body="b", head="feat/x", base="main",
        idempotency_key="key-pr",
    )
    pr2 = GitLabProvider().create_pr(
        p, token="t", title="hi", body="b", head="feat/x", base="main",
        idempotency_key="key-pr",
    )
    assert post_count == 1
    assert pr1.idempotent_replay is False
    assert pr2.idempotent_replay is True
    assert pr2.id == pr1.id == "4"


def test_azuredevops_create_ticket_idempotent_replay_avoids_duplicate_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            post_count += 1
            return _json(_ado_work_item_payload(7))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_ado_mock(monkeypatch, handler)
    p = _ado_project()
    t1 = AzureDevOpsProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    t2 = AzureDevOpsProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    assert post_count == 1
    assert t1.idempotent_replay is False
    assert t2.idempotent_replay is True
    assert t2.id == t1.id == "7"


def test_azuredevops_create_pr_idempotent_replay_avoids_duplicate_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        shard = _ado_pr_shards(req, pr_id=3)
        if shard is not None:
            return shard
        if req.method == "POST" and path.endswith("/pullrequests"):
            post_count += 1
            return _json(_ado_pr_payload(3))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_ado_mock(monkeypatch, handler)
    p = _ado_project()
    pr1 = AzureDevOpsProvider().create_pr(
        p, token="t", title="hi", body="b", head="feat/x", base="main",
        idempotency_key="key-pr",
    )
    pr2 = AzureDevOpsProvider().create_pr(
        p, token="t", title="hi", body="b", head="feat/x", base="main",
        idempotency_key="key-pr",
    )
    assert post_count == 1
    assert pr1.idempotent_replay is False
    assert pr2.idempotent_replay is True
    assert pr2.id == pr1.id == "3"


# =================================================================================
# Backward-compat: omitted / None / "" key never dedupes
# =================================================================================


def test_github_create_ticket_no_key_creates_twice(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(post_count), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    for key in (None, ""):
        ticket = GitHubProvider().create_ticket(
            p, token="t", title="hi", body="b", labels=[], assignees=[],
            idempotency_key=key,
        )
        assert ticket.idempotent_replay is False
    assert post_count == 2


def test_github_create_ticket_omitted_key_creates_twice(monkeypatch):
    """Callers that never pass `idempotency_key` at all keep the pre-#150
    default behaviour byte-for-byte — two calls create two tickets."""
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(post_count), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    GitHubProvider().create_ticket(p, token="t", title="hi", body="b", labels=[], assignees=[])
    GitHubProvider().create_ticket(p, token="t", title="hi", body="b", labels=[], assignees=[])
    assert post_count == 2


# =================================================================================
# Non-core args are ignored for conflict detection
# =================================================================================


def test_github_create_ticket_non_core_args_ignored(monkeypatch):
    """Same key + same title/body but different labels/status -> replay,
    not a conflict; the create POST fires exactly once."""
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(9), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    t1 = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    t2 = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=["bug"], assignees=["octocat"],
        custom_fields=None, idempotency_key="key-1",
    )
    assert post_count == 1
    assert t2.idempotent_replay is True
    assert t2.id == t1.id


def test_github_create_pr_non_core_args_ignored(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        shard = _gh_pr_labels_shard(req)
        if shard is not None:
            return shard
        if path.endswith("/pulls") and req.method == "POST":
            post_count += 1
            return _json(_gh_pr_payload(11), status_code=201)
        return _json([])

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    pr1 = GitHubProvider().create_pr(
        p, token="t", title="hi", body="b", head="feature", base="main",
        idempotency_key="key-pr",
    )
    pr2 = GitHubProvider().create_pr(
        p, token="t", title="hi", body="different body",
        head="feature", base="main", draft=True, labels=["x"],
        idempotency_key="key-pr",
    )
    assert post_count == 1
    assert pr2.idempotent_replay is True
    assert pr2.number == pr1.number


# =================================================================================
# Conflict: same key, different core args -> IdempotencyConflict, no 2nd POST
# =================================================================================


def test_github_create_ticket_conflict_raises_and_no_second_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(9), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    with pytest.raises(IdempotencyConflict) as excinfo:
        GitHubProvider().create_ticket(
            p, token="t", title="different title", body="b", labels=[], assignees=[],
            idempotency_key="key-1",
        )
    assert post_count == 1
    assert excinfo.value.key == "key-1"
    assert excinfo.value.status == 409
    assert excinfo.value.stored["title"] == "hi"
    assert excinfo.value.received["title"] == "different title"


def test_github_create_pr_conflict_raises_and_no_second_post(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        shard = _gh_pr_labels_shard(req)
        if shard is not None:
            return shard
        if path.endswith("/pulls") and req.method == "POST":
            post_count += 1
            return _json(_gh_pr_payload(11), status_code=201)
        return _json([])

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    GitHubProvider().create_pr(
        p, token="t", title="hi", body="b", head="feature", base="main",
        idempotency_key="key-pr",
    )
    with pytest.raises(IdempotencyConflict) as excinfo:
        GitHubProvider().create_pr(
            p, token="t", title="hi", body="b", head="other-branch", base="main",
            idempotency_key="key-pr",
        )
    assert post_count == 1
    assert excinfo.value.received["head"] == "other-branch"


def test_azuredevops_create_ticket_conflict_raises(monkeypatch):
    """Verifies the `*,`-only ADO signature (status/custom_fields stay
    positional) still applies conflict semantics correctly."""
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            post_count += 1
            return _json(_ado_work_item_payload(7))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_ado_mock(monkeypatch, handler)
    p = _ado_project()
    AzureDevOpsProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    with pytest.raises(IdempotencyConflict):
        AzureDevOpsProvider().create_ticket(
            p, token="t", title="hi", body="different body", labels=[], assignees=[],
            idempotency_key="key-1",
        )
    assert post_count == 1


# =================================================================================
# TTL: default replay within window, env override, invalid values, expiry
# =================================================================================


def test_ttl_seconds_default_when_unset(monkeypatch):
    monkeypatch.delenv("PROJECT_ISSUES_IDEMPOTENCY_TTL_SECONDS", raising=False)
    assert _idempotency._ttl_seconds() == 86400


@pytest.mark.parametrize("bad_value", ["abc", "0", "-5", ""])
def test_ttl_seconds_invalid_values_fall_back_to_default(monkeypatch, bad_value):
    monkeypatch.setenv("PROJECT_ISSUES_IDEMPOTENCY_TTL_SECONDS", bad_value)
    assert _idempotency._ttl_seconds() == 86400


def test_ttl_seconds_positive_env_value_used(monkeypatch):
    monkeypatch.setenv("PROJECT_ISSUES_IDEMPOTENCY_TTL_SECONDS", "120")
    assert _idempotency._ttl_seconds() == 120


def test_ttl_default_replay_within_window(monkeypatch):
    """A replay well inside the 24h default window still succeeds."""
    monkeypatch.delenv("PROJECT_ISSUES_IDEMPOTENCY_TTL_SECONDS", raising=False)
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(9), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    # Simulate 1 hour passing — well within the 24h default TTL.
    real_monotonic = _idempotency.time.monotonic()
    monkeypatch.setattr(
        _idempotency.time, "monotonic", lambda: real_monotonic + 3600,
    )
    t2 = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    assert post_count == 1
    assert t2.idempotent_replay is True


def test_ttl_expired_entry_creates_fresh(monkeypatch):
    """A retry past the configured TTL window must re-create rather than
    replay — the entry is lazily evicted."""
    monkeypatch.setenv("PROJECT_ISSUES_IDEMPOTENCY_TTL_SECONDS", "10")
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(post_count), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    t1 = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    # Simulate 11 seconds passing — past the 10s TTL.
    real_monotonic = _idempotency.time.monotonic()
    monkeypatch.setattr(
        _idempotency.time, "monotonic", lambda: real_monotonic + 11,
    )
    t2 = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    assert post_count == 2
    assert t2.idempotent_replay is False
    assert t2.id != t1.id


# =================================================================================
# Namespacing: same key across different project.id / provider never collides
# =================================================================================


def test_namespacing_same_key_different_project_id_no_collision(monkeypatch):
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            post_count += 1
            return _json(_gh_issue_payload(post_count), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    t1 = GitHubProvider().create_ticket(
        _github_project(id="project-a"), token="t", title="hi", body="b",
        labels=[], assignees=[], idempotency_key="shared-key",
    )
    t2 = GitHubProvider().create_ticket(
        _github_project(id="project-b"), token="t", title="hi", body="b",
        labels=[], assignees=[], idempotency_key="shared-key",
    )
    assert post_count == 2
    assert t1.idempotent_replay is False
    assert t2.idempotent_replay is False


def test_namespacing_same_key_different_provider_no_collision():
    """Direct store-level check: `(provider, project_id, key)` is the
    namespace tuple, so the same key on two different providers never
    collides even when project.id happens to match."""
    from lib_python_projects.providers.base import Ticket

    ticket = Ticket(
        id="1", title="T", body="B", status="open", author="a",
        assignees=[], labels=[], url="u", created_at="", updated_at="",
    )
    _idempotency.record(("github", "same-id"), "k", {"title": "T", "body": "B"}, ticket)
    # Different provider, same project id, same key -> no hit.
    assert _idempotency.lookup(("gitlab", "same-id"), "k", {"title": "T", "body": "B"}) is None
    # Same provider + id + key -> hit.
    replay = _idempotency.lookup(("github", "same-id"), "k", {"title": "T", "body": "B"})
    assert replay is not None
    assert replay.idempotent_replay is True


# =================================================================================
# Failure is not cached: a failed first attempt lets the retry actually create
# =================================================================================


def test_failure_not_cached_retry_actually_creates(monkeypatch):
    attempt = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempt
        if "/labels" in req.url.path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if req.url.path.endswith("/issues") and req.method == "POST":
            attempt += 1
            if attempt == 1:
                return _json({"message": "Internal Server Error"}, status_code=500)
            return _json(_gh_issue_payload(9), status_code=201)
        return _json({})

    _install_github_mock(monkeypatch, handler)
    p = _github_project()
    with pytest.raises(GitHubError):
        GitHubProvider().create_ticket(
            p, token="t", title="hi", body="b", labels=[], assignees=[],
            idempotency_key="key-1",
        )
    # Retry with the same key must genuinely re-create (the failed
    # attempt left the store untouched).
    ticket = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        idempotency_key="key-1",
    )
    assert attempt == 2
    assert ticket.idempotent_replay is False
    assert ticket.id == "9"


# =================================================================================
# Failure in a FOLLOW-UP step (after the core resource already exists) is
# also not cached — the plan's most fracture-prone design decision: record()
# must run only after a fully successful create *including* any follow-up
# PATCH/label/board steps, not right after the initial POST.
# =================================================================================


def test_github_create_ticket_custom_fields_followup_failure_not_cached_retry_recreates(
    monkeypatch,
):
    """A `custom_fields` Projects-v2 board-write failure — which happens
    strictly *after* the initial issue POST already succeeded — must
    propagate as `PartialTicketCreateError` without caching a partial
    result. The initial POST must fire exactly once for the failed
    attempt, and a same-key retry must genuinely re-POST (not replay)."""
    from lib_python_projects import Board, GithubProjectsV2Binding
    from lib_python_projects.providers.github import PartialTicketCreateError

    board = Board(
        columns=["Todo", "Done"],
        binding=GithubProjectsV2Binding(
            kind="github-projects-v2", owner="acme-org", project_number=7,
            status_field="Status", map=None,
        ),
    )
    p = ProjectConfig(
        id="acme", provider="github", path="acme/backend",
        token_env="GITHUB_TOKEN_ACME", board=board,
    )
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            post_count += 1
            number = 8 + post_count
            payload = _gh_issue_payload(number)
            payload["node_id"] = f"issue-node-{number}"
            return _json(payload, status_code=201)
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path == "/graphql":
            body = json.loads(req.content.decode("utf-8"))
            query = body["query"]
            # The very first board-write step (project-id resolve) fails
            # on the first create attempt only; the retry sees a fully
            # working board-write path.
            if "projectV2(number:$number){id}" in query:
                if post_count == 1:
                    return _json({
                        "data": {"organization": None},
                        "errors": [{"message": "could not resolve project"}],
                    })
                return _json(
                    {"data": {"organization": {"projectV2": {"id": "proj-node-id"}}}}
                )
            if "addProjectV2ItemById" in query:
                return _json(
                    {"data": {"addProjectV2ItemById": {"item": {"id": "item-1"}}}}
                )
            if "ProjectV2FieldCommon" in query:
                return _json({"data": {"organization": {"projectV2": {"field": {
                    "id": "field-status", "name": "Status",
                    "options": [{"id": "opt-done", "name": "Done"}],
                }}}}})
            if "updateProjectV2ItemFieldValue" in query:
                return _json({
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "item-1"},
                        }
                    }
                })
        return _json({})

    _install_github_mock(monkeypatch, handler)
    with pytest.raises(PartialTicketCreateError):
        GitHubProvider().create_ticket(
            p, token="t", title="hi", body="b", labels=[], assignees=[],
            custom_fields={"Status": "Done"}, idempotency_key="key-1",
        )
    # The initial issue POST fired exactly once for the failed attempt —
    # no duplicate creation attempt within that call.
    assert post_count == 1

    # A retry with the same idempotency_key must genuinely re-create
    # (the failed attempt must not have populated the idempotency store).
    ticket = GitHubProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        custom_fields={"Status": "Done"}, idempotency_key="key-1",
    )
    assert post_count == 2
    assert ticket.idempotent_replay is False
    assert ticket.id == "10"


def test_azuredevops_create_ticket_status_transition_followup_failure_not_cached_retry_recreates(
    monkeypatch,
):
    """A status-transition (follow-up PATCH) failure — which happens
    strictly *after* the initial work-item POST already succeeded — must
    propagate as `AzureDevOpsError` without caching a partial result. The
    initial POST must fire exactly once for the failed attempt, and a
    same-key retry must genuinely re-POST (not replay)."""
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes/Issue/states"):
            return _json({"value": [
                {"name": "To Do", "category": "Proposed"},
                {"name": "Doing", "category": "InProgress"},
                {"name": "Done", "category": "Completed"},
            ]})
        if "/_apis/wit/workitems/$" in path and req.method == "POST":
            post_count += 1
            wid = 100 + post_count
            payload = _ado_work_item_payload(wid)
            payload["fields"]["System.State"] = "To Do"
            return _json(payload)
        wid = 100 + post_count
        if req.method == "GET" and path.endswith(f"/_apis/wit/workitems/{wid}"):
            payload = _ado_work_item_payload(wid)
            payload["fields"]["System.State"] = "To Do"
            return _json(payload)
        if req.method == "PATCH" and path.endswith(f"/_apis/wit/workitems/{wid}"):
            # The transition fails on the first create attempt only; the
            # retry's transition succeeds.
            if post_count == 1:
                return _json(
                    {"message": "transition not allowed: To Do -> Done"},
                    status_code=400,
                )
            payload = _ado_work_item_payload(wid)
            payload["fields"]["System.State"] = "Done"
            return _json(payload)
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_ado_mock(monkeypatch, handler)
    p = _ado_project()
    with pytest.raises(azure_mod.AzureDevOpsError):
        AzureDevOpsProvider().create_ticket(
            p, token="t", title="hi", body="b", labels=[], assignees=[],
            status="Done", idempotency_key="key-1",
        )
    # The initial work-item POST fired exactly once for the failed attempt.
    assert post_count == 1

    # A retry with the same idempotency_key must genuinely re-create.
    ticket = AzureDevOpsProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        status="Done", idempotency_key="key-1",
    )
    assert post_count == 2
    assert ticket.idempotent_replay is False
    assert ticket.status == "Done"


def test_gitlab_create_ticket_close_followup_failure_not_cached_retry_recreates(
    monkeypatch,
):
    """A `status="closed"` follow-up close PUT failure — which happens
    strictly *after* the initial issue POST already succeeded — must
    propagate without caching a partial result. The initial POST must
    fire exactly once for the failed attempt, and a same-key retry must
    genuinely re-POST (not replay)."""
    post_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = req.url.path
        if req.method == "POST" and path.endswith("/issues"):
            post_count += 1
            return _json(_gl_issue_payload(post_count))
        if req.method == "PUT" and "/issues/" in path:
            # The follow-up close fails on the first create attempt only;
            # the retry's close succeeds.
            if post_count == 1:
                return _json({"message": "close failed"}, status_code=500)
            payload = _gl_issue_payload(post_count)
            payload["state"] = "closed"
            return _json(payload)
        return _json([])

    _install_gitlab_mock(monkeypatch, handler)
    p = _gitlab_project()
    with pytest.raises(gitlab_mod.GitLabError):
        GitLabProvider().create_ticket(
            p, token="t", title="hi", body="b", labels=[], assignees=[],
            status="closed", idempotency_key="key-1",
        )
    # The initial issue POST fired exactly once for the failed attempt.
    assert post_count == 1

    # A retry with the same idempotency_key must genuinely re-create.
    ticket = GitLabProvider().create_ticket(
        p, token="t", title="hi", body="b", labels=[], assignees=[],
        status="closed", idempotency_key="key-1",
    )
    assert post_count == 2
    assert ticket.idempotent_replay is False
    assert ticket.status == "closed"


# =================================================================================
# IdempotencyConflict exception shape
# =================================================================================


def test_idempotency_conflict_is_provider_error_with_409():
    from lib_python_projects.providers.base import ProviderError

    exc = IdempotencyConflict("k", {"title": "a"}, {"title": "b"})
    assert isinstance(exc, ProviderError)
    assert exc.status == 409
    assert exc.key == "k"
    assert exc.stored == {"title": "a"}
    assert exc.received == {"title": "b"}
