"""Tests for the label-conflict guard introduced in ticket #51.

Covers:
- _validate_label_lists helper unit tests (edge cases)
- Regression: each provider call site raises ValueError before any HTTP I/O
  when labels_add and labels_remove contain the same label.
- Happy-path non-regression: disjoint labels_add/labels_remove still reach
  the HTTP layer without raising.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)
from lib_python_projects.providers.base import _validate_label_lists
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider


# ---------- fixtures / helpers -----------------------------------------------


def _github_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _gitlab_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="gitlab",
        path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
    )


def _azure_project() -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path="seredos/azure-tests/azure-tests",
        token_env="AZURE_TOKEN",
    )


def _json_response(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _install_github_mock(
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
            base_url=github_mod.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_mod, "_client", fake_client)
    return seen


def _install_gitlab_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "test-agent"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(
            base_url=gitlab_mod._base_url(project),
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)
    return seen


def _install_azure_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "test-agent"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen


@pytest.fixture(autouse=True)
def _clear_azure_caches() -> None:
    """ADO provider has module-level caches — wipe between tests."""
    _cache_clear_all()


# ---------- _validate_label_lists unit tests ---------------------------------


def test_validate_single_overlap_raises() -> None:
    """A single label present in both lists raises ValueError with name in message."""
    with pytest.raises(ValueError, match="bug"):
        _validate_label_lists(["bug"], ["bug"])


def test_validate_multiple_overlap_raises() -> None:
    """Multiple conflicting labels all appear in the error message."""
    with pytest.raises(ValueError) as exc:
        _validate_label_lists(["bug", "wontfix"], ["wontfix", "bug"])
    msg = str(exc.value)
    assert "bug" in msg
    assert "wontfix" in msg


def test_validate_partial_overlap_raises() -> None:
    """One shared label among otherwise disjoint sets still raises."""
    with pytest.raises(ValueError, match="shared"):
        _validate_label_lists(["shared", "only-add"], ["shared", "only-remove"])


def test_validate_add_only_no_raise() -> None:
    """labels_add with labels_remove=None does not raise."""
    _validate_label_lists(["x"], None)  # must not raise


def test_validate_remove_only_no_raise() -> None:
    """labels_remove with labels_add=None does not raise."""
    _validate_label_lists(None, ["x"])  # must not raise


def test_validate_both_none_no_raise() -> None:
    """Both None does not raise."""
    _validate_label_lists(None, None)  # must not raise


def test_validate_both_empty_no_raise() -> None:
    """Both empty lists do not raise."""
    _validate_label_lists([], [])  # must not raise


def test_validate_error_message_format() -> None:
    """Error message includes the prescribed prefix."""
    with pytest.raises(ValueError, match="labels_add and labels_remove overlap"):
        _validate_label_lists(["dup"], ["dup"])


# ---------- regression: GitHub update_ticket ---------------------------------


def test_github_update_ticket_label_conflict_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket raises ValueError for overlapping labels; no HTTP call made."""
    seen = _install_github_mock(monkeypatch, lambda r: _json_response({}))
    with pytest.raises(ValueError, match="bug"):
        GitHubProvider().update_ticket(
            _github_project(),
            token="t",
            ticket_id="1",
            labels_add=["bug"],
            labels_remove=["bug"],
        )
    assert seen == [], "no HTTP request should have been made"


# ---------- regression: GitHub update_pr -------------------------------------


def test_github_update_pr_label_conflict_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_pr raises ValueError for overlapping labels; no HTTP call made."""
    seen = _install_github_mock(monkeypatch, lambda r: _json_response({}))
    with pytest.raises(ValueError, match="bug"):
        GitHubProvider().update_pr(
            _github_project(),
            token="t",
            pr_id="1",
            labels_add=["bug"],
            labels_remove=["bug"],
        )
    assert seen == [], "no HTTP request should have been made"


# ---------- regression: GitLab update_ticket ---------------------------------


def test_gitlab_update_ticket_label_conflict_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket raises ValueError for overlapping labels; no HTTP call made."""
    seen = _install_gitlab_mock(monkeypatch, lambda r: _json_response({}))
    with pytest.raises(ValueError, match="bug"):
        GitLabProvider().update_ticket(
            _gitlab_project(),
            token="t",
            ticket_id="1",
            labels_add=["bug"],
            labels_remove=["bug"],
        )
    assert seen == [], "no HTTP request should have been made"


# ---------- regression: GitLab update_pr (update_mr) -------------------------


def test_gitlab_update_mr_label_conflict_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_pr raises ValueError for overlapping labels; no HTTP call made."""
    seen = _install_gitlab_mock(monkeypatch, lambda r: _json_response({}))
    with pytest.raises(ValueError, match="bug"):
        GitLabProvider().update_pr(
            _gitlab_project(),
            token="t",
            pr_id="1",
            labels_add=["bug"],
            labels_remove=["bug"],
        )
    assert seen == [], "no HTTP request should have been made"


# ---------- regression: AzureDevOps update_ticket ----------------------------


def test_azuredevops_update_ticket_label_conflict_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket raises ValueError for overlapping labels; no HTTP call made."""
    seen = _install_azure_mock(monkeypatch, lambda r: _json_response({}))
    with pytest.raises(ValueError, match="bug"):
        AzureDevOpsProvider().update_ticket(
            _azure_project(),
            token="t",
            ticket_id="5",
            labels_add=["bug"],
            labels_remove=["bug"],
        )
    assert seen == [], "no HTTP request should have been made"


# ---------- regression: AzureDevOps update_pr --------------------------------


def test_azuredevops_update_pr_label_conflict_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_pr raises ValueError for overlapping labels; no HTTP call made.

    The guard must fire before _resolve_repository_id, which would otherwise
    issue a GET /_apis/git/repositories on a cold cache.
    """
    seen = _install_azure_mock(monkeypatch, lambda r: _json_response({}))
    with pytest.raises(ValueError, match="bug"):
        AzureDevOpsProvider().update_pr(
            _azure_project(),
            token="t",
            pr_id="7",
            labels_add=["bug"],
            labels_remove=["bug"],
        )
    assert seen == [], "no HTTP request should have been made"


# ---------- happy-path non-regression: disjoint labels still reach HTTP -------


def _github_issue_payload(number: int) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [{"name": "ai-generated"}],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }


def _github_pr_payload(number: int) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "mergeable": None,
        "mergeable_state": "clean",
        "merge_commit_sha": None,
        "auto_merge": None,
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [{"name": "ai-generated"}],
        "requested_reviewers": [],
        "head": {
            "ref": "feat/branch",
            "sha": "abc123",
            "repo": {"full_name": "acme/backend"},
        },
        "base": {"ref": "main", "sha": "def456"},
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }


def _gitlab_issue_payload(iid: int) -> dict:
    return {
        "iid": iid,
        "title": f"Issue {iid}",
        "description": "body",
        "state": "opened",
        "author": {"username": "alice"},
        "assignees": [],
        "labels": ["ai-generated"],
        "web_url": f"https://gitlab.com/acme/backend/-/issues/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }


def _gitlab_mr_payload(iid: int) -> dict:
    return {
        "iid": iid,
        "title": f"MR {iid}",
        "description": "body",
        "state": "opened",
        "draft": False,
        "author": {"username": "alice"},
        "assignees": [],
        "reviewers": [],
        "labels": ["ai-generated"],
        "source_branch": "feat/x",
        "target_branch": "main",
        "sha": "abc123",
        "web_url": f"https://gitlab.com/acme/backend/-/merge_requests/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "detailed_merge_status": "mergeable",
    }


def _azure_work_item_payload(work_item_id: int) -> dict:
    return {
        "id": work_item_id,
        "fields": {
            "System.Title": f"Item {work_item_id}",
            "System.Description": "<p>Body</p>",
            "System.State": "To Do",
            "System.WorkItemType": "Issue",
            "System.Tags": "ai-generated",
            "System.CreatedDate": "2026-05-18T10:00:00Z",
            "System.ChangedDate": "2026-05-18T11:00:00Z",
        },
    }


def test_github_update_ticket_disjoint_labels_reaches_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disjoint labels_add / labels_remove do not raise; HTTP layer is reached."""
    seen = _install_github_mock(
        monkeypatch,
        lambda r: _json_response(
            _github_issue_payload(1) if r.method == "GET" else _github_issue_payload(1)
        ),
    )
    # Should not raise; the guard must allow disjoint lists through.
    GitHubProvider().update_ticket(
        _github_project(),
        token="t",
        ticket_id="1",
        labels_add=["enhancement"],
        labels_remove=["bug"],
    )
    assert len(seen) > 0, "HTTP requests should have been made for disjoint labels"


def test_github_update_pr_disjoint_labels_reaches_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disjoint labels_add / labels_remove do not raise; HTTP layer is reached."""

    def handler(req: httpx.Request) -> httpx.Response:
        # PUT /issues/{n}/labels returns a list of label objects, not a PR.
        if req.method == "PUT" and "/labels" in req.url.path:
            return _json_response([{"id": 1, "name": "ai-generated", "color": "ededed", "description": ""}])
        return _json_response(_github_pr_payload(1))

    seen = _install_github_mock(monkeypatch, handler)
    GitHubProvider().update_pr(
        _github_project(),
        token="t",
        pr_id="1",
        labels_add=["enhancement"],
        labels_remove=["bug"],
    )
    assert len(seen) > 0, "HTTP requests should have been made for disjoint labels"


def test_gitlab_update_ticket_disjoint_labels_reaches_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disjoint labels_add / labels_remove do not raise; HTTP layer is reached."""
    seen = _install_gitlab_mock(
        monkeypatch,
        lambda r: _json_response(
            _gitlab_issue_payload(1) if r.method == "GET" else _gitlab_issue_payload(1)
        ),
    )
    GitLabProvider().update_ticket(
        _gitlab_project(),
        token="t",
        ticket_id="1",
        labels_add=["enhancement"],
        labels_remove=["bug"],
    )
    assert len(seen) > 0, "HTTP requests should have been made for disjoint labels"


def test_gitlab_update_mr_disjoint_labels_reaches_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disjoint labels_add / labels_remove do not raise; HTTP layer is reached."""
    seen = _install_gitlab_mock(
        monkeypatch,
        lambda r: _json_response(
            _gitlab_mr_payload(1) if r.method == "GET" else _gitlab_mr_payload(1)
        ),
    )
    GitLabProvider().update_pr(
        _gitlab_project(),
        token="t",
        pr_id="1",
        labels_add=["enhancement"],
        labels_remove=["bug"],
    )
    assert len(seen) > 0, "HTTP requests should have been made for disjoint labels"


def test_azuredevops_update_ticket_disjoint_labels_reaches_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disjoint labels_add / labels_remove do not raise; HTTP layer is reached."""
    seen = _install_azure_mock(
        monkeypatch,
        lambda r: _json_response(
            _azure_work_item_payload(5) if r.method == "GET"
            else _azure_work_item_payload(5)
        ),
    )
    AzureDevOpsProvider().update_ticket(
        _azure_project(),
        token="t",
        ticket_id="5",
        labels_add=["enhancement"],
        labels_remove=["bug"],
    )
    assert len(seen) > 0, "HTTP requests should have been made for disjoint labels"


def test_azuredevops_update_pr_disjoint_labels_reaches_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disjoint labels_add / labels_remove do not raise; HTTP layer is reached.

    The PR payload carries the ai-generated label so the ai-modified
    best-effort path is skipped and the handler only needs to serve:
    - GET /_apis/git/repositories  (repo-id resolution)
    - GET /pullrequests/7           (current state)
    - POST /pullrequests/7/labels   (labels_add=["enhancement"])
    - DELETE /pullrequests/7/labels/bug  (labels_remove=["bug"])
    """
    repo_id = "da0d7da0-6a8c-4958-aad3-be17cbf806eb"

    def _azure_pr_payload(pr_id: int) -> dict:
        return {
            "pullRequestId": pr_id,
            "title": f"PR {pr_id}",
            "description": "<p>impl</p>",
            "status": "active",
            "isDraft": False,
            "createdBy": {"displayName": "Alice"},
            "reviewers": [],
            "labels": [{"name": "ai-generated", "active": True}],
            "sourceRefName": "refs/heads/feat/x",
            "targetRefName": "refs/heads/main",
            "lastMergeSourceCommit": {"commitId": "abc"},
            "lastMergeTargetCommit": {"commitId": "def"},
            "creationDate": "2026-05-18T10:00:00Z",
            "repository": {"name": "azure-tests"},
        }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/git/repositories"):
            return _json_response({
                "value": [{"id": repo_id, "name": "azure-tests", "defaultBranch": "refs/heads/main"}]
            })
        if req.method == "GET" and "pullrequests/7" in path and path.endswith("/labels"):
            # _fetch_pr_labels GET — returns a labels list
            return _json_response({"value": [{"name": "ai-generated", "active": True}]})
        if req.method == "GET" and "pullrequests/7" in path:
            return _json_response(_azure_pr_payload(7))
        if req.method == "GET" and "threads" in path:
            return _json_response({"value": []})
        if req.method == "POST" and path.endswith("/labels"):
            return _json_response({"name": "enhancement", "active": True})
        if req.method == "DELETE" and "/labels/" in path:
            return httpx.Response(status_code=200, content=b"", headers={})
        raise AssertionError(f"unexpected {req.method} {path}")

    seen = _install_azure_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_pr(
        _azure_project(),
        token="t",
        pr_id="7",
        labels_add=["enhancement"],
        labels_remove=["bug"],
    )
    assert len(seen) > 0, "HTTP requests should have been made for disjoint labels"
