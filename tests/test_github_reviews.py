"""Tests for `GitHubProvider.list_pr_reviews` (ticket #148, finding 1).

Covers:
  - `GET /repos/{o}/{r}/pulls/{n}/reviews` request shape.
  - State normalization: APPROVED -> approve, CHANGES_REQUESTED ->
    request_changes, COMMENTED/DISMISSED -> comment.
  - PENDING reviews (unsubmitted drafts) are skipped.
  - Field mapping: id/author/body/url/submitted_at/commit_sha.
  - Edge cases: empty list -> []; missing commit_id -> None;
    body: null -> "".

Pattern mirrors `tests/test_github_list_prs.py`:
  httpx.MockTransport + monkeypatch on `github_provider._client`.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import GitHubProvider


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


def _review(review_id: int, state: str, **overrides) -> dict:
    base: dict = {
        "id": review_id,
        "user": {"login": "reviewer1"},
        "body": "looks good",
        "state": state,
        "html_url": f"https://github.com/acme/backend/pull/7#pullrequestreview-{review_id}",
        "submitted_at": "2024-01-01T00:00:00Z",
        "commit_id": "abc123",
    }
    base.update(overrides)
    return base


def test_list_pr_reviews_maps_state_and_skips_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APPROVED/CHANGES_REQUESTED/COMMENTED map to normalized states;
    PENDING (unsubmitted draft review) is skipped entirely."""
    reviews_payload = [
        _review(1, "APPROVED"),
        _review(2, "CHANGES_REQUESTED"),
        _review(3, "COMMENTED"),
        _review(4, "PENDING"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/pulls/7/reviews"
        assert dict(req.url.params)["per_page"] == "100"
        return _json(reviews_payload)

    _install_mock(monkeypatch, handler)
    reviews = GitHubProvider().list_pr_reviews(_project(), token="t", pr_id="7")
    assert [rv.id for rv in reviews] == ["1", "2", "3"]
    assert [rv.state for rv in reviews] == ["approve", "request_changes", "comment"]
    for rv in reviews:
        assert rv.author == "reviewer1"
        assert rv.body == "looks good"
        assert rv.commit_sha == "abc123"


def test_list_pr_reviews_dismissed_maps_to_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DISMISSED reviews normalize to the "comment" state, same as
    COMMENTED — GitHub's DISMISSED is a comment-shaped review whose
    effect was administratively cleared, not a distinct review verb."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([_review(5, "DISMISSED")])

    _install_mock(monkeypatch, handler)
    reviews = GitHubProvider().list_pr_reviews(_project(), token="t", pr_id="7")
    assert len(reviews) == 1
    assert reviews[0].state == "comment"


def test_list_pr_reviews_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """No reviews on the PR -> empty list, not an error."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    reviews = GitHubProvider().list_pr_reviews(_project(), token="t", pr_id="7")
    assert reviews == []


def test_list_pr_reviews_missing_commit_id_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review payload with no `commit_id` maps to `commit_sha=None`,
    not a KeyError or a fabricated placeholder."""

    def handler(req: httpx.Request) -> httpx.Response:
        payload = _review(6, "APPROVED")
        del payload["commit_id"]
        return _json([payload])

    _install_mock(monkeypatch, handler)
    reviews = GitHubProvider().list_pr_reviews(_project(), token="t", pr_id="7")
    assert reviews[0].commit_sha is None


def test_list_pr_reviews_null_body_becomes_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`body: null` (a bare approve with no note) must map to `""`,
    never `None` — GitHub always emits `str` for Review.body per the
    shared "null vs empty string" convention documented on `Review`."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([_review(7, "APPROVED", body=None)])

    _install_mock(monkeypatch, handler)
    reviews = GitHubProvider().list_pr_reviews(_project(), token="t", pr_id="7")
    assert reviews[0].body == ""
