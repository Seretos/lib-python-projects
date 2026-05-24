"""Tests for GitHub provider error-contract fixes (tickets #17 and #28).

Covers:
- update_ticket 404 → "ticket '<project>#<id>' not found"
- add_comment 404 → "ticket '<project>#<id>' not found"
- update_comment 404 → "comment '<project>#<id>' not found"
- add_pr_review_comment 422 → named parameter message
- create_pr reviewer 422 → PR still returned with warning (ticket #28)
- create_pr reviewer 500 → GitHubError raised
- create_pr primary 422 → GitHubError raised
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers.github import GitHubError, GitHubProvider


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
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


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


# ---------- Issue #17 defect 1: resource-named errors ------------------------


def test_update_ticket_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_ticket on a missing ticket wraps the 404 with resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().update_ticket(
            _project(), token="t", ticket_id="42", title="x",
        )
    assert exc.value.status == 404
    assert "ticket 'acme#42' not found" in exc.value.message


def test_add_comment_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_comment on a missing ticket wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().add_comment(_project(), token="t", ticket_id="42", body="hi")
    assert exc.value.status == 404
    assert "ticket 'acme#42' not found" in exc.value.message


def test_update_comment_404_names_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_comment on a missing comment wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().update_comment(
            _project(), token="t", comment_id="99", body="x",
        )
    assert exc.value.status == 404
    assert "comment 'acme#99' not found" in exc.value.message


# ---------- Issue #17 defect 6: add_pr_review_comment 422 names params -------


def test_add_pr_review_comment_422_names_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_pr_review_comment receiving 422 must surface a message that names
    path, line, and commit_sha so the caller knows which inputs were bad."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {
                "message": "Validation Failed",
                "errors": [{"message": "pull_request_review_thread.position is invalid"}],
            },
            status_code=422,
        )

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().add_pr_review_comment(
            _project(),
            token="t",
            pr_id="7",
            body="nit",
            path="src/foo.py",
            line=42,
            commit_sha="abc123",
        )
    msg = exc.value.message
    assert exc.value.status == 422
    assert "path" in msg
    assert "line" in msg
    assert "commit_sha" in msg
    assert "7" in msg  # PR id


# ---------- Ticket #28: create_pr side-step 422 is non-fatal -----------------


def _minimal_pr_payload(number: int = 42) -> dict:
    """Return a minimal GitHub PR REST payload accepted by `_map_pr`."""
    return {
        "number": number,
        "state": "open",
        "title": "Test PR",
        "body": "<!-- #ai-generated -->\nDescription.",
        "user": {"login": "bot"},
        "assignees": [],
        "requested_reviewers": [],
        "labels": [],
        "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "acme/backend"}},
        "base": {"ref": "main", "sha": "def"},
        "draft": False,
        "merged": False,
        "mergeable": None,
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def test_create_pr_reviewer_422_returns_pr_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr with a reviewer 422 must still return the PR with a warning."""

    pr_payload = _minimal_pr_payload(42)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Label ensure (GET or POST /repos/.../labels/...)
        if "/labels" in path and req.method in ("GET", "POST"):
            if req.method == "GET":
                return _json({"name": "ai-generated", "color": "0075ca"})
            # POST /issues/.../labels — success, return label list
            return _json([{"name": "ai-generated"}])
        # Primary PR creation
        if path.endswith("/pulls") and req.method == "POST":
            return _json(pr_payload, status_code=201)
        # Reviewer request — 422
        if "/requested_reviewers" in path and req.method == "POST":
            return _json(
                {"message": "Review cannot be requested from pull request author."},
                status_code=422,
            )
        # Fallback success for any other call
        return _json({})

    _install_mock(monkeypatch, handler)
    pr = GitHubProvider().create_pr(
        _project(),
        token="t",
        title="Test PR",
        body="Description.",
        head="feature",
        base="main",
        requested_reviewers=["author-user"],
    )

    assert pr.number == 42
    assert pr.url == "https://github.com/acme/backend/pull/42"
    assert len(pr.warnings) == 1
    assert "requested_reviewers" in pr.warnings[0]
    assert "422" in pr.warnings[0] or "Review cannot" in pr.warnings[0]


def test_create_pr_reviewer_500_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr with a reviewer 500 must raise GitHubError."""

    pr_payload = _minimal_pr_payload(43)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/labels" in path and req.method == "GET":
            return _json({"name": "ai-generated", "color": "0075ca"})
        if "/labels" in path and req.method == "POST":
            return _json([{"name": "ai-generated"}])
        if path.endswith("/pulls") and req.method == "POST":
            return _json(pr_payload, status_code=201)
        if "/requested_reviewers" in path and req.method == "POST":
            return _json({"message": "Internal Server Error"}, status_code=500)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_pr(
            _project(),
            token="t",
            title="Test PR",
            body="Description.",
            head="feature",
            base="main",
            requested_reviewers=["reviewer"],
        )
    assert exc.value.status == 500


def test_create_pr_primary_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr with a 422 on the primary POST /pulls must raise GitHubError."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/labels" in path and req.method == "GET":
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path.endswith("/pulls") and req.method == "POST":
            return _json(
                {
                    "message": "Validation Failed",
                    "errors": [{"message": "head branch does not exist"}],
                },
                status_code=422,
            )
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_pr(
            _project(),
            token="t",
            title="Test PR",
            body="Description.",
            head="nonexistent",
            base="main",
        )
    assert exc.value.status == 422
