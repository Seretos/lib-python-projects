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


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
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


# ---------- epic #224 (220.2): null GraphQL payload -> structured 422 -------
#
# GitHub can return `thread: null` / `comment: null` from these mutations
# with NO top-level `errors[]` key — the existing `resp_body.get("errors")`
# guard never fires, so the un-guarded payload navigation
# (`resp_body["data"][...]["thread"]["comments"]["nodes"]`) raises a raw
# `TypeError: 'NoneType' object is not subscriptable` that escapes
# `add_pr_review_comment`'s own `except GitHubError` handler entirely.
# These tests cover only the two diagnosed null shapes — NOT `{"data":
# null}` (a standard GraphQL top-level failure) or an empty `nodes` list
# (the thread WAS created), which are different failure modes this fix
# must not misclassify as "could not resolve diff location".


def test_add_thread_to_pending_review_null_thread_raises_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R12: a second new-thread comment (pending review already exists,
    routed through `_add_thread_to_pending_review`'s GraphQL call) whose
    response is `{"data": {"addPullRequestReviewThread": {"thread":
    null}}}` with no `errors` key must raise a structured `GitHubError`
    (422) instead of crashing with `TypeError` — `add_pr_review_comment`'s
    outer handler then converts that 422 into the actionable "could not
    resolve diff location" message (no new user-facing wording is
    invented here)."""
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            return _json(
                {"data": {"addPullRequestReviewThread": {"thread": None}}}
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(github_mod.GitHubError) as exc:
        GitHubProvider().add_pr_review_comment(
            _project(),
            token="t",
            pr_id="7",
            body="second nit",
            path="src/bar.py",
            line=20,
            side="RIGHT",
            commit_sha="abc123",
        )
    assert exc.value.status == 422
    assert "could not resolve diff location" in exc.value.message
    assert "path='src/bar.py'" in exc.value.message
    assert "line=20" in exc.value.message
    assert "commit_sha='abc123'" in exc.value.message


def test_add_thread_to_pending_review_errors_key_still_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage (must already pass — pre-existing behaviour):
    the pre-existing `errors[]`-populated path via `_graphql_review_error`
    is untouched by the null-payload guard and must stay 422."""
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            return _json(
                {"errors": [{"type": "UNPROCESSABLE", "message": "bad position"}]}
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(github_mod.GitHubError) as exc:
        GitHubProvider().add_pr_review_comment(
            _project(),
            token="t",
            pr_id="7",
            body="second nit",
            path="src/bar.py",
            line=20,
            side="RIGHT",
            commit_sha="abc123",
        )
    assert exc.value.status == 422


def test_add_thread_to_pending_review_top_level_data_null_not_mislabeled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary test (epic #224 critique-gate note 4): `{"data": null}`
    with no top-level `errors` key is the standard GraphQL
    top-level-failure shape — a genuinely different failure mode from
    the diagnosed `thread: null` case. The null-payload guard must not
    be widened to swallow it: it must NOT come out as the friendly
    "could not resolve diff location" 422 (that would misrepresent a
    top-level GraphQL failure as a diff-location problem). This repo's
    fix deliberately leaves this shape unguarded — it surfaces as the
    raw `TypeError` from navigating `None["addPullRequestReviewThread"]`,
    the same way it did before the #220.2 fix, because widening the
    guard to cover it is explicitly out of scope."""
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            return _json({"data": None})
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(TypeError):
        GitHubProvider().add_pr_review_comment(
            _project(),
            token="t",
            pr_id="7",
            body="second nit",
            path="src/bar.py",
            line=20,
            side="RIGHT",
            commit_sha="abc123",
        )


def test_add_thread_to_pending_review_empty_nodes_not_mislabeled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary test (epic #224 critique-gate note 4): an empty
    `comments.nodes` list means the thread WAS created (GitHub just
    didn't echo a comment node back) — a different failure mode from a
    null `thread`. The null-payload guard only fires on `thread is
    None`, so it does not intercept this case at all: it must NOT come
    out as "could not resolve diff location" either. It surfaces as the
    raw `IndexError` from `nodes[0]` on an empty list, which is the
    correct (if unfriendly) behaviour for this out-of-scope shape."""
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {"comments": {"nodes": []}}
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(IndexError):
        GitHubProvider().add_pr_review_comment(
            _project(),
            token="t",
            pr_id="7",
            body="second nit",
            path="src/bar.py",
            line=20,
            side="RIGHT",
            commit_sha="abc123",
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


def test_reply_in_pending_review_null_comment_raises_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R12 (epic #224 / ticket #220.2): a reply (pending review already
    exists, routed through `_reply_in_pending_review`'s GraphQL call)
    whose response is `{"data": {"addPullRequestReviewComment":
    {"comment": null}}}` with no `errors` key must raise a structured
    `GitHubError` (422) instead of crashing with `TypeError` — same
    null-payload guard as the new-thread mutation, mirrored onto the
    reply mutation.

    Decision for the developer-phase note on "what the reply-mode
    message says" (plan carries this forward as an under-specified
    observable): `add_pr_review_comment`'s outer `except GitHubError`
    handler (github.py ~5238-5245) is a single generic wrap shared by
    both the new-thread and reply branches — ticket #220.2 explicitly
    keeps it that way ("no new user-facing wording is invented"; step 8
    of the plan). It is not mode-aware, so on a reply (`in_reply_to` set,
    `path`/`line`/`commit_sha` all `None` because the caller never
    supplied them) it renders those three as `None` verbatim rather than
    omitting them. That is a known, accepted quirk of reusing the
    existing wrap as-is instead of special-casing reply mode — pinned
    here explicitly rather than left as a vague "same 422", per this
    round's critique note asking for the exact reply-mode observable."""
    pending = _pending_review()
    parent = _comment(8001, node_id="PARENT_NODE_8001")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8001":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": None}}}
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(github_mod.GitHubError) as exc:
        GitHubProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="reply text", in_reply_to="8001",
        )
    assert exc.value.status == 422
    assert "could not resolve diff location" in exc.value.message
    # Pin the reply-mode quirk explicitly: path/line/commit_sha are all
    # `None` in reply mode, and the shared outer wrap names them as such.
    assert "path=None" in exc.value.message
    assert "line=None" in exc.value.message
    assert "commit_sha=None" in exc.value.message


def test_reply_in_pending_review_null_comment_created_review_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage: when THIS call created the pending review
    (none existed yet) and the reply mutation then comes back with a
    null `comment` (the R12 failure), the existing fix-round-3 cleanup
    contract must still fire — the just-created, still-empty pending
    review is deleted rather than left orphaned, exactly as it already
    does for any other exception from this call."""
    created_review = _pending_review()
    parent = _comment(8001, node_id="PARENT_NODE_8001")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json(created_review)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8001":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": None}}}
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/900/comments":
            return _json([])
        if req.method == "DELETE" and path == "/repos/acme/backend/pulls/7/reviews/900":
            return httpx.Response(status_code=204)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(github_mod.GitHubError) as exc:
        GitHubProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="reply text", in_reply_to="8001",
        )
    assert exc.value.status == 422
    assert any(
        r.method == "DELETE" and r.url.path == "/repos/acme/backend/pulls/7/reviews/900"
        for r in seen
    )


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
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/comments":
            # A1's fallback listing: the 404 above is genuine, the parent
            # id isn't on the fallback listing either.
            return _json([])
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


# ---------- fix-round 3, finding 1: _find_pending_review paginates ----------


def test_add_pr_review_comment_finds_pending_review_on_second_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR with more than 100 reviews can have its PENDING review sit
    on page 2+ of `GET /pulls/{n}/reviews`. `_find_pending_review` must
    follow the `Link: rel="next"` header and keep paging rather than
    stopping after page 1 — otherwise it wrongly concludes there is no
    pending review and a second, duplicate one gets created."""
    page1 = [{"id": i, "state": "APPROVED"} for i in range(100)]
    pending = _pending_review()
    fetched = _comment(9006, path="src/qux.py", line=5, side="RIGHT")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            page = dict(req.url.params).get("page", "1")
            if page == "1":
                return _json(
                    page1,
                    headers={
                        "Link": (
                            '<https://api.github.com/repos/acme/backend'
                            '/pulls/7/reviews?page=2>; rel="next"'
                        )
                    },
                )
            if page == "2":
                return _json([pending])
            raise AssertionError(f"unexpected page: {page}")
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert "addPullRequestReviewThread" in gql["query"]
            assert gql["variables"]["reviewId"] == "REVIEW_NODE_900"
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {"comments": {"nodes": [{"databaseId": 9006}]}}
                        }
                    }
                }
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9006":
            return _json(fetched)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="fourth nit",
        path="src/qux.py",
        line=5,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9006"
    # Must reuse the existing pending review found on page 2, never
    # create a duplicate one.
    assert not any(
        r.method == "POST" and r.url.path == "/repos/acme/backend/pulls/7/reviews"
        for r in seen
    )


# ---------- fix-round 3, finding 2: cleanup an orphaned pending review ------


def test_add_pr_review_comment_reply_cleans_up_review_when_graphql_fails_for_other_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If this call creates the pending review itself and the follow-up
    GraphQL reply call then fails for a reason OTHER than a bad parent
    id (here: a generic 500), and the review's own comments (re-fetched
    to verify) are still empty — i.e. the mutation never actually
    landed — the just-created pending review must be deleted before the
    error is re-raised; otherwise it's left dangling, empty, on the
    PR."""
    from lib_python_projects.providers.github import GitHubError

    created_review = _pending_review()
    parent = _comment(8002, node_id="PARENT_NODE_8002")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8002":
            return _json(parent)
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json(created_review)
        if req.method == "POST" and path == "/graphql":
            return _json({"message": "internal server error"}, status_code=500)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/900/comments":
            # Verification re-fetch: the review is still genuinely
            # empty — the mutation never landed — so cleanup should
            # proceed with the delete.
            return _json([])
        if req.method == "DELETE" and path == "/repos/acme/backend/pulls/7/reviews/900":
            return _json({})
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError):
        GitHubProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="reply", in_reply_to="8002",
        )
    assert any(
        r.method == "DELETE" and r.url.path == "/repos/acme/backend/pulls/7/reviews/900"
        for r in seen
    )


def test_add_pr_review_comment_reply_does_not_delete_review_when_reply_actually_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix-round 4, finding 2: if the GraphQL reply mutation actually
    succeeded server-side (the review now has a comment attached when
    re-fetched) but the client still saw the call as failed — e.g. a
    timeout or a malformed response after the mutation landed — cleanup
    must NOT delete the review. Deleting it would silently destroy the
    comment that was actually posted, which is worse than the orphan
    this cleanup exists to prevent. The original exception must still
    propagate to the caller."""
    from lib_python_projects.providers.github import GitHubError

    created_review = _pending_review()
    parent = _comment(8004, node_id="PARENT_NODE_8004")
    landed_comment = _comment(9004, node_id="COMMENT_NODE_9004")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8004":
            return _json(parent)
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json(created_review)
        if req.method == "POST" and path == "/graphql":
            return _json({"message": "internal server error"}, status_code=500)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/900/comments":
            # Verification re-fetch: the mutation actually landed —
            # the review already has a real comment on it — so cleanup
            # must leave it alone.
            return _json([landed_comment])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError):
        GitHubProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="reply", in_reply_to="8004",
        )
    assert not any(r.method == "DELETE" for r in seen)


def test_add_pr_review_comment_reply_does_not_delete_preexisting_pending_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a pending review already existed BEFORE this call (found via
    `_find_pending_review`, not created by it) and the follow-up GraphQL
    reply fails, cleanup must NOT delete it — only a review this call
    itself created is eligible for cleanup."""
    from lib_python_projects.providers.github import GitHubError

    pending = _pending_review()
    parent = _comment(8003, node_id="PARENT_NODE_8003")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8003":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            return _json({"message": "internal server error"}, status_code=500)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError):
        GitHubProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="reply", in_reply_to="8003",
        )
    assert not any(r.method == "DELETE" for r in seen)


# ---------- ticket #205 fix-round 5: warn when pagination cap is hit --------


def test_find_pending_review_logs_warning_when_pagination_cap_hit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_find_pending_review`'s pagination loop bails out after 100 pages
    as a safety cap against a pathological/misbehaving API response. If a
    PR genuinely has more than 10,000 reviews and the caller's PENDING
    review sits beyond that cap, this silently returns `None` even though
    a pending review exists. Hitting the cap must log a warning so this
    doesn't happen with zero signal."""
    import logging

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        assert req.method == "GET"
        assert path == "/repos/acme/backend/pulls/7/reviews"
        page = dict(req.url.params).get("page", "1")
        return _json(
            [{"id": int(page), "state": "APPROVED"}],
            headers={
                "Link": (
                    f'<https://api.github.com/repos/acme/backend/pulls/7'
                    f'/reviews?page={int(page) + 1}>; rel="next"'
                )
            },
        )

    _install_mock(monkeypatch, handler)
    client = github_mod._client("t")
    try:
        with caplog.at_level(logging.WARNING, logger="project-issues.github"):
            result = github_mod._find_pending_review(client, _project(), "7")
    finally:
        client.close()

    assert result is None
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_texts, "Expected a warning about the pagination cap, got none"
    combined = " ".join(warning_texts)
    assert "acme/backend#7" in combined, (
        f"Expected owner/repo#pr_id in warning: {combined!r}"
    )
    assert "100" in combined, f"Expected the page cap in warning: {combined!r}"


# =============================================================================
# WP #241 (bundles #233 + #234): reliable add_pr_review_comment writes
# =============================================================================
#
# Behaviour 1 (#233 bug 1): reply mode must resolve the *thread anchor*
# node id, with a pending-comment-aware fallback when the single-comment
# REST endpoint 404s (it does not serve comments still attached to an
# unsubmitted PENDING review — precisely the shape this library creates).
#
# Behaviour 2 (#233 bug 2): a successful mutation must never be reported
# as a failure just because the post-write confirmation *read* 404s (or
# otherwise fails) — the pending comment it's trying to re-fetch may not
# be visible on that endpoint yet/at all.
#
# Behaviour 3 (#234 finding 1): when the immediate response is synthesized
# from known call params (because the confirmation read came back empty),
# `line`/`side`/`original_line` must be filled from those params instead
# of surfacing as `null`.


def test_reply_to_pending_parent_falls_back_to_pr_comments_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 1 driving test. Replying to a comment that still belongs
    to our own unsubmitted PENDING review 404s on the single-comment
    endpoint (`GET /pulls/comments/{id}`) — that endpoint does not serve
    pending-review comments. The fix must fall back to scanning
    `GET /pulls/{n}/comments?per_page=100` (which DOES return the
    caller's own pending comments) to resolve the thread-anchor node id,
    then proceed with the GraphQL reply mutation.

    RED reason (today): `_review_comment_node_id` only tries the
    single-comment GET; a 404 there is rewrapped straight into
    `GitHubError(404, "review comment 'X' not found")` and the GraphQL
    mutation is never sent."""
    pending = _pending_review()
    parent = _comment(8100, node_id="PARENT_NODE_8100")
    reply = _comment(9200, in_reply_to_id=8100)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8100":
            return _json({"message": "Not Found"}, status_code=404)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/comments":
            assert dict(req.url.params).get("per_page") == "100"
            return _json([parent])
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert "addPullRequestReviewComment" in gql["query"]
            assert gql["variables"]["inReplyTo"] == "PARENT_NODE_8100"
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": {"databaseId": 9200}}}}
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9200":
            return _json(reply)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8100",
    )
    assert result.id == "9200"
    assert result.in_reply_to == "8100"


def test_reply_to_a_reply_uses_anchor_node_id_not_the_replys_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 1 edge case: replying to a comment that is ITSELF a
    reply must resolve to the thread ANCHOR's node id (its
    `in_reply_to_id`), not the directly-addressed comment's own node id
    — `addPullRequestReviewThread(inReplyTo:)` wants the anchor.

    RED reason (today): `_review_comment_node_id` returns the directly-
    fetched comment's own `node_id` unconditionally, never looking at
    `in_reply_to_id` — so the mutation is sent with the wrong (non-
    anchor) node id."""
    pending = _pending_review()
    mid_reply = _comment(
        8100, node_id="MID_NODE_8100", in_reply_to_id=8050,
    )
    anchor = _comment(8050, node_id="ANCHOR_NODE_8050")
    reply = _comment(9201, in_reply_to_id=8100)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8100":
            return _json(mid_reply)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8050":
            return _json(anchor)
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert gql["variables"]["inReplyTo"] == "ANCHOR_NODE_8050"
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": {"databaseId": 9201}}}}
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9201":
            return _json(reply)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8100",
    )
    assert result.id == "9201"


def test_reply_fallback_listing_spans_two_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 1 edge case: the `GET /pulls/{n}/comments` fallback
    listing must paginate — a PR with more than 100 review comments can
    have the pending-review parent sitting on page 2+.

    RED reason (today): no fallback listing is attempted at all, so the
    call 404s regardless of how many pages exist."""
    pending = _pending_review()
    parent = _comment(8200, node_id="PARENT_NODE_8200")
    page1 = [_comment(7000 + i) for i in range(100)]
    reply = _comment(9202, in_reply_to_id=8200)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8200":
            return _json({"message": "Not Found"}, status_code=404)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/comments":
            page = dict(req.url.params).get("page", "1")
            if page == "1":
                return _json(
                    page1,
                    headers={
                        "Link": (
                            '<https://api.github.com/repos/acme/backend'
                            '/pulls/7/comments?page=2>; rel="next"'
                        )
                    },
                )
            if page == "2":
                return _json([parent])
            raise AssertionError(f"unexpected page: {page}")
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert gql["variables"]["inReplyTo"] == "PARENT_NODE_8200"
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": {"databaseId": 9202}}}}
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9202":
            return _json(reply)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8200",
    )
    assert result.id == "9202"


def test_reply_missing_comment_and_anchor_everywhere_still_reports_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 1 edge case (additional coverage — may already pass):
    when the requested comment 404s AND the fallback listing does not
    contain it either, `add_pr_review_comment` must still raise the same
    `"review comment 'X' not found"` message (byte-identical to today's
    contract) and must NOT create a pending review on the server."""
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/99999":
            return _json({"message": "Not Found"}, status_code=404)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/comments":
            return _json([])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(github_mod.GitHubError) as exc:
        GitHubProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="reply", in_reply_to="99999",
        )
    assert exc.value.status == 404
    assert "review comment" in exc.value.message
    assert "99999" in exc.value.message
    assert not any(
        r.method == "POST" and r.url.path == "/repos/acme/backend/pulls/7/reviews"
        for r in seen
    )


def test_new_thread_returns_comment_when_confirmation_lookup_404s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 2 driving test. A pending review already exists; the
    GraphQL `addPullRequestReviewThread` mutation succeeds and returns a
    comment node carrying no identity fields at all — just
    `databaseId: 9100` — the mutation payload itself is the only source
    of truth now (ticket #253 removed the post-write confirmation read
    entirely, so there is no fallback read left to stub). The call must
    NOT raise, and must synthesize the missing fields from the known
    call params.

    This is no longer a RED-producing case post-#253 (there is no
    confirmation read left to 404): it stays as coverage for the
    "mutation payload carries no identity fields" contract — the merge
    in `_review_comment_result` must fall back to the synthesized
    call-param values exactly like it does for a null REST field."""
    pending = _pending_review()

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
                            "thread": {"comments": {"nodes": [{"databaseId": 9100}]}}
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/foo.py",
        line=15,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9100"
    assert result.body == "#ai-generated\n\nnit"
    assert result.path == "src/foo.py"
    assert result.line == 15
    assert result.side == "RIGHT"
    assert result.url is None
    assert result.created_at == ""
    assert result.updated_at == ""


def test_reply_returns_comment_when_confirmation_lookup_404s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 2 edge case: same "mutation payload carries no identity
    fields" contract on the reply branch. The GraphQL reply mutation
    succeeds but its comment node carries only `databaseId: 9300` — no
    confirmation read is left to fall back on post-#253, so this pins
    the synthesized-fallback contract for the reply shape specifically.

    Ticket #241 fix-round finding 1: the reply branch already fetched
    the parent/anchor comment's raw payload (to resolve the GraphQL
    `node_id` for `inReplyTo`), which carries the thread's real
    `path`/`line`/`side`/`commit_id` — a reply necessarily lands at the
    same diff position as its parent. When the mutation payload doesn't
    supply those fields, those anchor-derived values must seed the
    synthesized fallback instead of being discarded in favour of `None`.
    The parent fixture (`_comment`'s defaults) has `path="src/foo.py"`,
    `line=10`, `side="RIGHT"`, `commit_id="abc123"` — assert the result
    inherits exactly those."""
    pending = _pending_review()
    parent = _comment(8300, node_id="PARENT_NODE_8300")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8300":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": {"databaseId": 9300}}}}
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8300",
    )
    assert result.id == "9300"
    assert result.in_reply_to == "8300"
    assert result.body == "#ai-generated\n\nreply text"
    assert result.url is None
    assert result.created_at == ""
    assert result.updated_at == ""
    assert result.path == "src/foo.py"
    assert result.line == 10
    assert result.side == "RIGHT"
    assert result.commit_sha == "abc123"


def test_reply_to_a_reply_seeds_in_reply_to_from_anchor_on_confirmation_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix-round 3 nit. Double-edge case: the caller replies to a
    MID-THREAD comment (8100, itself a reply to anchor 8050 — so
    `_review_thread_anchor_node_id` walks up), AND the post-write
    confirmation lookup for the new reply (9201) misses entirely (the
    direct GET 404s, and the paginated fallback listing finds no match
    either). The synthesized `in_reply_to` must come from the resolved
    thread ANCHOR (8050), not the caller's original mid-thread argument
    (8100) — a reply's `in_reply_to` should always point at the true
    anchor, matching what `_map_review_comment`'s `discussion_id` rule
    already promises for read paths.

    RED reason (pre-fix): the synthesized fallback used the caller's
    raw `in_reply_to` argument (`"8100"`) unconditionally, so
    `result.in_reply_to == "8100"` instead of the true anchor `"8050"`.
    """
    pending = _pending_review()
    mid_reply = _comment(8100, node_id="MID_NODE_8100", in_reply_to_id=8050)
    anchor = _comment(8050, node_id="ANCHOR_NODE_8050")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8100":
            return _json(mid_reply)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8050":
            return _json(anchor)
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            assert gql["variables"]["inReplyTo"] == "ANCHOR_NODE_8050"
            return _json(
                {"data": {"addPullRequestReviewComment": {"comment": {"databaseId": 9201}}}}
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9201":
            # Confirmation lookup misses entirely: direct GET 404s...
            return _json({"message": "Not Found"}, status_code=404)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/comments":
            # ...and the paginated fallback scan finds no match either.
            assert dict(req.url.params).get("per_page") == "100"
            return _json([])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8100",
    )
    assert result.id == "9201"
    assert result.in_reply_to == "8050"


def test_new_thread_confirmation_rate_limit_error_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 2 edge case: the confirmation read can fail with a
    `RateLimitError` (403, not 404) rather than a 404 — the "never fail
    after a successful mutation" contract must not be narrowed to just
    `GitHubError(404)`. `RateLimitError` is a sibling of `GitHubError`
    under `ProviderError`, so a narrow `except GitHubError` would leak
    it.

    RED reason (today): nothing catches the confirmation read's
    exceptions at all, so the `RateLimitError` propagates straight out
    of `add_pr_review_comment` as the call's own failure."""
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {"comments": {"nodes": [{"databaseId": 9101}]}}
                        }
                    }
                }
            )
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/9101":
            return _json(
                {"message": "rate limited"},
                status_code=403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"},
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/foo.py",
        line=16,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9101"
    assert result.path == "src/foo.py"
    assert result.line == 16


def test_seeded_create_returns_empty_id_when_confirmation_lookup_misses_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 2 edge case, pins the A2a documented tradeoff: on the
    seeded-create branch (no pending review yet), when the confirmation
    listing `GET /pulls/{n}/reviews/{review_id}/comments` comes back
    empty (`[]`) — the created review genuinely has no comment attached
    yet — the call must still return a `ReviewComment`, not raise. Its
    `id` is the best available value: `""` (never the *review* id, which
    would be actively harmful if later used as `in_reply_to`). All other
    fields (path/line/side/body) must still be filled from the known
    call params.

    RED reason (today): `_latest_pending_review_comment` does
    `r.json()[-1]` on an empty list, raising `IndexError` straight out
    of `add_pr_review_comment`."""
    created_review = _pending_review(review_id=950, node_id="REVIEW_NODE_950")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json(created_review)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/950/comments":
            return _json([])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/foo.py",
        line=17,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == ""
    assert result.path == "src/foo.py"
    assert result.line == 17
    assert result.side == "RIGHT"
    assert "nit" in result.body


def test_new_thread_fills_line_and_side_when_pending_payload_omits_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 3 driving test. Seeded-create branch: the confirmation
    payload (a *pending*-review comment) omits `line`/`original_line`/
    `side` (`None`) — GitHub does not resolve those on a still-pending
    comment. The immediate response must fill them from the known call
    params (`line=1`, `side="RIGHT"`) instead of surfacing `null`.
    Server-provided identity fields (id/created_at/html_url) must still
    be preserved from the confirmation payload.

    RED reason (today): `_map_review_comment` maps `raw.get("line")` /
    `raw.get("original_line")` / `raw.get("side")` straight through as
    `None` — the nulls pass through unfilled."""
    created_review = _pending_review(review_id=960, node_id="REVIEW_NODE_960")
    pending_comment = _comment(
        9600,
        path="src/foo.py",
        line=None,
        side=None,
        original_line=None,
        created_at="2024-02-02T00:00:00Z",
        html_url="https://github.com/acme/backend/pull/7#discussion_r9600",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json(created_review)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/960/comments":
            return _json([pending_comment])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/foo.py",
        line=1,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9600"
    assert result.line == 1
    assert result.original_line == 1
    assert result.side == "RIGHT"
    assert result.created_at == "2024-02-02T00:00:00Z"
    assert result.url == "https://github.com/acme/backend/pull/7#discussion_r9600"


def test_new_thread_server_resolved_line_wins_over_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour 3 edge case: when the confirmation payload DOES carry a
    non-null `line` (the server resolved it, e.g. to `2`) that differs
    from the caller's requested `line` (`1`), the server's value must
    win — `_review_comment_result` only fills in the synthesized default
    when the server payload is null/missing for that field, it never
    overrides a genuine non-null server value."""
    created_review = _pending_review(review_id=970, node_id="REVIEW_NODE_970")
    pending_comment = _comment(
        9700,
        path="src/foo.py",
        line=2,
        side="RIGHT",
        original_line=2,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json(created_review)
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/970/comments":
            return _json([pending_comment])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/foo.py",
        line=1,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9700"
    assert result.line == 2
    assert result.original_line == 2


# ---------- ticket #253: identity fields straight from the mutation --------
#
# `_lookup_review_comment_safe`'s post-write confirmation read can miss for
# a comment still inside an unsubmitted PENDING review — GitHub's single-
# comment REST endpoint doesn't serve those. When it misses, today's code
# swallows the miss (by design, see `_lookup_review_comment_safe`'s
# docstring) and falls back to the synthesized defaults, which have no
# `author`/`created_at`/`updated_at`/`url` at all. The fix asks both GraphQL
# mutations for those fields directly in their selection sets and reads them
# out of the mutation response itself, removing the need for the
# confirmation read. These tests stub the mutation response WITHOUT a
# `/pulls/comments/{id}` branch, so any confirmation GET the current code
# still issues either hits the file's `raise AssertionError("unexpected
# request…")` (swallowed by `_lookup_review_comment_safe`'s broad `except
# Exception`) or is caught directly by the `not any(...)` assertion below —
# either way, the identity fields stay empty today.


def test_reply_populates_author_timestamp_and_url_from_mutation_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 driving test: a reply whose GraphQL mutation response carries
    the identity fields must return them directly, with no post-write
    confirmation GET at all.

    RED reason (today): `_reply_in_pending_review`'s mutation selection
    set asks only for `comment{databaseId}}` — every other field on the
    mutation's response node is ignored. `add_pr_review_comment` then
    calls `_lookup_review_comment_safe(...)`, which DOES issue
    `GET /repos/acme/backend/pulls/comments/9003` (recorded in `seen`)
    even though this handler has no branch for it; the handler's
    `raise AssertionError("unexpected request…")` is swallowed by that
    helper's deliberately broad `except Exception` (best-effort, "never
    fail after a successful mutation"), so the call does not raise —
    it silently returns `author == ""`, `created_at == ""`, `url is
    None` instead of the mutation's values, and the `not any(...)`
    assertion below fails because that confirmation GET did happen.

    Test-critic F1/F2 (ticket #253 dispatch): the query-shape check below
    asserts on the selection-set fragment itself (`"author{login}"` /
    `"originalLine"` / `"diffSide"` / `"replyTo{databaseId}"` /
    `"commit{oid}"`), not a bare `"line" in query` substring scan — `line`
    already appears as a bare input-argument name (`line:$line` doesn't
    apply to this mutation, but `diffSide`/`originalLine`/etc. as bare
    words could still coincidentally match elsewhere, so the assertion
    targets text that can only appear inside the selection set). The
    mutation node's `path`/`diffSide`/`commit.oid`/`replyTo.databaseId`
    are also deliberately set to values that DIFFER from the anchor
    comment's (`path="src/foo.py"`, `side="RIGHT"`, `commit_id="abc123"`,
    own id `8001`) — proving these come from the mutation payload, not
    silently falling through to the anchor/call-param seed the way a
    partial implementation (one that dropped these keys from
    `_graphql_comment_raw`) would.
    """
    pending = _pending_review()
    parent = _comment(8001, node_id="PARENT_NODE_8001")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8001":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            query = gql["query"]
            assert "addPullRequestReviewComment" in query
            for fragment in (
                "author{login}", "originalLine", "diffSide",
                "replyTo{databaseId}", "commit{oid}", "createdAt", "updatedAt", "url",
            ):
                assert fragment in query, f"mutation query missing {fragment!r}: {query}"
            return _json(
                {
                    "data": {
                        "addPullRequestReviewComment": {
                            "comment": {
                                "databaseId": 9003,
                                "author": {"login": "reviewer"},
                                "body": "#ai-generated\n\nreply text",
                                # Deliberately diverges from the anchor
                                # comment's path/side/commit (all
                                # "src/foo.py"/"RIGHT"/"abc123") and from
                                # the anchor's own id (8001) — mutation
                                # values must win, not the anchor seed.
                                "path": "src/mutation-path.py",
                                "line": 10,
                                "originalLine": 10,
                                "diffSide": "LEFT",
                                "commit": {"oid": "mutation-sha"},
                                "replyTo": {"databaseId": 9999},
                                "createdAt": "2024-03-03T00:00:00Z",
                                "updatedAt": "2024-03-03T00:00:00Z",
                                "url": "https://github.com/acme/backend/pull/7#discussion_r9003",
                            }
                        }
                    }
                }
            )
        # Deliberately no `/pulls/comments/9003` branch — see module note.
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8001",
    )
    assert result.id == "9003"
    assert result.author == "reviewer"
    assert result.created_at == "2024-03-03T00:00:00Z"
    assert result.updated_at == "2024-03-03T00:00:00Z"
    assert result.url == "https://github.com/acme/backend/pull/7#discussion_r9003"
    # Mutation payload wins over the anchor/call-param seed (test-critic F2).
    assert result.path == "src/mutation-path.py"
    assert result.line == 10
    assert result.side == "LEFT"
    assert result.commit_sha == "mutation-sha"
    assert result.in_reply_to == "9999"
    assert result.discussion_id == "9999"
    # R2: no post-write single-comment GET at all.
    assert not any(
        r.method == "GET" and r.url.path == "/repos/acme/backend/pulls/comments/9003"
        for r in seen
    )


def test_new_thread_populates_author_timestamp_and_url_from_mutation_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1(a) — the shared second call site: a new-thread comment added to
    an EXISTING pending review (`_add_thread_to_pending_review`,
    `addPullRequestReviewThread`) must get the same treatment. Also pins
    `discussion_id` for a new thread: the comment's OWN id (9002), not an
    anchor — there is no anchor, this starts a new thread.

    RED reason (today): same shape as the reply test above —
    `_add_thread_to_pending_review`'s selection set asks only for
    `thread{comments(first:1){nodes{databaseId}}}}`, so
    `_lookup_review_comment_safe`'s confirmation GET (unstubbed here) is
    swallowed and the identity fields stay empty/None.

    Test-critic F1/F2 (ticket #253 dispatch): the query-shape check
    targets the selection-set fragment itself, not a bare `"line" in
    query` scan (see the reply test's docstring for why). The mutation
    node's `path`/`diffSide`/`commit.oid` also deliberately DIFFER from
    the caller's own params (`path="src/bar.py"`, `side="RIGHT"`,
    `commit_sha="abc123"`), proving the mutation payload — not the
    call-param seed — is what the result reflects.
    """
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            gql = _body(req)
            query = gql["query"]
            assert "addPullRequestReviewThread" in query
            for fragment in (
                "author{login}", "originalLine", "diffSide",
                "replyTo{databaseId}", "commit{oid}", "createdAt", "updatedAt", "url",
            ):
                assert fragment in query, f"mutation query missing {fragment!r}: {query}"
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 9002,
                                            "author": {"login": "reviewer"},
                                            "body": "#ai-generated\n\nsecond nit",
                                            # Deliberately diverges from
                                            # the caller's own
                                            # path/side/commit_sha params
                                            # ("src/bar.py"/"RIGHT"/
                                            # "abc123") — mutation values
                                            # must win, not the call-param
                                            # seed.
                                            "path": "src/mutation-bar.py",
                                            "line": 20,
                                            "originalLine": 20,
                                            "diffSide": "LEFT",
                                            "commit": {"oid": "mutation-thread-sha"},
                                            "replyTo": None,
                                            "createdAt": "2024-03-03T00:00:00Z",
                                            "updatedAt": "2024-03-03T00:00:00Z",
                                            "url": "https://github.com/acme/backend/pull/7#discussion_r9002",
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        # Deliberately no `/pulls/comments/9002` branch — see module note.
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
    assert result.author == "reviewer"
    assert result.created_at == "2024-03-03T00:00:00Z"
    assert result.updated_at == "2024-03-03T00:00:00Z"
    assert result.url == "https://github.com/acme/backend/pull/7#discussion_r9002"
    assert result.discussion_id == "9002"
    # Mutation payload wins over the call-param seed (test-critic F2).
    assert result.path == "src/mutation-bar.py"
    assert result.side == "LEFT"
    assert result.commit_sha == "mutation-thread-sha"
    assert not any(
        r.method == "GET" and r.url.path == "/repos/acme/backend/pulls/comments/9002"
        for r in seen
    )


def test_reply_populates_empty_author_when_mutation_author_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1(b): a mutation response with `author: null` (GitHub can omit
    the author, e.g. a deleted/anonymized account) must not crash — it
    maps to `author == ""`, same convention as every other
    `_map_review_comment` caller, while `created_at`/`updated_at`/`url`
    are still populated from the same payload.

    RED reason (today): identical swallow-and-synthesize path as the
    main driving test — `created_at`/`updated_at`/`url` come back empty/
    None regardless of `author`, so the non-author assertions fail.
    """
    pending = _pending_review()
    parent = _comment(8002, node_id="PARENT_NODE_8002")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/comments/8002":
            return _json(parent)
        if req.method == "POST" and path == "/graphql":
            return _json(
                {
                    "data": {
                        "addPullRequestReviewComment": {
                            "comment": {
                                "databaseId": 9004,
                                "author": None,
                                "path": "src/foo.py",
                                "line": 10,
                                "originalLine": 10,
                                "diffSide": "RIGHT",
                                "commit": {"oid": "abc123"},
                                "replyTo": {"databaseId": 8002},
                                "createdAt": "2024-03-03T00:00:00Z",
                                "updatedAt": "2024-03-03T00:00:00Z",
                                "url": "https://github.com/acme/backend/pull/7#discussion_r9004",
                            }
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="reply text", in_reply_to="8002",
    )
    assert result.id == "9004"
    assert result.author == ""
    assert result.created_at == "2024-03-03T00:00:00Z"
    assert result.updated_at == "2024-03-03T00:00:00Z"
    assert result.url == "https://github.com/acme/backend/pull/7#discussion_r9004"


def test_new_thread_mutation_resolved_line_wins_over_requested_no_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1(c): mirrors `test_new_thread_server_resolved_line_wins_over_requested`'s
    "server value wins" contract onto the mutation-derived raw dict, with
    no confirmation GET involved at all — a mutation response
    `line`/`originalLine` that differs from the caller's requested value
    must still win.

    RED reason (today): the mutation response's `line`/`originalLine`
    are ignored entirely (only `databaseId` is parsed today); with no
    `/pulls/comments/9005` stub, the confirmation lookup's exception is
    swallowed and the result falls back to the synthesized (requested)
    `line=1`, not the server's `2`.
    """
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 9005,
                                            "line": 2,
                                            "originalLine": 2,
                                            "diffSide": "RIGHT",
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/bar.py",
        line=1,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9005"
    assert result.line == 2
    assert result.original_line == 2
    assert not any(
        r.method == "GET" and r.url.path == "/repos/acme/backend/pulls/comments/9005"
        for r in seen
    )


def test_new_thread_mutation_null_line_falls_back_to_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1(c), second half: a mutation response with `line: null` (server
    didn't resolve it) must fall back to the caller's requested `line`,
    exactly like the confirmation-read path already does.

    Note: this assertion already passes today (both before and after the
    fix, `line` ends up `1` either way — today via the synthesized
    fallback after the swallowed confirmation miss, post-fix via the
    non-null merge rule on a null mutation field) — recorded here as
    edge-case coverage for the merge contract, not as a RED-producing
    driving test.
    """
    pending = _pending_review()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/graphql":
            return _json(
                {
                    "data": {
                        "addPullRequestReviewThread": {
                            "thread": {
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 9006,
                                            "line": None,
                                            "originalLine": None,
                                            "diffSide": None,
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="nit",
        path="src/bar.py",
        line=1,
        side="RIGHT",
        commit_sha="abc123",
    )
    assert result.id == "9006"
    assert result.line == 1
    assert result.side == "RIGHT"


# ---------- pure unit tests of `_review_comment_result` (no HTTP) -----------


def test_review_comment_result_prefers_server_non_null_over_synthesized() -> None:
    """`_review_comment_result` is a pure function: given a server `raw`
    payload with non-null values, those values must win over the
    synthesized defaults built from the call params — across fields in
    general, not only the ones already covered by the HTTP-level tests
    above."""
    raw = {
        "id": 4242,
        "body": "server body",
        "path": "server/path.py",
        "line": 99,
        "original_line": 98,
        "side": "LEFT",
        "commit_id": "server-sha",
        "in_reply_to_id": None,
        "created_at": "2024-03-03T00:00:00Z",
        "updated_at": "2024-03-04T00:00:00Z",
        "html_url": "https://github.com/acme/backend/pull/7#discussion_r4242",
        "user": {"login": "reviewer"},
    }

    result = github_mod._review_comment_result(
        comment_id="123",
        raw=raw,
        body="synthesized body",
        path="synth/path.py",
        line=1,
        side="RIGHT",
        commit_sha="synth-sha",
        in_reply_to=None,
    )

    assert result.id == "4242"
    assert result.body == "server body"
    assert result.path == "server/path.py"
    assert result.line == 99
    assert result.original_line == 98
    assert result.side == "LEFT"
    assert result.commit_sha == "server-sha"
    assert result.created_at == "2024-03-03T00:00:00Z"
    assert result.updated_at == "2024-03-04T00:00:00Z"
    assert result.url == "https://github.com/acme/backend/pull/7#discussion_r4242"
    assert result.author == "reviewer"


def test_review_comment_result_falls_back_to_synthesized_when_raw_empty() -> None:
    """Pure unit test: an empty `raw` (confirmation lookup missed
    entirely) must fall back to every synthesized field, byte-identical
    to the call params."""
    result = github_mod._review_comment_result(
        comment_id="123",
        raw={},
        body="synthesized body",
        path="synth/path.py",
        line=1,
        side="RIGHT",
        commit_sha="synth-sha",
        in_reply_to="999",
    )

    assert result.id == "123"
    assert result.body == "synthesized body"
    assert result.path == "synth/path.py"
    assert result.line == 1
    assert result.original_line == 1
    assert result.side == "RIGHT"
    assert result.commit_sha == "synth-sha"
    assert result.in_reply_to == "999"
    assert result.created_at == ""
    assert result.updated_at == ""
    assert result.url is None
