"""Tests for WP #241 (bundles #233 + #234) on the GitLab provider.

GitLab has no native "review" resource — `request_changes`/`comment`
reviews are posted as plain notes, indistinguishable from ordinary MR
comments once posted, and `submit_pr_review`'s own writes did not stamp
any marker to make them recoverable later. This left three surfaces
broken:

  - `submit_pr_review(request_changes|comment|approve-with-body)` wrote a
    bare `#ai-generated` body with no review-state marker (finding 2).
  - `get_pr().reviews` / `list_pr_reviews()` / `merge_pr(...).reviews`
    only ever surfaced approvals-derived `Review` objects — a
    `request_changes` review vanished the moment it was posted (finding
    3).
  - A bare `approve` (no note) returned `Review.id`/`submitted_at` from
    the MR root instead of the acting user (finding 4).

Covers (design points from the plan):
  1. `submit_pr_review` writes a fixed `#ai-review-<state>` marker line
     for all three review states (never derived from `auto_labels`).
  2. Bare-approve `Review` shape uses the authenticated user's id/empty
     submitted_at; note-posting branches return the full marked body.
  3/4. Marked notes are reconstructed into `Review` objects on all three
     read surfaces (`get_pr`, `list_pr_reviews`, `merge_pr`) and merged
     with the live approvals-derived entries without duplicating a
     same-state event from the same author.
  5. `review_decision` is overridden to `CHANGES_REQUESTED` when an
     author's most recent marked state is `request_changes` and they are
     not currently a live approver.
  6. Marked notes never leak into `comments[]`.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import AutoLabels, ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.gitlab import GitLabProvider


def _project(**kwargs) -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="gitlab", path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
        **kwargs,
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

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "test"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(
            base_url=gitlab_mod._base_url(project),
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _json_page(payload, next_page: str = "") -> httpx.Response:
    """Like `_json` but sets `X-Next-Page`, GitLab's pagination header,
    so a handler can simulate a multi-page notes listing."""
    headers = {"Content-Type": "application/json"}
    if next_page:
        headers["X-Next-Page"] = next_page
    return httpx.Response(
        status_code=200,
        content=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content or b"{}")


def _mr_payload(iid: int, **overrides) -> dict:
    base = {
        "iid": iid,
        "title": f"MR {iid}",
        "description": "body",
        "state": "opened",
        "draft": False,
        "author": {"username": "alice"},
        "assignees": [],
        "reviewers": [],
        "labels": [],
        "source_branch": "feat/x",
        "target_branch": "main",
        "sha": "abc123",
        "web_url": f"https://gitlab.com/acme/backend/-/merge_requests/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "detailed_merge_status": "mergeable",
    }
    base.update(overrides)
    return base


def _note(
    note_id: int,
    body: str,
    author: str = "alice",
    created_at: str = "2024-03-01T00:00:00Z",
    system: bool = False,
    position: dict | None = None,
) -> dict:
    d = {
        "id": note_id,
        "body": body,
        "author": {"username": author},
        "created_at": created_at,
        "updated_at": created_at,
        "system": system,
    }
    if position is not None:
        d["position"] = position
    return d


_MARK_RC = "#ai-generated\n#ai-review-request-changes\n\nPlease fix X"
_MARK_APPROVE = "#ai-generated\n#ai-review-approve\n\nLGTM"
_MARK_COMMENT = "#ai-generated\n#ai-review-comment\n\nJust a note"


# =============================================================================
# Behaviour 4 (#234 finding 2): submit_pr_review writes a review-state
# marker line for all three states.
# =============================================================================


def test_submit_pr_review_request_changes_body_carries_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. The note POSTed by a `request_changes` review must
    start with `"#ai-generated\\n#ai-review-request-changes\\n\\n"` and
    still contain the caller's own text.

    RED reason (today): the posted body is bare
    `ensure_comment_prefix(body)` — `"#ai-generated\\n\\n<body>"` — with
    no review-state marker line at all."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/unapprove"):
            return _json({}, status_code=404)
        if req.method == "POST" and url.endswith("/notes"):
            captured["body"] = _body(req)["body"]
            return _json(_note(1, captured["body"]))
        raise AssertionError(f"unexpected request: {req.method} {url}")

    _install_mock(monkeypatch, handler)
    GitLabProvider().submit_pr_review(
        _project(), "t", "10", state="request_changes", body="Please fix the thing",
    )
    assert captured["body"].startswith(
        "#ai-generated\n#ai-review-request-changes\n\n"
    )
    assert "Please fix the thing" in captured["body"]


def test_submit_pr_review_approve_with_body_carries_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: an `approve` review WITH a body posts a note too, and
    that note must carry the `#ai-review-approve` marker.

    RED reason (today): the note body is bare `ensure_comment_prefix`,
    no review marker."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/approve"):
            return _json({"iid": 10, "updated_at": "2024-01-01T00:00:00Z"})
        if req.method == "POST" and url.endswith("/notes"):
            captured["body"] = _body(req)["body"]
            return _json(_note(2, captured["body"]))
        raise AssertionError(f"unexpected request: {req.method} {url}")

    _install_mock(monkeypatch, handler)
    GitLabProvider().submit_pr_review(
        _project(), "t", "10", state="approve", body="LGTM",
    )
    assert captured["body"].startswith("#ai-generated\n#ai-review-approve\n\n")
    assert "LGTM" in captured["body"]


def test_submit_pr_review_comment_state_carries_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: `state="comment"` posts a note carrying
    `#ai-review-comment`.

    RED reason (today): bare `ensure_comment_prefix`, no review
    marker."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/notes"):
            captured["body"] = _body(req)["body"]
            return _json(_note(3, captured["body"]))
        raise AssertionError(f"unexpected request: {req.method} {url}")

    _install_mock(monkeypatch, handler)
    GitLabProvider().submit_pr_review(
        _project(), "t", "10", state="comment", body="Just a note",
    )
    assert captured["body"].startswith("#ai-generated\n#ai-review-comment\n\n")
    assert "Just a note" in captured["body"]


def test_submit_pr_review_bare_approve_posts_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage (already passes — unaffected by this fix): a
    bare `approve` (no body) posts NO note at all, since there is
    nothing to mark. Verified precondition from the plan: `submit_pr_review`
    already raises `ValueError` for `state in ("comment", "request_changes")`
    with a falsy body, so a comment/request_changes review always posts a
    note — bare-approve is the only marker-less path."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/approve"):
            return _json({"iid": 10, "updated_at": "2024-01-01T00:00:00Z"})
        if req.method == "GET" and url.endswith("/user"):
            return _json({"id": 1, "username": "alice"})
        raise AssertionError(f"unexpected request: {req.method} {url}")

    seen = _install_mock(monkeypatch, handler)
    GitLabProvider().submit_pr_review(_project(), "t", "10", state="approve")
    assert not any(r.method == "POST" and str(r.url).endswith("/notes") for r in seen)


def test_submit_pr_review_custom_auto_labels_still_uses_fixed_marker_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: a project with custom `auto_labels` still gets the
    FIXED `#ai-review-request-changes` literal (never derived from
    `auto_labels`) — only the generated/modified prefix name is
    project-configurable.

    RED reason (today): no review marker is written at all, regardless
    of `auto_labels`."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/unapprove"):
            return _json({}, status_code=404)
        if req.method == "POST" and url.endswith("/notes"):
            captured["body"] = _body(req)["body"]
            return _json(_note(4, captured["body"]))
        raise AssertionError(f"unexpected request: {req.method} {url}")

    _install_mock(monkeypatch, handler)
    project = _project(
        auto_labels=AutoLabels(ai_generated="custom-gen", ai_modified="custom-mod"),
    )
    GitLabProvider().submit_pr_review(
        project, "t", "10", state="request_changes", body="text",
    )
    assert captured["body"].startswith(
        "#custom-gen\n#ai-review-request-changes\n\n"
    )


# =============================================================================
# Behaviour 8 (design point 2 + finding 4): bare-approve Review shape and
# the immediate Review.body contract.
# =============================================================================


def test_submit_pr_review_bare_approve_returns_user_id_and_empty_submitted_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. On a bare `approve` (no note), `Review.id` must be
    the acting user's id (from `GET /user`) and `submitted_at` must be
    `""` — not the MR's own `iid`/`updated_at`, which is what today's
    code substitutes because the `/approve` response (a
    *MergeRequestApproval* object) has no reliable per-call actor.

    RED reason (today): `id=str(mr_raw.get('iid', ''))` and
    `submitted_at=mr_raw.get('updated_at') or ''` — both come from the MR
    root instead of the resolved user."""
    user_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/approve"):
            return _json({"iid": 10, "updated_at": "2024-05-01T00:00:00Z"})
        if req.method == "GET" and url.endswith("/user"):
            user_calls.append(url)
            return _json({"id": 42, "username": "alice"})
        raise AssertionError(f"unexpected request: {req.method} {url}")

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(_project(), "t", "10", state="approve")
    assert review.id == "42"
    assert review.submitted_at == ""
    assert review.body is None
    assert len(user_calls) == 1


def test_submit_pr_review_returns_marked_body_verbatim_even_when_server_echo_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: on a note-posting branch, `Review.body` must be the
    full marked body (prefix + review marker + text) byte-identical to
    what was POSTed — even when the server's echo omits/empties the
    `body` field. Implementation must fall back to the locally
    constructed marked string, not trust the server echo blindly.

    RED reason (today): `body=note_raw.get('body', '')` trusts the
    (here, empty) server echo verbatim — and even when non-empty, no
    review marker is included at all."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/unapprove"):
            return _json({}, status_code=404)
        if req.method == "POST" and url.endswith("/notes"):
            # Server omits/empties the body echo.
            return _json(_note(5, "", author="alice"))
        raise AssertionError(f"unexpected request: {req.method} {url}")

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "10", state="request_changes", body="Please fix the thing",
    )
    assert review.body == (
        "#ai-generated\n#ai-review-request-changes\n\nPlease fix the thing"
    )


def test_submit_pr_review_with_note_branch_returns_actual_note_id_and_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test-critic note 7. On the WITH-NOTE branch (`request_changes`
    here), `Review.id` must equal the actual mocked note id and
    `Review.submitted_at` must equal that note's `created_at` — neither
    the MR iid nor the acting user's id. Only the bare-approve branch
    (no note) was previously pinned for id/submitted_at; this covers the
    note-posting branch's own id/timestamp sourcing.

    RED reason (today): before WP #241 the request_changes branch's
    `Review.id` already came from `note_raw.get('id')`, so this specific
    assertion happens to hold even pre-fix — it is included as coverage
    of the WITH-NOTE id/submitted_at contract that #234 finding 4's fix
    must not regress, alongside the bare-approve driving test above."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/unapprove"):
            return _json({}, status_code=404)
        if req.method == "POST" and url.endswith("/notes"):
            return _json(_note(
                777, "irrelevant-server-echo", author="alice",
                created_at="2024-06-15T10:20:30Z",
            ))
        raise AssertionError(f"unexpected request: {req.method} {url}")

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "10", state="request_changes", body="Please fix the thing",
    )
    assert review.id == "777"
    assert review.submitted_at == "2024-06-15T10:20:30Z"


# =============================================================================
# Behaviour 5 (design points 3 & 4): marked notes reappear as Reviews on
# get_pr / list_pr_reviews / merge_pr, merged with live approvals.
# =============================================================================


def test_get_pr_reconstructs_reviews_from_marked_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. Three marked notes (request_changes from alice,
    approve from bob, comment from carol), no live approvals. `pr.reviews`
    must surface all three, in chronological order, with the marker still
    verbatim in `body`.

    RED reason (today): `pr.reviews = _reviews_from_approvals(approvals)`
    only ever looks at the (here, empty) approvals payload — notes are
    never consulted, so `pr.reviews == []`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
                _note(2, _MARK_APPROVE, author="bob", created_at="2024-03-01T00:00:02Z"),
                _note(3, _MARK_COMMENT, author="carol", created_at="2024-03-01T00:00:03Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert [rv.author for rv in pr.reviews] == ["alice", "bob", "carol"]
    assert [rv.state for rv in pr.reviews] == ["request_changes", "approve", "comment"]
    assert pr.reviews[0].body == _MARK_RC
    assert "#ai-review-request-changes" in pr.reviews[0].body
    # Test-critic note 4: id/submitted_at/url of the reconstructed
    # reviews must be sourced from the note payload, not left blank —
    # not just author/state/body.
    assert pr.reviews[0].id == "1"
    assert pr.reviews[0].submitted_at == "2024-03-01T00:00:01Z"
    assert pr.reviews[0].url == (
        "https://gitlab.com/acme/backend/-/merge_requests/10#note_1"
    )
    assert pr.reviews[1].id == "2"
    assert pr.reviews[1].submitted_at == "2024-03-01T00:00:02Z"
    assert pr.reviews[1].url == (
        "https://gitlab.com/acme/backend/-/merge_requests/10#note_2"
    )
    assert pr.reviews[2].id == "3"
    assert pr.reviews[2].submitted_at == "2024-03-01T00:00:03Z"
    assert pr.reviews[2].url == (
        "https://gitlab.com/acme/backend/-/merge_requests/10#note_3"
    )


def test_get_pr_paginates_notes_across_multiple_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test (fix round 3, blocking finding). A marked review note
    that only appears on PAGE 2 of the notes listing must still show up
    in `pr.reviews` — `get_pr`'s notes fetch must follow `X-Next-Page`
    instead of stopping after the first `per_page=100` page.

    RED reason (pre-fix): the notes fetch was a single unpaginated
    `client.get(...)` call — page 2 (and the marked note it carries) was
    never requested at all, so `pr.reviews == []`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            page = req.url.params.get("page")
            if page == "2":
                return _json_page([
                    _note(
                        2, _MARK_RC, author="alice",
                        created_at="2024-03-01T00:00:02Z",
                    ),
                ])
            return _json_page([
                _note(
                    1, "an ordinary comment, not a marker", author="bob",
                    created_at="2024-03-01T00:00:01Z",
                ),
            ], next_page="2")
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert [rv.author for rv in pr.reviews] == ["alice"]
    assert [rv.state for rv in pr.reviews] == ["request_changes"]


def test_list_pr_reviews_paginates_notes_across_multiple_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage: `list_pr_reviews` (via `_fetch_mr_notes`) must
    walk pages the same way `get_pr` does — a page-2-only marked note
    must still surface.

    RED reason (pre-fix): `_fetch_mr_notes` stopped after page 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if "merge_requests/10/notes" in url:
            page = req.url.params.get("page")
            if page == "2":
                return _json_page([
                    _note(
                        2, _MARK_RC, author="alice",
                        created_at="2024-03-01T00:00:02Z",
                    ),
                ])
            return _json_page([
                _note(
                    1, "an ordinary comment, not a marker", author="bob",
                    created_at="2024-03-01T00:00:01Z",
                ),
            ], next_page="2")
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    reviews = GitLabProvider().list_pr_reviews(_project(), "t", "10")
    assert [rv.author for rv in reviews] == ["alice"]
    assert [rv.state for rv in reviews] == ["request_changes"]


def test_merge_pr_paginates_notes_across_multiple_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage: `merge_pr`'s post-merge notes fetch (also
    `_fetch_mr_notes`) must walk pages too — a page-2-only marked note
    must still surface in the merged PR's `reviews[]`.

    RED reason (pre-fix): `_fetch_mr_notes` stopped after page 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "PUT" and url.endswith("/merge"):
            return _json(_mr_payload(10, state="merged"))
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if "merge_requests/10/notes" in url:
            page = req.url.params.get("page")
            if page == "2":
                return _json_page([
                    _note(
                        2, _MARK_RC, author="alice",
                        created_at="2024-03-01T00:00:02Z",
                    ),
                ])
            return _json_page([
                _note(
                    1, "an ordinary comment, not a marker", author="bob",
                    created_at="2024-03-01T00:00:01Z",
                ),
            ], next_page="2")
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10, state="merged"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().merge_pr(_project(), "t", "10")
    assert [rv.author for rv in pr.reviews] == ["alice"]
    assert [rv.state for rv in pr.reviews] == ["request_changes"]


def test_list_pr_reviews_reconstructs_reviews_from_marked_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: `list_pr_reviews` must reconstruct the identical merged
    list `get_pr` does — it hits the notes endpoint itself (degrading to
    `[]` on 403/404, unlike `get_pr`'s strict fetch).

    RED reason (today): `list_pr_reviews` only ever calls
    `_reviews_from_approvals` — notes are never fetched at all."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    reviews = GitLabProvider().list_pr_reviews(_project(), "t", "10")
    assert [rv.author for rv in reviews] == ["alice"]
    assert [rv.state for rv in reviews] == ["request_changes"]


def test_merge_pr_reconstructs_reviews_from_marked_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: `merge_pr`'s returned PR must carry the identical
    merged reviews list too — same best-effort notes fetch as
    `list_pr_reviews`, inside the existing best-effort approvals `try`.

    RED reason (today): `merge_pr` only calls `_reviews_from_approvals`
    on the (here, empty) approvals payload — notes are never fetched."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "PUT" and url.endswith("/merge"):
            return _json(_mr_payload(10, state="merged"))
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
            ])
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10, state="merged"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().merge_pr(_project(), "t", "10")
    assert [rv.author for rv in pr.reviews] == ["alice"]
    assert [rv.state for rv in pr.reviews] == ["request_changes"]


def test_merge_reviews_keeps_live_approve_after_stale_request_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case (resolved-edge-case contract, pinned): alice has a
    marked `request_changes` note AND appears in the live
    `approved_by[]` (a later bare approve, no note). BOTH entries must
    surface: the marked `request_changes` (history) first, then the
    approvals-derived `approve` (current state) — not deduped away, and
    not reordered.

    RED reason (today): `pr.reviews` is only ever the single
    approvals-derived entry; the marked note is never reconstructed at
    all."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": True, "approvals_required": 1, "approvals_left": 0,
                "approved_by": [{"user": {"id": 1, "username": "alice"}}],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert len(pr.reviews) == 2
    assert pr.reviews[0].state == "request_changes"
    assert pr.reviews[0].author == "alice"
    assert pr.reviews[1].state == "approve"
    assert pr.reviews[1].author == "alice"
    assert pr.reviews[1].body is None
    assert pr.reviews[1].submitted_at == ""


def test_merge_reviews_does_not_suppress_duplicate_approve_from_same_author(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case (the OTHER direction of the dedupe rule; behaviour
    changed by WP #241 review finding R1 — see `_merge_reviews`'s
    docstring): bob has a marked `approve(body=…)` note AND appears in
    the live `approved_by[]` for what LOOKS LIKE the same approve
    event. Both entries now surface for bob — the marked note (real
    body/url/timestamp) AND the approvals-derived entry — rather than
    suppressing the latter.

    This looks like it should collapse to one entry, and used to. It no
    longer does: GitLab's `/approvals` payload carries no per-event id
    or timestamp, so this exact same `(note_reviews, approvals)` shape
    is ALSO what a stale marked `approve` note plus a genuinely NEW
    live re-approval (unapprove -> bare-reapprove, no note either way)
    produces — see
    `test_merge_reviews_keeps_live_approve_after_unapprove_then_bare_reapprove`.
    `_merge_reviews` cannot tell the two cases apart from these inputs,
    so it no longer silently drops one of them; the occasional
    duplicate-looking pair here is the accepted, documented cost of
    never losing a live approval. Was
    `test_merge_reviews_suppresses_duplicate_approve_from_same_author`,
    asserting `len(bob_reviews) == 1`, before this fix."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": True, "approvals_required": 1, "approvals_left": 0,
                "approved_by": [{"user": {"id": 2, "username": "bob"}}],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_APPROVE, author="bob", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    bob_reviews = [rv for rv in pr.reviews if rv.author == "bob"]
    assert len(bob_reviews) == 2
    assert bob_reviews[0].state == "approve"
    assert bob_reviews[0].body == _MARK_APPROVE
    assert bob_reviews[1].state == "approve"
    assert bob_reviews[1].body is None


def test_merge_reviews_keeps_live_approve_after_unapprove_then_bare_reapprove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for WP #241 review finding R1: a marked `approve`
    note is stale relative to a later, real re-approval, and that live
    re-approval must still surface rather than being silently dropped.

    Sequence: carol approves-with-a-body (marked `approve` note
    written) -> later plain-unapproves (GitLab's bare "revoke approval"
    action — no note, since it doesn't go through this library's own
    `request_changes` path) -> later bare-reapproves (no note either,
    by `submit_pr_review`'s own design). From the read side this is
    indistinguishable from the same-event case (GitLab's `/approvals`
    payload has no per-event id/timestamp — see `_merge_reviews`'s
    docstring for the full reasoning) — the mock below reproduces
    exactly that: one stale marked `approve` note plus a live
    `approved_by` entry for the same author.

    RED reason (pre-fix, `_merge_reviews`'s old suppression rule):
    `current_marked_state_by_author["carol"] == "approve"` filtered the
    approvals-derived entry out entirely, so `pr.reviews` only ever
    carried the STALE marked note and `carol`'s current, live approval
    was silently lost — exactly the class of bug #233/#234 were about.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": True, "approvals_required": 1, "approvals_left": 0,
                "approved_by": [{"user": {"id": 3, "username": "carol"}}],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_APPROVE, author="carol", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    carol_reviews = [rv for rv in pr.reviews if rv.author == "carol"]
    assert len(carol_reviews) == 2
    assert carol_reviews[0].state == "approve"
    assert carol_reviews[0].body == _MARK_APPROVE
    assert carol_reviews[1].state == "approve"
    assert carol_reviews[1].body is None
    assert carol_reviews[1].submitted_at == ""


# =============================================================================
# Behaviour 6 (design point 5): review_decision override.
# =============================================================================


def test_get_pr_review_decision_overridden_to_changes_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. Base decision from the approvals gate is
    `REVIEW_REQUIRED` (approvals_required=1, approved=False); alice has a
    marked `request_changes` note and is NOT a live approver. The
    override must promote this to `CHANGES_REQUESTED`.

    RED reason (today): `review_decision` only ever comes from `_map_mr`'s
    approvals-gate derivation — marked notes are never consulted, so it
    stays `REVIEW_REQUIRED`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 1, "approvals_left": 1,
                "approved_by": [],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert pr.review_decision == "CHANGES_REQUESTED"


def test_get_pr_review_decision_no_override_when_author_live_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case (supersede rule): alice has a marked `request_changes`
    note but is ALSO a live approver (`approved_by`) — approve wins, so
    the override must NOT fire; `review_decision` stays whatever the
    approvals gate computed (`APPROVED`). The stale `request_changes`
    note still appears in `reviews[]` as history (see the sibling merge
    test), it just no longer drives the decision.

    RED reason (today): since marked notes are never consulted at all,
    this assertion happens to describe the SAME value the unfixed code
    already returns from the approvals gate alone — so this test is
    additional coverage that may already pass; the override-suppression
    behaviour it pins only becomes meaningful once Behaviour 6 is
    implemented alongside Behaviour 5's merge. It is included here for
    completeness of the design-point contract."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": True, "approvals_required": 1, "approvals_left": 0,
                "approved_by": [{"user": {"id": 1, "username": "alice"}}],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert pr.review_decision == "APPROVED"


def test_get_pr_review_decision_comment_only_marker_is_decision_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: an author with ONLY a marked `comment`-state note (no
    `request_changes`, no `approve` marker at all from that author) must
    never trigger the override — proves a marked comment is genuinely
    decision-neutral, not merely that a request_changes-then-comment
    sequence still overrides (already covered by the merge test above).
    `_current_marked_state_by_author` ignores `comment`-state entries
    entirely, so carol never even enters the override's per-author
    state map here.

    RED reason (today): since marked notes are never consulted at all,
    this assertion happens to describe the SAME value the unfixed code
    already returns from the approvals gate alone — additional coverage
    that pins the comment-is-neutral contract once Behaviour 6 lands."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_COMMENT, author="carol", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert pr.review_decision is None


def test_get_pr_review_decision_not_overridden_when_approvals_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test (review round 4 blocking finding). When `/approvals`
    403s/404s, `_fetch_mr_approvals` returns `None` as a degrade sentinel
    — live approval state is genuinely unknown, not "zero approvals".
    Alice has a marked `request_changes` note; if the override treated
    `None` the same as `{}`/empty `approved_by[]`, it would promote the
    decision to `CHANGES_REQUESTED` permanently and uncorrectably (alice's
    live state, possibly a since-given approval, can never be observed on
    this degraded path). The override must instead leave `review_decision`
    at `base` (`None`, since `_map_mr` also can't compute a decision
    without approvals data).

    RED reason (today): `_decision_with_marker_override` does
    `approved_by = (approvals or {}).get("approved_by") or []`, which
    collapses `approvals is None` into the same `[]` as "zero approvals",
    so alice's marked `request_changes` state is never neutralized and the
    decision is wrongly promoted to `CHANGES_REQUESTED`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({}, status_code=403)
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert pr.review_decision is None


def test_merge_pr_review_decision_not_overridden_when_approvals_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same degraded-approvals-plus-marked-request_changes scenario as
    the `get_pr` driving test above, but through `merge_pr`'s own
    approvals-enrichment path (gitlab.py ~4615), which calls the same
    `_decision_with_marker_override` helper."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "PUT" and url.endswith("/merge"):
            return _json(_mr_payload(10, state="merged"))
        if req.method == "GET" and url.endswith("/approvals"):
            return _json({}, status_code=403)
        if req.method == "GET" and url.endswith("merge_requests/10"):
            return _json(_mr_payload(10, state="merged"))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().merge_pr(_project(), "t", "10", merge_method="merge")
    assert pr.review_decision is None


# =============================================================================
# Behaviour 7 (design point 6): marked notes excluded from comments[].
# =============================================================================


def test_get_pr_excludes_marked_review_notes_from_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. A marked `request_changes` note plus a plain note:
    `comments` must contain only the plain one; the marked note routes
    only into `reviews[]`.

    RED reason (today): the comment filter is
    `not it.get("system", False) and not it.get("position")` — a marked
    note has neither `system` nor `position` set, so it passes straight
    through into `comments[]` alongside the plain note."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/10/approvals"):
            return _json({
                "approved": False, "approvals_required": 0,
                "approvals_left": 0, "approved_by": [],
            })
        if url.endswith("merge_requests/10"):
            return _json(_mr_payload(10))
        if "merge_requests/10/notes" in url:
            return _json([
                _note(1, _MARK_RC, author="alice", created_at="2024-03-01T00:00:01Z"),
                _note(2, "just a plain note", author="dave", created_at="2024-03-01T00:00:02Z"),
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, comments = GitLabProvider().get_pr(_project(), "t", "10")
    assert len(comments) == 1
    assert comments[0].body == "just a plain note"
    assert not any(c.body == _MARK_RC for c in comments)
