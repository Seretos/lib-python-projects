"""Tests for ticket #205: `add_pr_review_comment` routes inline comments
through GitHub's pending-review flow instead of posting directly to
`POST /pulls/{n}/comments`, which implicitly wraps every comment in its
own auto-submitted (`COMMENTED`, `body: ""`) review — polluting
`list_pr_reviews()` / `get_pr().reviews` with orphan entries.

Covers:
  R1: new-thread comment (no pending review yet) creates one via
      `POST /pulls/{n}/reviews` seeded with `comments`; the payload has
      no `event` key and the right `comments[0]` shape; the legacy
      `/pulls/{n}/comments` endpoint is never hit.
  R2: a second new-thread comment (pending review already exists)
      reuses it via GraphQL `addPullRequestReviewThread` — no duplicate
      review is created.
  R3: reply mode resolves the parent comment's node id then posts via
      GraphQL `addPullRequestReviewComment`; covers both branches —
      pending review already exists, and reply-when-no-pending-review
      -yet (one is created first, bare, then the reply attaches to it).
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_mod
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
            base_url=github_mod.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content or b"{}")


def _pending_review(review_id: int = 900, node_id: str = "REVIEW_NODE_900") -> dict:
    return {
        "id": review_id,
        "node_id": node_id,
        "state": "PENDING",
        "user": {"login": "me"},
        "body": "",
        "html_url": f"https://github.com/acme/backend/pull/7#pullrequestreview-{review_id}",
        "submitted_at": None,
        "commit_id": "abc123",
    }


def _comment(comment_id: int, node_id: str | None = None, **overrides) -> dict:
    base = {
        "id": comment_id,
        "node_id": node_id or f"COMMENT_NODE_{comment_id}",
        "user": {"login": "me"},
        "body": "some text",
        "path": "src/foo.py",
        "line": 10,
        "side": "RIGHT",
        "commit_id": "abc123",
        "in_reply_to_id": None,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "html_url": f"https://github.com/acme/backend/pull/7#discussion_r{comment_id}",
    }
    base.update(overrides)
    return base


# ---------- R1: new thread, no pending review -> create + seed --------------


def test_add_pr_review_comment_new_thread_creates_pending_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First inline comment on a PR (no pending review yet) creates one
    via `POST /pulls/{n}/reviews` seeded with `comments`. The payload
    must carry no `event` key — that's what makes GitHub leave the
    review `PENDING` instead of auto-submitting it — and `comments[0]`
    must carry the right path/line/side/body. The legacy
    `/pulls/{n}/comments` endpoint must never be hit."""
    created_review = _pending_review()
    created_comment = _comment(9001, path="src/foo.py", line=10, side="RIGHT")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            payload = _body(req)
            assert "event" not in payload
            assert payload["commit_id"] == "abc123"
            assert len(payload["comments"]) == 1
            c = payload["comments"][0]
            assert c["path"] == "src/foo.py"
            assert c["line"] == 10
            assert c["side"] == "RIGHT"
            assert "nit" in c["body"]
            return _json(created_review)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/900/comments":
            return _json([created_comment])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/foo.py",
        line=10,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9001"
    assert not any(
        r.method == "POST" and r.url.path == "/repos/acme/backend/pulls/7/comments"
        for r in seen
    )


# ---------- R2: new thread, pending review exists -> GraphQL reuse ----------


def test_add_pr_review_comment_second_new_thread_reuses_pending_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second new-thread comment, with a PENDING review already on
    the PR, reuses it via GraphQL `addPullRequestReviewThread` instead
    of creating a duplicate review."""
    pending = _pending_review()
    fetched = _comment(9002, path="src/bar.py", line=20, side="RIGHT")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert "addPullRequestReviewThread" in gql["query"]
            v = gql["variables"]
            assert v["reviewId"] == "REVIEW_NODE_900"
            assert v["path"] == "src/bar.py"
            assert v["line"] == 20
            assert v["side"] == "RIGHT"
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {"comments": {"nodes": [{"databaseId": 9002}]}}
                        }
                    }
                }
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9002":
            return _json(fetched)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="second nit",
        path="src/bar.py",
        line=20,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9002"
    assert not any(
        r.method == "POST" and r.url.path == "/repos/acme/backend/pulls/7/reviews"
        for r in seen
    )


# ---------- R3: reply mode ---------------------------------------------------


def test_add_pr_review_comment_reply_with_pending_review_uses_graphql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply, with a PENDING review already on the PR, resolves the
    parent comment's node id then posts via GraphQL
    `addPullRequestReviewComment(inReplyTo:)`."""
    pending = _pending_review()
    parent = _comment(8001, node_id="PARENT_NODE_8001")
    reply = _comment(9003, in_reply_to_id=8001)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8001":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert "addPullRequestReviewComment" in gql["query"]
            v = gql["variables"]
            assert v["reviewId"] == "REVIEW_NODE_900"
            assert v["inReplyTo"] == "PARENT_NODE_8001"
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": {"databaseId": 9003}}}}
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9003":
            return _json(reply)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8001",
    )
    assert result.id == "9003"
    assert result.in_reply_to == "8001"


def test_add_pr_review_comment_reply_with_no_pending_review_creates_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply, with no PENDING review yet, first creates a bare
    pending review (no `comments`/`event` in the payload) and then
    attaches the reply to it via GraphQL."""
    created_review = _pending_review()
    parent = _comment(8001, node_id="PARENT_NODE_8001")
    reply = _comment(9004, in_reply_to_id=8001)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            payload = _body(req)
            assert "event" not in payload
            assert "comments" not in payload
            return _json(created_review)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8001":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert "addPullRequestReviewComment" in gql["query"]
            assert gql["variables"]["reviewId"] == "REVIEW_NODE_900"
            assert gql["variables"]["inReplyTo"] == "PARENT_NODE_8001"
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": {"databaseId": 9004}}}}
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9004":
            return _json(reply)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply", in_reply_to="8001",
    )
    assert result.id == "9004"


def test_add_pr_review_comment_reply_with_bad_parent_leaves_no_pending_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix-round 2, finding 1: a reply with a bad/404-ing `in_reply_to`,
    when no pending review exists yet, must NOT create a pending review
    on the server before failing. The parent comment's node id has to be
    resolved/validated first; only once that succeeds should a pending
    review be created. Otherwise a failed reply leaves an orphaned,
    empty `PENDING` review dangling on the PR."""
    from lib_python_projects.providers.github import GitHubError

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/99999":
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError):
        GitHubProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="reply", in_reply_to="99999",
        )
    assert not any(
        r.method == "POST" and r.url.path == "/repos/acme/backend/pulls/7/reviews"
        for r in seen
    )


# ---------- fix-round 2, finding 2: commit_sha contract on reused review ----


def test_add_pr_review_comment_reused_review_rejects_mismatched_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a pending review already exists, a new-thread `commit_sha`
    that does not match the pending review's own commit must raise
    rather than being silently ignored — GitHub does not support mixing
    commits within one review. Must fail fast: no GraphQL mutation may
    be sent."""
    pending = _pending_review()  # commit_id == "abc123"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="commit_sha"):
        GitHubProvider().add_pr_review_comment(
            _project(),
            token="t",
            pr_id="7",
            body="third nit",
            path="src/baz.py",
            line=30,
            side="RIGHT",
            commit_sha="different-sha",
        )
    assert not any(r.method == "POST" and r.url.path == "/graphql" for r in seen)


def test_add_pr_review_comment_reused_review_accepts_matching_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: passing the SAME commit_sha as the pending
    review's own commit on a second new-thread call still succeeds
    normally (no false-positive rejection)."""
    pending = _pending_review()  # commit_id == "abc123"
    fetched = _comment(9005, path="src/baz.py", line=30, side="RIGHT")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert "addPullRequestReviewThread" in gql["query"]
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {"comments": {"nodes": [{"databaseId": 9005}]}}
                        }
                    }
                }
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9005":
            return _json(fetched)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="matching commit",
        path="src/baz.py",
        line=30,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9005"
