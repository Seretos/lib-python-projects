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


# ---------- Ticket #205: submit_pr_review submits a pending review -----------


def test_submit_pr_review_submits_existing_pending_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4: when the caller already has a PENDING review on the PR (left
    by prior `add_pr_review_comment` calls), `submit_pr_review` submits
    *that* review via `POST /pulls/{n}/reviews/{id}/events` instead of
    creating a new one — GitHub rejects creating a second review while
    one is still pending for the same author."""
    pending = {
        "id": 900,
        "node_id": "REVIEW_NODE_900",
        "state": "PENDING",
        "user": {"login": "me"},
        "body": "",
        "html_url": "",
        "submitted_at": None,
        "commit_id": "abc123",
    }
    submitted = {
        **pending,
        "state": "COMMENTED",
        "body": "lgtm",
        "submitted_at": "2024-01-02T00:00:00Z",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews/900/events":
            payload = json.loads(req.content or b"{}")
            assert payload["event"] == "COMMENT"
            assert payload["body"]
            return _json(submitted)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    review = GitHubProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="comment", body="lgtm",
    )
    assert review.id == "900"
    assert review.state == "comment"
    assert not any(
        r.method == "POST" and r.url.path == "/repos/acme/backend/pulls/7/reviews"
        for r in seen
    )


def test_submit_pr_review_creates_new_review_when_none_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4: with no PENDING review on the PR, `submit_pr_review` falls
    back to the original create-and-submit path,
    `POST /pulls/{n}/reviews`."""
    created = _review(901, "APPROVED")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            payload = json.loads(req.content or b"{}")
            assert payload["event"] == "APPROVE"
            return _json(created)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    review = GitHubProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="approve",
    )
    assert review.id == "901"
    assert review.state == "approve"


# ---------- Ticket #205, fix-round 3, finding 3: commit_sha contract ---------


def test_submit_pr_review_rejects_mismatched_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `add_pr_review_comment`'s same-commit contract: once a
    pending review is found, a `commit_sha` that does not match the
    pending review's own commit must raise `ValueError` rather than
    being silently accepted — otherwise a review could be submitted
    against a different commit than the pending review's original one.
    Must fail fast: no submit POST may be sent."""
    pending = {
        "id": 900,
        "node_id": "REVIEW_NODE_900",
        "state": "PENDING",
        "user": {"login": "me"},
        "body": "",
        "html_url": "",
        "submitted_at": None,
        "commit_id": "abc123",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        raise AssertionError(f"unexpected request: {req.method} {path}")

    seen = _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="commit_sha"):
        GitHubProvider().submit_pr_review(
            _project(),
            token="t",
            pr_id="7",
            state="comment",
            body="lgtm",
            commit_sha="different-sha",
        )
    assert not any(
        r.method == "POST" and r.url.path.startswith("/repos/acme/backend/pulls/7/reviews")
        for r in seen
    )


def test_submit_pr_review_accepts_matching_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: passing the SAME commit_sha as the pending
    review's own commit still submits normally (no false-positive
    rejection)."""
    pending = {
        "id": 900,
        "node_id": "REVIEW_NODE_900",
        "state": "PENDING",
        "user": {"login": "me"},
        "body": "",
        "html_url": "",
        "submitted_at": None,
        "commit_id": "abc123",
    }
    submitted = {
        **pending,
        "state": "COMMENTED",
        "body": "lgtm",
        "submitted_at": "2024-01-02T00:00:00Z",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([pending])
        if req.method == "POST" and path == "/repos/acme/backend/pulls/7/reviews/900/events":
            return _json(submitted)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    review = GitHubProvider().submit_pr_review(
        _project(),
        token="t",
        pr_id="7",
        state="comment",
        body="lgtm",
        commit_sha="abc123",
    )
    assert review.id == "900"


# ---------- Ticket #205: end-to-end acceptance (R5) --------------------------


def test_pending_review_flow_end_to_end_yields_one_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 acceptance: two `add_pr_review_comment` calls followed by one
    `submit_pr_review` must result in exactly one entry in both
    `list_pr_reviews()` and `get_pr().reviews` — not two orphaned
    auto-submitted reviews (the ticket #205 defect), and not zero (the
    comments must actually land somewhere, via the shared pending
    review)."""
    _EVENT_TO_STATE = {
        "APPROVE": "APPROVED",
        "REQUEST_CHANGES": "CHANGES_REQUESTED",
        "COMMENT": "COMMENTED",
    }
    state: dict = {"review": None, "comments": [], "next_comment_id": 9000}

    def _comment_dict(cid: int, body: str, path: str, line: int) -> dict:
        return {
            "id": cid,
            "node_id": f"COMMENT_NODE_{cid}",
            "user": {"login": "me"},
            "body": body,
            "path": path,
            "line": line,
            "side": "RIGHT",
            "commit_id": "abc123",
            "in_reply_to_id": None,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "html_url": f"https://github.com/acme/backend/pull/7#discussion_r{cid}",
        }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method

        if method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([state["review"]] if state["review"] else [])

        if method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            payload = json.loads(req.content or b"{}")
            assert state["review"] is None, "only one review should ever be created"
            state["review"] = {
                "id": 900,
                "node_id": "REVIEW_NODE_900",
                "state": "PENDING",
                "user": {"login": "me"},
                "body": "",
                "html_url": "https://github.com/acme/backend/pull/7#pullrequestreview-900",
                "submitted_at": None,
                "commit_id": "abc123",
            }
            for c in payload.get("comments", []):
                cid = state["next_comment_id"]
                state["next_comment_id"] += 1
                state["comments"].append(_comment_dict(cid, c["body"], c["path"], c["line"]))
            return _json(state["review"])

        if method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/900/comments":
            return _json(state["comments"])

        if method == "POST" and path == "/graphql":
            gql = json.loads(req.content or b"{}")
            if "addPullRequestReviewThread" in gql["query"]:
                v = gql["variables"]
                cid = state["next_comment_id"]
                state["next_comment_id"] += 1
                state["comments"].append(_comment_dict(cid, v["body"], v["path"], v["line"]))
                return _json(
                    {
                        "data": {
                            "addPullRequestReviewThread": {
                                "thread": {"comments": {"nodes": [{"databaseId": cid}]}}
                            }
                        }
                    }
                )
            raise AssertionError(f"unexpected graphql query: {gql['query']}")

        if method == "GET" and path.startswith("/repos/acme/backend/pulls/comments/"):
            wanted = int(path.rsplit("/", 1)[-1])
            match = next(c for c in state["comments"] if c["id"] == wanted)
            return _json(match)

        if method == "POST" and path == "/repos/acme/backend/pulls/7/reviews/900/events":
            payload = json.loads(req.content or b"{}")
            state["review"]["state"] = _EVENT_TO_STATE[payload["event"]]
            state["review"]["body"] = payload.get("body", "")
            state["review"]["submitted_at"] = "2024-01-02T00:00:00Z"
            return _json(state["review"])

        if method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(_pr_payload())

        if method == "GET" and path == "/repos/acme/backend/issues/7/comments":
            return _json([])

        raise AssertionError(f"unexpected request: {method} {path}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()

    provider.add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="first nit",
        path="src/foo.py",
        line=1,
        side="RIGHT",
        commit_sha="abc123",
    )
    provider.add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="second nit",
        path="src/foo.py",
        line=2,
        side="RIGHT",
        commit_sha="abc123",
    )
    provider.submit_pr_review(
        _project(), token="t", pr_id="7", state="comment", body="done",
    )

    reviews = provider.list_pr_reviews(_project(), token="t", pr_id="7")
    assert len(reviews) == 1
    assert reviews[0].state == "comment"

    pr, _ = provider.get_pr(_project(), token="t", pr_id="7")
    assert len(pr.reviews) == 1
    assert pr.reviews[0].state == "comment"


# ---------- Ticket #212 -------------------------------------------------------


def test_add_pr_review_comment_pending_review_does_not_leak_into_get_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard (ticket #212): the E2E sweep believed a new-thread
    `add_pr_review_comment` call created a phantom empty-body 'comment'
    review entry in `get_pr().reviews`. Investigation found this was
    already fixed by the #205 wave (new-thread comments route through an
    unsubmitted PENDING review; `_map_review`/`_fetch_pr_reviews` already
    filter `PENDING` out of `reviews[]`). This test locks that already-
    correct behaviour in as a regression guard — it must PASS unmodified
    against current `_map_review`/`_fetch_pr_reviews`/
    `add_pr_review_comment`, and stops BEFORE `submit_pr_review` (unlike
    `test_pending_review_flow_end_to_end_yields_one_review`, which only
    asserts the post-submission state)."""
    state: dict = {"review": None, "comments": []}

    def _comment_dict(cid: int, body: str, path: str, line: int) -> dict:
        return {
            "id": cid,
            "node_id": f"COMMENT_NODE_{cid}",
            "user": {"login": "me"},
            "body": body,
            "path": path,
            "line": line,
            "side": "RIGHT",
            "commit_id": "abc123",
            "in_reply_to_id": None,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "html_url": f"https://github.com/acme/backend/pull/7#discussion_r{cid}",
        }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method

        if method == "GET" and path == "/repos/acme/backend/pulls/7/reviews":
            return _json([state["review"]] if state["review"] else [])

        if method == "POST" and path == "/repos/acme/backend/pulls/7/reviews":
            payload = json.loads(req.content or b"{}")
            assert state["review"] is None, "only one review should ever be created"
            state["review"] = {
                "id": 900,
                "node_id": "REVIEW_NODE_900",
                "state": "PENDING",
                "user": {"login": "me"},
                "body": "",
                "html_url": "https://github.com/acme/backend/pull/7#pullrequestreview-900",
                "submitted_at": None,
                "commit_id": "abc123",
            }
            for c in payload.get("comments", []):
                state["comments"].append(
                    _comment_dict(9001, c["body"], c["path"], c["line"])
                )
            return _json(state["review"])

        if method == "GET" and path == "/repos/acme/backend/pulls/7/reviews/900/comments":
            return _json(state["comments"])

        if method == "GET" and path == "/repos/acme/backend/pulls/7/comments":
            # Real GitHub behaviour: a pending review's inline comments are
            # visible to the review's own author via this endpoint right
            # away — only *other* users can't see them until submission.
            # Our mock always answers "as" the comment's own author, so it
            # must return them here too (ticket #212 finding #2).
            return _json(state["comments"])

        if method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(_pr_payload())

        if method == "GET" and path == "/repos/acme/backend/issues/7/comments":
            return _json([])

        raise AssertionError(f"unexpected request: {method} {path}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()

    created_comment = provider.add_pr_review_comment(
        _project(),
        token="t",
        pr_id="7",
        body="pending nit",
        path="src/foo.py",
        line=1,
        side="RIGHT",
        commit_sha="abc123",
    )
    # The comment itself is created and returned (with the project's
    # AI-generated marker prefix applied) — it's just not visible in the
    # review-level surfaces below until submitted.
    assert "pending nit" in created_comment.body

    reviews = provider.list_pr_reviews(_project(), token="t", pr_id="7")
    assert reviews == [], "an unsubmitted PENDING review must not leak into list_pr_reviews()"

    pr, _ = provider.get_pr(_project(), token="t", pr_id="7")
    assert pr.reviews == [], "an unsubmitted PENDING review must not leak into get_pr().reviews"

    # The comment itself is NOT invisible everywhere: it is readable back
    # via list_pr_review_comments() (the review_comments path) even though
    # the review that carries it is still excluded from reviews[] above —
    # that's the real GitHub behaviour this regression guard locks in.
    review_comments = provider.list_pr_review_comments(_project(), token="t", pr_id="7")
    assert any("pending nit" in rc.body for rc in review_comments), (
        "the pending review's inline comment must be visible via "
        "list_pr_review_comments() even though its review is absent "
        "from reviews[]"
    )
