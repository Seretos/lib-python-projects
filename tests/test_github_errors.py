"""Tests for GitHub provider error-contract fixes (ticket #17).

Covers:
- update_ticket 404 → "ticket '<project>#<id>' not found"
- add_comment 404 → "ticket '<project>#<id>' not found"
- update_comment 404 → "comment '<project>#<id>' not found"
- add_pr_review_comment 422 → named parameter message
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
