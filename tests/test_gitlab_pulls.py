"""Tests for the GitLab provider's merge-request (PR) surface.

Covers list_prs, get_pr, create_pr, update_pr, add_pr_comment, merge_pr.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.base import PRFilters
from lib_python_projects.providers.gitlab import GitLabProvider


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="gitlab", path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
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


# ---------- list_prs ---------------------------------------------------------


def test_list_prs_default_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "acme%2Fbackend/merge_requests" in str(req.url)
        assert req.url.params.get("state") == "opened"
        assert req.url.params.get("per_page") == "30"
        return _json([_mr_payload(1), _mr_payload(2)])

    _install_mock(monkeypatch, handler)
    prs, _ = GitLabProvider().list_prs(_project(), "t", PRFilters())
    assert [p.id for p in prs] == ["1", "2"]


def test_list_prs_branch_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("source_branch") == "feat/x"
        assert req.url.params.get("target_branch") == "main"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_prs(  # return value intentionally ignored
        _project(), "t", PRFilters(head="feat/x", base="main"),
    )


def test_list_prs_state_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("state"))
        return _json([])

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().list_prs(p, "t", PRFilters(status="open"))   # result ignored
    GitLabProvider().list_prs(p, "t", PRFilters(status="closed"))  # result ignored
    GitLabProvider().list_prs(p, "t", PRFilters(status="any"))     # result ignored
    assert seen == ["opened", "closed", "all"]


# ---------- has_more boundary regression (ticket #39) -------------------------


def test_list_prs_has_more_true_when_full_page_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #39: list_prs returns has_more=True when the API returns
    exactly per_page (limit) items, indicating more pages may exist."""
    limit = 3
    payloads = [_mr_payload(i) for i in range(1, limit + 1)]

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(payloads)

    _install_mock(monkeypatch, handler)
    prs, has_more = GitLabProvider().list_prs(_project(), "t", PRFilters(limit=limit))
    assert len(prs) == limit
    assert has_more is True, "has_more must be True when API returns exactly per_page items"


def test_list_prs_has_more_false_when_partial_page_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #39: list_prs returns has_more=False when the API returns
    fewer than per_page items, indicating no further pages."""
    limit = 10

    def handler(req: httpx.Request) -> httpx.Response:
        # Return only 2 items when limit is 10 — partial page.
        return _json([_mr_payload(1), _mr_payload(2)])

    _install_mock(monkeypatch, handler)
    prs, has_more = GitLabProvider().list_prs(_project(), "t", PRFilters(limit=limit))
    assert len(prs) == 2
    assert has_more is False, "has_more must be False when API returns fewer than per_page items"


# ---------- get_pr -----------------------------------------------------------


def test_get_pr_returns_pr_and_filtered_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([
                {"id": 1, "body": "comment", "system": False,
                 "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z"},
                {"id": 2, "body": "approved", "system": True,
                 "author": {"username": "a"}, "created_at": "2024-01-01T00:01:00Z"},
                # Positional (diff-anchored) note — must be filtered out
                # so it doesn't double-surface alongside review_comments.
                {"id": 3, "body": "nit", "system": False,
                 "author": {"username": "a"}, "created_at": "2024-01-01T00:02:00Z",
                 "position": {"new_path": "src/foo.py", "new_line": 1}},
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, comments = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.id == "5"
    # system note + positional note both filtered → only the plain comment.
    assert len(comments) == 1
    assert comments[0].body == "comment"


# ---------- get_pr approval state (ticket #52 F9) ---------------------------


def test_gitlab_get_pr_review_decision_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approved MR: /approvals reports approved=True with a non-empty
    approved_by list — derive review_decision="APPROVED" and
    approvals_received from len(approved_by)."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5/approvals"):
            return _json({
                "approved": True,
                "approvals_required": 1,
                "approvals_left": 0,
                "approved_by": [{"user": {"username": "alice"}}],
            })
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.review_decision == "APPROVED"
    assert pr.approvals_received == 1
    assert pr.approvals_required == 1


def test_gitlab_get_pr_review_decision_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate configured but nobody has approved yet — should surface
    review_decision="REVIEW_REQUIRED" so a Merge-Gate agent can
    distinguish "not approved" from "no gate"."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5/approvals"):
            return _json({
                "approved": False,
                "approvals_required": 1,
                "approvals_left": 1,
                "approved_by": [],
            })
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.review_decision == "REVIEW_REQUIRED"
    assert pr.approvals_received == 0
    assert pr.approvals_required == 1


def test_gitlab_get_pr_no_approval_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No approval rules configured AND nobody approved: review_decision
    stays None (truly "nothing happened yet")."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5/approvals"):
            return _json({
                "approvals_required": 0,
                "approvals_left": 0,
                "approved_by": [],
            })
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.review_decision is None
    assert pr.approvals_received == 0
    assert pr.approvals_required == 0


def test_gitlab_get_pr_no_rules_but_someone_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No approval rules configured, but somebody hit `approve` on the
    MR: ad-hoc approves still surface as review_decision=APPROVED so
    consumers can tell "approved" apart from "no review yet" — the
    exact case #52 F9 sandbox testing flagged."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5/approvals"):
            return _json({
                "approvals_required": 0,
                "approvals_left": 0,
                "approved_by": [{"user": {"username": "alice"}}],
            })
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.review_decision == "APPROVED"
    assert pr.approvals_received == 1
    assert pr.approvals_required == 0


def test_gitlab_get_pr_gate_partially_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate requires 2 approvals, only 1 has signed off: review_decision
    must be REVIEW_REQUIRED (the gate is not satisfied) even though
    approved_by is non-empty."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5/approvals"):
            return _json({
                "approved": False,
                "approvals_required": 2,
                "approvals_left": 1,
                "approved_by": [{"user": {"username": "alice"}}],
            })
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.review_decision == "REVIEW_REQUIRED"
    assert pr.approvals_received == 1
    assert pr.approvals_required == 2


def test_gitlab_list_prs_does_not_fetch_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_prs must NOT issue a per-MR /approvals round-trip. The
    follow-up request would scale linearly with the listing and is
    only worth it on the single-MR get_pr path."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        # Any /approvals hit during list_prs is a regression — fail
        # the test loudly rather than silently returning data.
        assert "/approvals" not in url, (
            f"list_prs must not fetch approvals (saw {url})"
        )
        if url.endswith("/merge_requests") or "/merge_requests?" in url:
            return _json([_mr_payload(1), _mr_payload(2)])
        return _json({}, status_code=404)

    seen = _install_mock(monkeypatch, handler)
    GitLabProvider().list_prs(_project(), "t", PRFilters())
    # Belt-and-braces: scan recorded requests as well, in case the
    # handler's assert was bypassed by an exception.
    assert all("/approvals" not in str(r.url) for r in seen)


# ---------- create_pr --------------------------------------------------------


def test_create_pr_applies_markers_and_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "merge_requests" in str(req.url):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(7, description=captured["body"]["description"]))
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().create_pr(
        _project(), "t",
        title="New MR", body="x", head="feat/x", base="main",
        draft=True, labels=["enhancement"],
    )
    assert pr.id == "7"
    # Title carries the canonical Draft: prefix in addition to draft=True
    # (some GitLab setups silently drop the param — prefix is the
    # reliable signal).
    assert captured["body"]["title"] == "Draft: New MR"
    assert captured["body"]["source_branch"] == "feat/x"
    assert captured["body"]["target_branch"] == "main"
    assert captured["body"]["draft"] is True
    assert captured["body"]["description"].startswith("#ai-generated")
    assert "ai-generated" in captured["body"]["labels"]
    assert "enhancement" in captured["body"]["labels"]


def test_create_pr_draft_omitted_when_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "merge_requests" in str(req.url):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(1))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_pr(
        _project(), "t", title="t", body="b", head="x", base="main",
    )
    assert "draft" not in captured["body"]


# ---------- update_pr -------------------------------------------------------


def test_update_pr_status_close(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_mr_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="closed"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().update_pr(_project(), "t", "5", status="closed")
    assert pr.status == "closed"
    assert captured["body"]["state_event"] == "close"


def test_update_pr_rejects_merged_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status="merged"` must point to merge_pr — refuse it here."""
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(ValueError, match="merge_pr"):
        GitLabProvider().update_pr(_project(), "t", "5", status="merged")


def test_update_pr_changes_target_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_mr_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, target_branch="develop"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(_project(), "t", "5", base="develop")
    assert captured["body"]["target_branch"] == "develop"


def test_update_pr_adds_ai_modified_when_not_ai_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_mr_payload(5, labels=["bug"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(_project(), "t", "5", title="renamed")
    assert "ai-modified" in captured["body"]["add_labels"]


# ---------- add_pr_comment ---------------------------------------------------


def test_add_pr_comment_applies_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 1, "body": captured["body"]["body"],
                "author": {"username": "a"},
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().add_pr_comment(_project(), "t", "5", "review note")
    assert c.id == "1"
    assert captured["body"]["body"].startswith("#ai-generated")


def test_add_pr_comment_synthesises_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When GitLab omits noteable_iid/noteable_type/web_url from the MR-note
    POST response, the URL must still be synthesised from the MR iid and
    project.web_url that the caller already holds."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return _json({
                "id": 1, "body": "review note",
                "author": {"username": "a"},
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().add_pr_comment(_project(), "t", "5", "review note")
    assert c.url == "https://gitlab.com/acme/backend/-/merge_requests/5#note_1"


# ---------- merge_pr ---------------------------------------------------------


def test_merge_pr_merge_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "PUT" and url.endswith("/merge"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="merged"))
        if req.method == "GET":
            return _json(_mr_payload(5, state="merged"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().merge_pr(_project(), "t", "5", merge_method="merge")
    assert pr.status == "merged"
    assert "squash" not in captured["body"]


def test_merge_pr_squash_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "PUT" and url.endswith("/merge"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="merged"))
        if req.method == "GET":
            return _json(_mr_payload(5, state="merged"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().merge_pr(_project(), "t", "5", merge_method="squash")
    assert captured["body"]["squash"] is True


def test_merge_pr_rebase_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`merge_method='rebase'` must surface a clear error pointing at the
    separate rebase endpoint."""
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(ValueError, match="rebase"):
        GitLabProvider().merge_pr(_project(), "t", "5", merge_method="rebase")


def test_merge_pr_unknown_strategy_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(ValueError, match="unsupported"):
        GitLabProvider().merge_pr(
            _project(), "t", "5", merge_method="cherry-pick",
        )


def test_merge_pr_refetches_after_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror GitHub provider's pattern: after merge, do a fresh GET to
    pick up post-merge state mutations (merge_commit_sha, etc.)."""
    seen_methods: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        seen_methods.append(f"{req.method} {url.split('?')[0].split('/api/v4')[-1]}")
        if req.method == "PUT":
            return _json(_mr_payload(5, state="merged"))
        if req.method == "GET":
            return _json(_mr_payload(5, state="merged"))
        return _json({}, 404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().merge_pr(_project(), "t", "5", merge_method="merge")
    # Sequence: PUT /merge then GET /merge_requests/5
    assert seen_methods[0].startswith("PUT")
    assert seen_methods[0].endswith("/merge")
    assert seen_methods[1].startswith("GET")
    assert seen_methods[1].endswith("/merge_requests/5")


def test_merge_pr_commit_message_routed_per_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            captured.setdefault("bodies", []).append(
                json.loads(req.content.decode())
            )
            return _json(_mr_payload(5, state="merged"))
        return _json(_mr_payload(5, state="merged"))

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().merge_pr(
        p, "t", "5", merge_method="merge", commit_message="m1",
    )
    GitLabProvider().merge_pr(
        p, "t", "5", merge_method="squash", commit_message="m2",
    )
    assert captured["bodies"][0]["merge_commit_message"] == "m1"
    assert captured["bodies"][1]["squash_commit_message"] == "m2"


def test_gitlab_merge_pr_default_merge_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `merge_method="merge"` must NOT send `merge_method` in
    the PUT body (GitLab doesn't accept that field) and must not toggle
    `squash`. Guards the GitHub-style signature unification (#52 F1).
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="merged"))
        return _json(_mr_payload(5, state="merged"))

    _install_mock(monkeypatch, handler)
    GitLabProvider().merge_pr(_project(), "t", "5", merge_method="merge")
    assert "merge_method" not in captured["body"]
    assert "squash" not in captured["body"]


def test_gitlab_merge_pr_squash_with_commit_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`commit_title` + `commit_message` join into a single GitLab-side
    `squash_commit_message` separated by a blank line (#52 F1 parity)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="merged"))
        return _json(_mr_payload(5, state="merged"))

    _install_mock(monkeypatch, handler)
    GitLabProvider().merge_pr(
        _project(), "t", "5",
        merge_method="squash",
        commit_title="T",
        commit_message="B",
    )
    assert captured["body"]["squash"] is True
    assert captured["body"]["squash_commit_message"] == "T\n\nB"


# ---------- inline review comments (ticket #43 D) --------------------------


def test_list_pr_review_comments_flattens_positional_discussions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only diff-anchored discussions become ReviewComment items."""

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "GET"
            and req.url.path.endswith("merge_requests/5/discussions")
        ):
            return _json([
                {
                    "id": "disc-A",
                    "notes": [
                        {
                            "id": 1, "body": "nit",
                            "author": {"username": "alice"},
                            "position": {
                                "new_path": "src/foo.py", "new_line": 42,
                                "old_path": "src/foo.py", "old_line": 40,
                                "head_sha": "h1", "base_sha": "b1",
                            },
                            "created_at": "t",
                        },
                        {
                            "id": 2, "body": "agreed",
                            "author": {"username": "bob"},
                            "position": None,
                            "created_at": "t",
                        },
                    ],
                },
                {
                    "id": "disc-B",
                    "notes": [
                        {
                            "id": 3, "body": "general thought",
                            "author": {"username": "alice"},
                            "position": None,
                            "created_at": "t",
                        },
                    ],
                },
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rcs = GitLabProvider().list_pr_review_comments(_project(), "t", "5")
    assert [rc.id for rc in rcs] == ["1", "2"]
    assert rcs[0].in_reply_to is None
    assert rcs[1].in_reply_to == "disc-A"
    assert rcs[0].path == "src/foo.py"
    # Both notes share the discussion anchor — caller passes this to
    # `in_reply_to` to reply (fix for #43 live-verify reply-blocked bug).
    assert rcs[0].discussion_id == "disc-A"
    assert rcs[1].discussion_id == "disc-A"


def test_add_pr_review_comment_new_thread_posts_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New-thread mode POSTs `/discussions` with a position object."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(
                    5,
                    diff_refs={"base_sha": "b1", "start_sha": "s1", "head_sha": "h1"},
                )
            )
        if req.method == "POST" and url.endswith("merge_requests/5/discussions"):
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": "disc-new",
                "notes": [{
                    "id": 99, "body": captured["body"]["body"],
                    "author": {"username": "alice"},
                    "created_at": "t",
                    "web_url": "url",
                }],
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rc = GitLabProvider().add_pr_review_comment(
        _project(), "t", "5",
        body="rename", path="src/foo.py", line=42, commit_sha="h1",
    )
    assert captured["body"]["body"].startswith("#ai-generated\n\n")
    pos = captured["body"]["position"]
    assert pos["new_path"] == "src/foo.py"
    assert pos["new_line"] == 42
    assert pos["head_sha"] == "h1"
    assert pos["base_sha"] == "b1"
    assert pos["start_sha"] == "s1"
    assert rc.id == "99"
    # Discussion id surfaced so the caller can immediately reply
    # without a second round-trip (fix for #43 live-verify bug).
    assert rc.discussion_id == "disc-new"


def test_add_pr_review_comment_reply_posts_to_discussion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reply mode POSTs `/discussions/{id}/notes`."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if (
            req.method == "POST"
            and "/discussions/disc-A/notes" in req.url.path
        ):
            return _json({
                "id": 100, "body": "agreed",
                "author": {"username": "alice"},
                "created_at": "t", "web_url": "url",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rc = GitLabProvider().add_pr_review_comment(
        _project(), "t", "5",
        body="agreed", in_reply_to="disc-A",
    )
    assert rc.id == "100"
    assert rc.in_reply_to == "disc-A"
    # The reply stays anchored to the same discussion the caller joined.
    assert rc.discussion_id == "disc-A"
    # Sanity: no merge-request GET (reply mode skips diff_refs fetch).
    assert all("/merge_requests/5" not in p or "/discussions/" in p for p in seen)


# ---------- Issue #17: merge_pr 405 → "already merged" ----------------------


def test_merge_pr_405_reports_already_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab returns 405 when trying to merge an already-merged MR.
    The provider must surface a clear 'already merged' message."""
    from lib_python_projects.providers.gitlab import GitLabError

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and "/merge" in str(req.url):
            return _json({"message": "405 Method Not Allowed"}, status_code=405)
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().merge_pr(_project(), "t", "7", merge_method="merge")
    assert exc.value.status == 405
    assert "already merged" in exc.value.message
    assert "acme#7" in exc.value.message


def test_create_then_reply_round_trip_uses_surfaced_discussion_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh new-thread call exposes discussion_id; using it as
    in_reply_to lets the next call reach `/discussions/{id}/notes`
    without any extra read. This is the exact flow that was broken on
    GitLab live (#43 comment 4511603938)."""
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        seen.append((req.method, req.url.path))
        if req.method == "GET" and url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(
                    5,
                    diff_refs={"base_sha": "b", "start_sha": "s", "head_sha": "h"},
                )
            )
        if req.method == "POST" and url.endswith("merge_requests/5/discussions"):
            return _json({
                "id": "disc-real-id",
                "notes": [{
                    "id": 10, "body": "starter",
                    "author": {"username": "alice"}, "created_at": "t",
                    "web_url": "url",
                }],
            })
        if (
            req.method == "POST"
            and "/discussions/disc-real-id/notes" in req.url.path
        ):
            return _json({
                "id": 11, "body": "reply",
                "author": {"username": "alice"}, "created_at": "t",
                "web_url": "url",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    provider = GitLabProvider()
    starter = provider.add_pr_review_comment(
        _project(), "t", "5",
        body="starter", path="src/foo.py", line=1, commit_sha="h",
    )
    assert starter.discussion_id == "disc-real-id"
    reply = provider.add_pr_review_comment(
        _project(), "t", "5",
        body="reply", in_reply_to=starter.discussion_id,
    )
    assert reply.discussion_id == "disc-real-id"
    # Sanity: the reply POST went to the right discussion path.
    assert any(
        m == "POST" and p.endswith("/discussions/disc-real-id/notes")
        for m, p in seen
    )


# ---------- submit_pr_review (ticket #43 C) --------------------------------


def test_submit_pr_review_approve_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approve without body hits `/approve` and `GET /user` — never `/notes`.

    Ticket #136: the `/approve` response is a MergeRequestApproval object
    (no top-level `user`), so `author` must come from the authenticated-
    identity endpoint instead of degrading to `""`. `body`/`url` are
    genuinely absent (no note was posted) so they must be `None`, not `""`.
    """
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "POST" and req.url.path.endswith("/approve"):
            return _json({"iid": 5, "web_url": "u", "updated_at": "t"})
        if req.method == "GET" and req.url.path.endswith("/user"):
            return _json({"id": 1, "username": "acting-user"})
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="approve",
    )
    assert review.state == "approve"
    assert review.author == "acting-user"
    assert review.body is None
    assert review.url is None
    assert all("notes" not in p for p in seen)
    assert any(p.endswith("/user") for p in seen)


def test_submit_pr_review_approve_with_body_populates_note_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the bare-approve case: when a note IS created, the
    note's author/body/url stay populated (non-None) strings — the
    None-on-absence behavior must not regress this path."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/approve"):
            return _json({"iid": 5, "web_url": "u", "updated_at": "t"})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return _json({
                "id": 9, "body": "#ai-generated\n\nlgtm",
                "author": {"username": "alice"},
                "web_url": "url",
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="approve", body="lgtm",
    )
    assert review.author == "alice"
    assert isinstance(review.body, str) and review.body
    assert isinstance(review.url, str) and review.url


def test_submit_pr_review_approve_with_body_posts_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approve with body hits `/approve` then `/notes`."""
    seen: list[str] = []
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "POST" and req.url.path.endswith("/approve"):
            return _json({"iid": 5, "web_url": "u", "updated_at": "t"})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 9, "body": captured["body"]["body"],
                "author": {"username": "alice"},
                "web_url": "url",
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="approve", body="lgtm",
    )
    assert review.state == "approve"
    assert captured["body"]["body"].startswith("#ai-generated\n\n")


def test_submit_pr_review_request_changes_unapproves_then_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_changes hits `/unapprove` then `/notes`."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "POST" and req.url.path.endswith("/unapprove"):
            return _json({})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return _json({
                "id": 9, "body": "please fix",
                "author": {"username": "alice"},
                "web_url": "url",
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="request_changes", body="please fix",
    )
    assert review.state == "request_changes"
    assert any("/unapprove" in p for p in seen)
    assert any("/notes" in p for p in seen)


def test_submit_pr_review_comment_only_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return _json({
                "id": 9, "body": "fyi",
                "author": {"username": "alice"},
                "web_url": "url",
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="comment", body="fyi",
    )
    assert all("/approve" not in p and "/unapprove" not in p for p in seen)


# ---------- reviewers on write surface (ticket #43 B) ----------------------


def test_create_pr_passes_reviewer_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pr resolves usernames to ids and sends `reviewer_ids`."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and req.url.path == "/api/v4/users":
            name = req.url.params.get("username")
            uid = {"bob": 11, "carol": 22}.get(name)
            return _json([{"id": uid, "username": name}] if uid else [])
        if req.method == "POST" and "merge_requests" in url:
            captured["body"] = json.loads(req.content.decode())
            return _json(
                _mr_payload(8, reviewers=[{"username": "bob"}, {"username": "carol"}])
            )
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_pr(
        _project(), "t",
        title="T", body="b", head="feat/x", base="main",
        requested_reviewers=["bob", "carol"],
    )
    assert captured["body"]["reviewer_ids"] == [11, 22]


def test_update_pr_reviewers_replace_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer diff is replace-all via a single PUT with `reviewer_ids`."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(5, reviewers=[{"username": "alice"}, {"username": "bob"}])
            )
        if req.method == "GET" and req.url.path == "/api/v4/users":
            name = req.url.params.get("username")
            uid = {"bob": 11, "carol": 22}.get(name)
            return _json([{"id": uid, "username": name}] if uid else [])
        if req.method == "PUT" and "merge_requests/5" in url:
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(
        _project(), "t", "5",
        reviewers_add=["carol"],
        reviewers_remove=["alice"],
    )
    # alice removed, bob preserved, carol added → final: [bob, carol] → [11, 22]
    assert captured["body"]["reviewer_ids"] == [11, 22]


# ---------- draft toggle (ticket #43 A) -------------------------------------


def test_update_pr_draft_true_adds_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """draft=True on a ready MR sets `Draft: ` title prefix."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("merge_requests/5"):
            return _json(_mr_payload(5, title="Add feature X"))
        if req.method == "PUT" and "merge_requests/5" in url:
            captured["body"] = json.loads(req.content)
            return _json(_mr_payload(5, title=captured["body"]["title"]))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(_project(), "t", "5", draft=True)
    assert captured["body"]["title"] == "Draft: Add feature X"


def test_update_pr_draft_false_strips_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """draft=False strips legacy prefixes (Draft:/WIP:/[Draft])."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("merge_requests/5"):
            return _json(_mr_payload(5, title="WIP: Add feature X"))
        if req.method == "PUT" and "merge_requests/5" in url:
            captured["body"] = json.loads(req.content)
            return _json(_mr_payload(5, title=captured["body"]["title"]))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(_project(), "t", "5", draft=False)
    assert captured["body"]["title"] == "Add feature X"


def test_update_pr_draft_combined_with_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit title plus draft=True yields a freshly-prefixed title."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("merge_requests/5"):
            return _json(_mr_payload(5, title="WIP: Old title"))
        if req.method == "PUT" and "merge_requests/5" in url:
            captured["body"] = json.loads(req.content)
            return _json(_mr_payload(5, title=captured["body"]["title"]))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(
        _project(), "t", "5", title="New title", draft=True,
    )
    assert captured["body"]["title"] == "Draft: New title"


# ---------- response-shape inventory (ticket #43 G) -------------------------


def test_get_pr_surfaces_gitlab_specific_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_map_mr` propagates detailed_merge_status, pipeline_status, approvals,
    and merge_commit_sha. GitHub-only fields stay `None`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(
                    5,
                    detailed_merge_status="ci_must_pass",
                    head_pipeline={"status": "running"},
                    approvals_required=2,
                    approvals_received=1,
                    merge_commit_sha="deadbeef",
                )
            )
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.detailed_merge_status == "ci_must_pass"
    assert pr.pipeline_status == "running"
    assert pr.approvals_required == 2
    assert pr.approvals_received == 1
    assert pr.merge_commit_sha == "deadbeef"
    # GitHub-only fields stay None on a GitLab payload.
    assert pr.mergeable_state is None
    assert pr.auto_merge is None
    assert pr.review_decision is None


# ---------- _map_mr head.repo_full_name (ticket #4) --------------------------


def test_map_mr_head_repo_full_name_same_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same source/target project → repo_full_name == project.path."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(5, project_id=10, source_project_id=10)
            )
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.head["repo_full_name"] == "acme/backend"


def test_map_mr_head_repo_full_name_cross_fork_with_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-fork with source_project_path → repo_full_name == source_project_path."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(
                    5,
                    project_id=10,
                    source_project_id=99,
                    source_project_path="fork/backend",
                )
            )
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.head["repo_full_name"] == "fork/backend"


def test_map_mr_head_repo_full_name_cross_fork_no_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-fork without source_project_path → repo_full_name is None."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(5, project_id=10, source_project_id=99)
            )
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.head["repo_full_name"] is None


# ---------- _map_mr base.sha from diff_refs (ticket #4) ----------------------


def test_map_mr_base_sha_from_diff_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """diff_refs.base_sha → pr.base['sha']."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(
                    5,
                    diff_refs={"base_sha": "deadbeef", "start_sha": "s1", "head_sha": "h1"},
                )
            )
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.base["sha"] == "deadbeef"


def test_map_mr_base_sha_empty_when_no_diff_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No diff_refs → pr.base['sha'] == ''."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.base["sha"] is None


# ---------- list_pr_review_comments side derivation (ticket #4) --------------


def test_list_pr_review_comments_side_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """new_line→RIGHT, old_line→LEFT, neither→None."""

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "GET"
            and req.url.path.endswith("merge_requests/5/discussions")
        ):
            return _json([
                {
                    "id": "disc-1",
                    "notes": [{
                        "id": 1, "body": "new-line only",
                        "author": {"username": "alice"},
                        "position": {
                            "new_path": "a.py", "new_line": 5,
                            "old_path": "a.py", "old_line": None,
                            "head_sha": "h", "base_sha": "b",
                        },
                        "created_at": "t",
                    }],
                },
                {
                    "id": "disc-2",
                    "notes": [{
                        "id": 2, "body": "old-line only",
                        "author": {"username": "alice"},
                        "position": {
                            "new_path": "a.py", "new_line": None,
                            "old_path": "a.py", "old_line": 3,
                            "head_sha": "h", "base_sha": "b",
                        },
                        "created_at": "t",
                    }],
                },
                {
                    "id": "disc-3",
                    "notes": [{
                        "id": 3, "body": "neither",
                        "author": {"username": "alice"},
                        "position": {
                            "new_path": "a.py", "new_line": None,
                            "old_path": "a.py", "old_line": None,
                            "head_sha": "h", "base_sha": "b",
                        },
                        "created_at": "t",
                    }],
                },
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rcs = GitLabProvider().list_pr_review_comments(_project(), "t", "5")
    assert rcs[0].side == "RIGHT"
    assert rcs[1].side == "LEFT"
    assert rcs[2].side is None


# ---------- add_pr_review_comment URL synthesis (ticket #4) ------------------


def test_add_pr_review_comment_new_thread_url_synthesised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New-thread POST without web_url → URL synthesised as note anchor."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("merge_requests/5"):
            return _json(
                _mr_payload(
                    5,
                    diff_refs={"base_sha": "b1", "start_sha": "s1", "head_sha": "h1"},
                )
            )
        if req.method == "POST" and url.endswith("merge_requests/5/discussions"):
            return _json({
                "id": "disc-new",
                "notes": [{
                    "id": 42, "body": "#ai-generated\n\nrename",
                    "author": {"username": "alice"},
                    "created_at": "t",
                }],
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rc = GitLabProvider().add_pr_review_comment(
        _project(), "t", "5",
        body="rename", path="src/foo.py", line=1, commit_sha="h1",
    )
    assert rc.url == "https://gitlab.com/acme/backend/-/merge_requests/5#note_42"


def test_add_pr_review_comment_reply_url_synthesised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reply POST without web_url → URL synthesised as note anchor."""

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "POST"
            and "/discussions/disc-A/notes" in req.url.path
        ):
            return _json({
                "id": 77, "body": "agreed",
                "author": {"username": "alice"},
                "created_at": "t",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rc = GitLabProvider().add_pr_review_comment(
        _project(), "t", "5",
        body="agreed", in_reply_to="disc-A",
    )
    assert rc.url.endswith("#note_77")
    assert "merge_requests/5" in rc.url


# ---------- submit_pr_review URL synthesis (ticket #4) -----------------------


def test_submit_pr_review_approve_no_body_url_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approve without body → url is None (ticket #136: no note means no
    review-specific URL to report, regardless of the MR's own web_url)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/approve"):
            return _json({
                "iid": 5,
                "web_url": "https://gitlab.com/acme/backend/-/merge_requests/5",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        if req.method == "GET" and req.url.path.endswith("/user"):
            return _json({"id": 1, "username": "acting-user"})
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(_project(), "t", "5", state="approve")
    assert review.url is None


def test_submit_pr_review_approve_with_body_url_is_note_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approve with body → note POST has no web_url → url is synthesised note anchor."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/approve"):
            return _json({"iid": 5, "updated_at": "t"})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return _json({
                "id": 55, "body": "#ai-generated\n\nlgtm",
                "author": {"username": "alice"},
                "created_at": "t",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="approve", body="lgtm",
    )
    assert review.url.endswith("#note_55")
    assert "merge_requests/5" in review.url


def test_submit_pr_review_request_changes_url_synthesised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_changes note POST without web_url → url is synthesised note anchor."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/unapprove"):
            return _json({})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return _json({
                "id": 66, "body": "#ai-generated\n\nplease fix",
                "author": {"username": "alice"},
                "created_at": "t",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="request_changes", body="please fix",
    )
    assert review.url.endswith("#note_66")
    assert "merge_requests/5" in review.url


def test_submit_pr_review_comment_url_synthesised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """comment note POST without web_url → url is synthesised note anchor."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return _json({
                "id": 88, "body": "#ai-generated\n\nfyi",
                "author": {"username": "alice"},
                "created_at": "t",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    review = GitLabProvider().submit_pr_review(
        _project(), "t", "5", state="comment", body="fyi",
    )
    assert review.url.endswith("#note_88")
    assert "merge_requests/5" in review.url


# ---------- list_comments created_after (ticket #4) --------------------------


def test_list_comments_since_uses_created_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """since parameter must be forwarded as created_after (not updated_after)."""
    seen_params: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen_params.update(dict(req.url.params))
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_comments(_project(), "t", "5", since="2024-06-01T00:00:00Z")
    assert seen_params.get("created_after") == "2024-06-01T00:00:00Z"
    assert "updated_after" not in seen_params


# ---------- ticket #30: return-shape None vs "" fixes -------------------------


def test_map_note_populates_updated_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """_map_note must populate updated_at from the GitLab payload."""
    note = {
        "id": 1,
        "body": "hi",
        "author": {"username": "alice"},
        "created_at": "2026-05-20T10:00:00.123Z",
        "updated_at": "2026-05-21T11:30:45.456Z",
        "system": False,
        "noteable_iid": 5,
        "noteable_type": "Issue",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/issues/5/notes"):
            return _json([note])
        if req.url.path.endswith("/issues/5"):
            return _json({
                "iid": 5, "title": "T", "description": "",
                "state": "opened", "author": {"username": "a"},
                "assignees": [], "labels": [],
                "web_url": "https://gitlab.com/acme/backend/-/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    _ticket, comments, _rels, _trunc = GitLabProvider().get_ticket(
        _project(), "t", "5", include_relations=False,
    )
    assert len(comments) == 1
    assert comments[0].updated_at == "2026-05-21T11:30:45Z"


def test_map_mr_strips_draft_prefix_from_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """_map_mr must strip the 'Draft: ' prefix from MR titles so that
    `pr.draft=True` and `pr.title` contains only the bare title."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/3"):
            return _json(_mr_payload(3, title="Draft: My feature", draft=True))
        if "merge_requests/3/approvals" in url:
            return _json({}, status_code=404)
        if "merge_requests/3/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "3")
    assert pr.draft is True
    assert pr.title == "My feature"


def test_map_mr_strips_wip_prefix_from_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """WIP: prefix variant is also stripped."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/4"):
            return _json(_mr_payload(4, title="WIP: Old style", draft=True))
        if "merge_requests/4/approvals" in url:
            return _json({}, status_code=404)
        if "merge_requests/4/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "4")
    assert pr.title == "Old style"


def test_map_mr_no_draft_prefix_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-draft MRs with a regular title are returned unchanged."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/6"):
            return _json(_mr_payload(6, title="Normal title", draft=False))
        if "merge_requests/6/approvals" in url:
            return _json({}, status_code=404)
        if "merge_requests/6/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "6")
    assert pr.title == "Normal title"


def test_map_mr_base_sha_none_when_diff_refs_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base.sha must be None when GitLab omits diff_refs (fresh MR)."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/7"):
            # No diff_refs key at all.
            return _json(_mr_payload(7))
        if "merge_requests/7/approvals" in url:
            return _json({}, status_code=404)
        if "merge_requests/7/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "7")
    assert pr.base["sha"] is None


def test_map_mr_base_sha_populated_when_diff_refs_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base.sha must be populated when GitLab includes diff_refs."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/8"):
            return _json(
                _mr_payload(8, diff_refs={"base_sha": "abc123", "head_sha": "def456", "start_sha": "abc123"})
            )
        if "merge_requests/8/approvals" in url:
            return _json({}, status_code=404)
        if "merge_requests/8/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, _ = GitLabProvider().get_pr(_project(), "t", "8")
    assert pr.base["sha"] == "abc123"


def test_review_comment_reply_has_commit_sha_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reply review comments (in_reply_to set) must have commit_sha=None
    because replies have no diff anchor of their own."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/discussions/disc-X/notes" in req.url.path:
            return _json({
                "id": 55, "body": "agreed",
                "author": {"username": "alice"},
                "created_at": "2026-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rc = GitLabProvider().add_pr_review_comment(
        _project(), "t", "5",
        body="agreed", in_reply_to="disc-X",
    )
    assert rc.commit_sha is None


def test_list_pr_review_comments_commit_sha_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When position carries no sha at all, commit_sha must be None."""
    discussions = [{
        "id": "disc-1",
        "notes": [{
            "id": 10, "body": "review",
            "author": {"username": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "position": {"new_path": "src/a.py", "new_line": 5},
            # No head_sha or base_sha.
        }],
    }]

    def handler(req: httpx.Request) -> httpx.Response:
        if "/discussions" in req.url.path:
            return _json(discussions)
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    rcs = GitLabProvider().list_pr_review_comments(_project(), "t", "5")
    assert rcs[0].commit_sha is None


def test_update_pr_draft_true_title_stripped_in_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_pr(draft=True) returns a pr whose title has the Draft: prefix
    stripped — the agent receives the clean title, not the prefixed wire value."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("merge_requests/9"):
            return _json(_mr_payload(9, title="My feature"))
        if req.method == "PUT" and url.endswith("merge_requests/9"):
            captured["sent"] = json.loads(req.content.decode())
            # GitLab echoes back the prefixed title.
            return _json(_mr_payload(9, title="Draft: My feature", draft=True))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().update_pr(_project(), "t", "9", draft=True)
    # Wire title is "Draft: My feature" but _map_mr must strip it.
    assert pr.title == "My feature"
    assert pr.draft is True


# ---------- Ticket #57: P4 — list_prs base.sha is None when diff_refs absent --


def test_list_prs_base_sha_is_none_when_diff_refs_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_prs must not crash and must return base.sha=None when the MR payload
    lacks diff_refs (freshly-created MRs before a pipeline run). This is the
    documented gap noted in _map_mr's docstring."""

    def handler(req: httpx.Request) -> httpx.Response:
        # MR payload with no diff_refs key at all.
        payload = _mr_payload(7)
        payload.pop("diff_refs", None)  # ensure absent
        return _json([payload])

    _install_mock(monkeypatch, handler)
    prs, has_more = GitLabProvider().list_prs(
        _project(), "t", PRFilters(limit=10),
    )
    assert len(prs) == 1
    assert prs[0].base["sha"] is None
