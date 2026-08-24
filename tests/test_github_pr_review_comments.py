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
