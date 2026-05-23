"""Tests for `list_runs_for_ticket` on the GitHub provider, specifically the
early-bail path where `ticket_id` refers to a PR rather than a plain issue.

We use `httpx.MockTransport` to intercept HTTP calls and return canned
responses; the provider is monkey-patched so `_client(token)` returns a
client backed by our mock transport.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import GitHubProvider


# ---------- helpers ----------------------------------------------------------


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
    """Replace `github._client` so calls go through MockTransport.

    Returns a list that will be populated with every intercepted request,
    for assertion convenience.
    """
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
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _pr_issue_payload(number: int) -> dict:
    """An `/issues/{number}` payload that represents a PR (has `pull_request` key)."""
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "PR body",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "pull_request": {
            "url": f"https://api.github.com/repos/acme/backend/pulls/{number}",
            "html_url": f"https://github.com/acme/backend/pull/{number}",
            "merged_at": None,
        },
    }


def _pr_payload(number: int, head_sha: str) -> dict:
    """A `/pulls/{number}` payload with the given head sha."""
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "head": {
            "sha": head_sha,
            "ref": "feature-branch",
            "label": f"acme:feature-branch",
        },
        "base": {
            "sha": "base000",
            "ref": "main",
        },
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }


def _run_payload(run_id: int, head_sha: str) -> dict:
    """A minimal workflow_run payload."""
    return {
        "id": run_id,
        "name": "CI",
        "head_sha": head_sha,
        "head_branch": "feature-branch",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "html_url": f"https://github.com/acme/backend/actions/runs/{run_id}",
        "created_at": "2024-01-02T00:00:00Z",
        "updated_at": "2024-01-02T01:00:00Z",
        "run_attempt": 1,
        "display_title": "CI run",
    }


# ---------- tests ------------------------------------------------------------


def test_ticket_is_pr_returns_head_sha_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ticket_id is a PR number, resolved_refs is [head_sha] and runs are returned."""
    head_sha = "abc123def456"
    run_id = 999

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_pr_issue_payload(42))
        if path == "/repos/acme/backend/pulls/42":
            return _json(_pr_payload(42, head_sha))
        if path == "/repos/acme/backend/actions/runs":
            # Must be queried by head_sha
            assert req.url.params.get("head_sha") == head_sha
            return _json({"workflow_runs": [_run_payload(run_id, head_sha)]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    runs, resolved_refs = provider.list_runs_for_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert resolved_refs == [head_sha]
    assert len(runs) == 1
    assert runs[0].head_sha == head_sha
    assert runs[0].id == str(run_id)


def test_ticket_is_pr_with_no_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ticket_id is a PR but has no runs, resolved_refs is still non-empty."""
    head_sha = "abc123def456"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_pr_issue_payload(42))
        if path == "/repos/acme/backend/pulls/42":
            return _json(_pr_payload(42, head_sha))
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    runs, resolved_refs = provider.list_runs_for_ticket(
        _project(), token="t", ticket_id="42"
    )
    # resolved_refs must be non-empty even when there are no runs,
    # so the caller can distinguish "PR exists but no runs" from "no linked PR".
    assert resolved_refs == [head_sha]
    assert runs == []


def test_issue_ticket_skips_pr_early_bail(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ticket_id is a plain issue (no pull_request key), the PR early-bail
    is not triggered and /pulls/{id} is never requested."""
    requested_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        requested_paths.append(path)
        if path == "/repos/acme/backend/issues/42":
            # Plain issue — no `pull_request` key.
            return _json({
                "number": 42,
                "title": "Plain issue",
                "body": "no branch reference",
                "state": "open",
                "user": {"login": "alice"},
                "assignees": [],
                "labels": [],
                "html_url": "https://github.com/acme/backend/issues/42",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
            })
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if path == "/search/issues":
            return _json({"items": [], "total_count": 0})
        # The PR early-bail path must NOT be triggered for plain issues.
        if path == "/repos/acme/backend/pulls/42":
            raise AssertionError(
                "/pulls/42 was requested for a plain issue — the PR guard fired incorrectly"
            )
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    runs, resolved_refs = provider.list_runs_for_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert resolved_refs == []
    assert runs == []
    # Confirm /pulls/42 was never in any of the requests.
    assert "/repos/acme/backend/pulls/42" not in requested_paths


# ---------- Issue #17: get_run 404 naming ------------------------------------


def test_get_run_404_names_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_run that receives a 404 must re-raise naming the project and run_id."""
    from lib_python_projects.providers.github import GitHubError

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().get_run(_project(), token="t", run_id="99999")
    assert exc.value.status == 404
    assert "pipeline 'acme#99999' not found" in exc.value.message


def test_get_run_non_numeric_404_naming(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_run with a non-numeric run_id (e.g. 'main') gets a 404 that names
    the run_id in the error (GitHub simply returns 404 for unknown ids)."""
    from lib_python_projects.providers.github import GitHubError

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().get_run(_project(), token="t", run_id="main")
    assert exc.value.status == 404
    assert "main" in exc.value.message
