"""Tests for GitHubProvider.merge_pr (ticket #56).

Covers:
- pre-flight GET merged=True → GitHubError(405, "already merged"), no PUT
- normal success path: pre-flight merged=False, PUT 200, re-fetch → PullRequest merged=True
- PUT 405 merge-conflict path (existing behaviour, no regression)
- PUT 405 race: pre-flight merged=False but probe after PUT shows merged=True
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers.github import GitHubError, GitHubProvider
from lib_python_projects.providers.base import PullRequest


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


def _pr_payload(number: int = 7, merged: bool = False) -> dict:
    """Minimal GitHub PR REST payload accepted by _map_pr."""
    return {
        "number": number,
        "state": "closed" if merged else "open",
        "title": "Test PR",
        "body": "<!-- #ai-generated -->\nDescription.",
        "user": {"login": "bot"},
        "assignees": [],
        "requested_reviewers": [],
        "labels": [],
        "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "acme/backend"}},
        "base": {"ref": "main", "sha": "def"},
        "draft": False,
        "merged": merged,
        "mergeable": None,
        "mergeable_state": "unknown",
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


# ---------- regression: pre-flight guards against silent double-merge ----------


def test_merge_pr_already_merged_precheck_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for ticket #56: when pre-flight GET shows merged=True,
    GitHubError(405, '...is already merged') is raised and no PUT is issued."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        # Pre-flight GET /pulls/7
        if req.method == "GET" and path.endswith("/pulls/7"):
            return _json(_pr_payload(7, merged=True))
        # PUT must NOT be reached
        if req.method == "PUT" and "/merge" in path:
            raise AssertionError("PUT /merge should not be called for already-merged PR")
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().merge_pr(_project(), token="t", pr_id="7")

    assert exc.value.status == 405
    assert "already merged" in exc.value.message
    assert "acme#7" in exc.value.message
    # Confirm no PUT was issued
    assert not any(r.method == "PUT" for r in seen), (
        "PUT /merge must not be called when pre-flight shows PR is already merged"
    )


# ---------- success path -------------------------------------------------------


def test_merge_pr_success_normal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal merge: pre-flight merged=False, PUT 200, re-fetch → PullRequest merged=True."""

    get_count: dict[str, int] = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/pulls/7"):
            get_count["n"] += 1
            # First GET is the pre-flight (not merged), second is the re-fetch
            merged = get_count["n"] > 1
            return _json(_pr_payload(7, merged=merged))
        if req.method == "PUT" and path.endswith("/pulls/7/merge"):
            return _json(
                {"sha": "abc123", "merged": True, "message": "Pull Request successfully merged"},
                status_code=200,
            )
        return _json({})

    _install_mock(monkeypatch, handler)
    pr = GitHubProvider().merge_pr(_project(), token="t", pr_id="7")

    assert isinstance(pr, PullRequest)
    assert pr.merged is True
    assert get_count["n"] == 2, "expected 2 GET calls: pre-flight + re-fetch"


# ---------- existing 405 conflict path (no regression) -----------------------


def test_merge_pr_405_conflict_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT 405 with non-merged state → conflict error (existing behaviour preserved)."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/pulls/7"):
            raw = _pr_payload(7, merged=False)
            raw["mergeable_state"] = "dirty"
            return _json(raw)
        if req.method == "PUT" and path.endswith("/pulls/7/merge"):
            return _json({"message": "Pull Request is not mergeable"}, status_code=405)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().merge_pr(_project(), token="t", pr_id="7")

    assert exc.value.status == 405
    assert "cannot be merged" in exc.value.message
    assert "dirty" in exc.value.message


# ---------- 405 race: pre-flight not-merged but probe shows merged ------------


def test_merge_pr_405_already_merged_via_405_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race condition: pre-flight shows not-merged, PUT returns 405, probe shows merged=True.
    Must raise the 'already merged' error (covers the race path)."""

    get_count: dict[str, int] = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/pulls/7"):
            get_count["n"] += 1
            # First GET (pre-flight): not merged
            # Subsequent GETs (probe after 405): merged
            merged = get_count["n"] > 1
            return _json(_pr_payload(7, merged=merged))
        if req.method == "PUT" and path.endswith("/pulls/7/merge"):
            return _json({"message": "Pull Request is not mergeable"}, status_code=405)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().merge_pr(_project(), token="t", pr_id="7")

    assert exc.value.status == 405
    assert "already merged" in exc.value.message
