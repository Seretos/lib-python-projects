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


def test_list_pr_reviews_missing_submitted_at_is_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review payload with no `submitted_at` maps to `""`, not `None` —
    `Review.submitted_at` is typed `str`, not `str | None`."""

    def handler(req: httpx.Request) -> httpx.Response:
        payload = _review(8, "APPROVED")
        del payload["submitted_at"]
        return _json([payload])

    _install_mock(monkeypatch, handler)
    reviews = GitHubProvider().list_pr_reviews(_project(), token="t", pr_id="7")
    assert reviews[0].submitted_at == ""


# ---------- get_pr reviews (ticket #148) --------------------------------------


def _pr_payload(number: int = 7) -> dict:
    """Minimal GitHub PR REST payload accepted by _map_pr."""
    return {
        "number": number,
        "state": "open",
        "title": "Test PR",
        "body": "Description.",
        "user": {"login": "bot"},
        "assignees": [],
        "requested_reviewers": [],
        "labels": [],
        "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "acme/backend"}},
        "base": {"ref": "main", "sha": "def"},
        "draft": False,
        "merged": False,
        "mergeable": None,
        "mergeable_state": "unknown",
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _install_get_pr_mock(
    monkeypatch: pytest.MonkeyPatch,
    reviews_payload: list[dict],
    pr_number: int = 7,
) -> list[httpx.Request]:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/repos/acme/backend/pulls/{pr_number}":
            return _json(_pr_payload(pr_number))
        if path == f"/repos/acme/backend/issues/{pr_number}/comments":
            return _json([])
        if path == f"/repos/acme/backend/pulls/{pr_number}/reviews":
            return _json(reviews_payload)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    return _install_mock(monkeypatch, handler)


def test_get_pr_populates_reviews_reviewers_and_decision_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ticket #148: get_pr previously left `pr.reviews`
    empty, `pr.reviewers` hardcoded `[]`, and `pr.review_decision` `None`
    on the REST path. A single APPROVED review must now populate all
    three."""
    _install_get_pr_mock(monkeypatch, [_review(1, "APPROVED")])
    pr, _ = GitHubProvider().get_pr(_project(), token="t", pr_id="7")
    assert len(pr.reviews) == 1
    assert pr.reviews[0].id == "1"
    assert pr.reviews[0].state == "approve"
    assert pr.reviewers == ["reviewer1"]
    assert pr.review_decision == "APPROVED"


def test_get_pr_empty_reviews_leaves_fields_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No submitted reviews -> reviews=[], reviewers=[], review_decision=None."""
    _install_get_pr_mock(monkeypatch, [])
    pr, _ = GitHubProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.reviews == []
    assert pr.reviewers == []
    assert pr.review_decision is None


def test_get_pr_changes_requested_wins_over_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES_REQUESTED from one author and APPROVED from another ->
    review_decision is CHANGES_REQUESTED (a single blocking review
    outweighs an approval from someone else)."""
    _install_get_pr_mock(
        monkeypatch,
        [
            _review(1, "APPROVED", user={"login": "alice"}),
            _review(2, "CHANGES_REQUESTED", user={"login": "bob"}),
        ],
    )
    pr, _ = GitHubProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.review_decision == "CHANGES_REQUESTED"
    assert set(pr.reviewers) == {"alice", "bob"}


def test_get_pr_pending_review_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PENDING (unsubmitted draft) review must not surface in
    `pr.reviews`/`pr.reviewers`, nor influence `review_decision`."""
    _install_get_pr_mock(monkeypatch, [_review(1, "PENDING")])
    pr, _ = GitHubProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.reviews == []
    assert pr.reviewers == []
    assert pr.review_decision is None


def test_get_pr_same_author_multiple_reviews_latest_state_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same author submits CHANGES_REQUESTED then later APPROVED (a
    re-review) -> only the latest state counts, the author appears once
    in `pr.reviewers`, and the decision reflects the newer APPROVED
    state (not stuck on the earlier CHANGES_REQUESTED)."""
    _install_get_pr_mock(
        monkeypatch,
        [
            _review(
                1, "CHANGES_REQUESTED",
                submitted_at="2024-01-01T00:00:00Z",
                user={"login": "alice"},
            ),
            _review(
                2, "APPROVED",
                submitted_at="2024-01-02T00:00:00Z",
                user={"login": "alice"},
            ),
        ],
    )
    pr, _ = GitHubProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.reviewers == ["alice"]
    assert pr.review_decision == "APPROVED"
    assert len(pr.reviews) == 2  # pr.reviews keeps the full history
