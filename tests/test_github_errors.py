"""Tests for GitHub provider error-contract fixes (tickets #17 and #28).

Covers:
- update_ticket 404 → "ticket '<project>#<id>' not found"
- add_comment 404 → "ticket '<project>#<id>' not found"
- update_comment 404 → "comment '<project>#<id>' not found"
- add_pr_review_comment 422 → named parameter message
- create_pr reviewer 422 → PR still returned with warning (ticket #28)
- create_pr reviewer 500 → GitHubError raised
- create_pr primary 422 → GitHubError raised
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
    assert "comment '99' not found in acme" in exc.value.message


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


# ---------- Ticket #28: create_pr side-step 422 is non-fatal -----------------


def _minimal_pr_payload(number: int = 42) -> dict:
    """Return a minimal GitHub PR REST payload accepted by `_map_pr`."""
    return {
        "number": number,
        "state": "open",
        "title": "Test PR",
        "body": "<!-- #ai-generated -->\nDescription.",
        "user": {"login": "bot"},
        "assignees": [],
        "requested_reviewers": [],
        "labels": [],
        "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "acme/backend"}},
        "base": {"ref": "main", "sha": "def"},
        "draft": False,
        "merged": False,
        "mergeable": None,
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def test_create_pr_reviewer_422_returns_pr_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr with a reviewer 422 must still return the PR with a warning."""

    pr_payload = _minimal_pr_payload(42)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Label ensure (GET or POST /repos/.../labels/...)
        if "/labels" in path and req.method in ("GET", "POST"):
            if req.method == "GET":
                return _json({"name": "ai-generated", "color": "0075ca"})
            # POST /issues/.../labels — success, return label list
            return _json([{"name": "ai-generated"}])
        # Primary PR creation
        if path.endswith("/pulls") and req.method == "POST":
            return _json(pr_payload, status_code=201)
        # Reviewer request — 422
        if "/requested_reviewers" in path and req.method == "POST":
            return _json(
                {"message": "Review cannot be requested from pull request author."},
                status_code=422,
            )
        # Fallback success for any other call
        return _json({})

    _install_mock(monkeypatch, handler)
    pr = GitHubProvider().create_pr(
        _project(),
        token="t",
        title="Test PR",
        body="Description.",
        head="feature",
        base="main",
        requested_reviewers=["author-user"],
    )

    assert pr.number == 42
    assert pr.url == "https://github.com/acme/backend/pull/42"
    assert len(pr.warnings) == 1
    assert "requested_reviewers" in pr.warnings[0]
    assert "422" in pr.warnings[0] or "Review cannot" in pr.warnings[0]


def test_create_pr_reviewer_500_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr with a reviewer 500 must raise GitHubError."""

    pr_payload = _minimal_pr_payload(43)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/labels" in path and req.method == "GET":
            return _json({"name": "ai-generated", "color": "0075ca"})
        if "/labels" in path and req.method == "POST":
            return _json([{"name": "ai-generated"}])
        if path.endswith("/pulls") and req.method == "POST":
            return _json(pr_payload, status_code=201)
        if "/requested_reviewers" in path and req.method == "POST":
            return _json({"message": "Internal Server Error"}, status_code=500)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_pr(
            _project(),
            token="t",
            title="Test PR",
            body="Description.",
            head="feature",
            base="main",
            requested_reviewers=["reviewer"],
        )
    assert exc.value.status == 500


def test_create_pr_primary_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr with a head-branch 422 names the branch and gives a push hint."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/labels" in path and req.method == "GET":
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path.endswith("/pulls") and req.method == "POST":
            return _json(
                {
                    "message": "Validation Failed",
                    "errors": [{"message": "head branch does not exist"}],
                },
                status_code=422,
            )
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_pr(
            _project(),
            token="t",
            title="Test PR",
            body="Description.",
            head="nonexistent",
            base="main",
        )
    assert exc.value.status == 422
    assert "nonexistent" in exc.value.message
    assert "push it first" in exc.value.message


# ---------- Case 4: no duplicated provider prefix in error messages ----------


def test_github_error_strips_trailing_suffix() -> None:
    """GitHubError must strip any trailing '(GitHub NNN)' from the message."""
    err = GitHubError(404, "resource not found (GitHub 404)")
    assert err.status == 404
    assert "(GitHub 404)" not in err.message
    assert "resource not found" in err.message
    # The __str__ should not double-embed the prefix.
    assert str(err) == "GitHub 404: resource not found"


def test_github_error_no_suffix_unchanged() -> None:
    """GitHubError with a clean message must be stored as-is."""
    err = GitHubError(404, "resource not found")
    assert err.message == "resource not found"
    assert str(err) == "GitHub 404: resource not found"


def test_github_error_strips_suffix_with_whitespace() -> None:
    """Whitespace before the suffix is also removed."""
    err = GitHubError(422, "some error  (GitHub 422) ")
    assert "(GitHub" not in err.message
    assert "some error" in err.message


# ---------- Case 6: self-review 422 gets platform-restriction note -----------


def test_submit_pr_review_self_review_422_adds_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submit_pr_review 422 'Can not approve your own pull request' must include
    a platform-restriction note pointing the caller to use another account."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/reviews" in req.url.path:
            return _json(
                {"message": "Can not approve your own pull request"},
                status_code=422,
            )
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().submit_pr_review(
            _project(), token="t", pr_id="7", state="approve",
        )
    assert exc.value.status == 422
    assert "GitHub platform restriction" in exc.value.message
    assert "another account" in exc.value.message


def test_submit_pr_review_other_422_not_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submit_pr_review 422 with a different message must propagate unchanged."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/reviews" in req.url.path:
            return _json(
                {"message": "Unprocessable entity"},
                status_code=422,
            )
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().submit_pr_review(
            _project(), token="t", pr_id="7", state="approve",
        )
    assert exc.value.status == 422
    assert "GitHub platform restriction" not in exc.value.message


# ---------- Ticket #38 regression tests: error-message contract fixes ---------


def test_get_ticket_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_ticket on a missing ticket wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().get_ticket(_project(), token="t", ticket_id="7")
    assert exc.value.status == 404
    assert "ticket 'acme#7' not found" in exc.value.message


def test_add_pr_comment_404_names_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_pr_comment on a missing PR wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().add_pr_comment(
            _project(), token="t", pr_id="55", body="hi",
        )
    assert exc.value.status == 404
    assert "PR 'acme#55' not found" in exc.value.message


def test_submit_pr_review_404_names_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """submit_pr_review on a missing PR wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/reviews" in req.url.path:
            return _json({"message": "Not Found"}, status_code=404)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().submit_pr_review(
            _project(), token="t", pr_id="7", state="approve",
        )
    assert exc.value.status == 404
    assert "PR 'acme#7' not found" in exc.value.message


def test_update_pr_404_names_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_pr on a missing PR wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().update_pr(
            _project(), token="t", pr_id="99999", title="x",
        )
    assert exc.value.status == 404
    assert "PR 'acme#99999' not found" in exc.value.message


def test_create_pr_head_branch_missing_422_names_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr 422 with PullRequest.head invalid payload names the head branch."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/labels" in path and req.method == "GET":
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path.endswith("/pulls") and req.method == "POST":
            return _json(
                {
                    "message": "Validation Failed",
                    "errors": [
                        {"resource": "PullRequest", "field": "head", "code": "invalid"}
                    ],
                },
                status_code=422,
            )
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_pr(
            _project(),
            token="t",
            title="Test PR",
            body="Description.",
            head="my-feature",
            base="main",
        )
    assert exc.value.status == 422
    assert "my-feature" in exc.value.message
    assert "push it first" in exc.value.message


def test_create_pr_non_head_422_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_pr 422 that is NOT about the head branch propagates without enrichment."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/labels" in path and req.method == "GET":
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path.endswith("/pulls") and req.method == "POST":
            return _json(
                {"message": "a pull request already exists for this branch"},
                status_code=422,
            )
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_pr(
            _project(),
            token="t",
            title="Dup PR",
            body="Description.",
            head="feature",
            base="main",
        )
    assert exc.value.status == 422
    assert "push it first" not in exc.value.message


# ---------- Ticket #57: C6 — list_comments 404 names ticket -----------------


def test_list_comments_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_comments (asc/explicit-page path) on a missing ticket wraps the 404
    with the resource id so callers see 'ticket not found' rather than raw 404."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().list_comments(
            _project(), token="t", ticket_id="42", order="asc",
        )
    assert exc.value.status == 404
    assert "ticket 'acme#42' not found" in exc.value.message


def test_list_comments_tail_probe_404_names_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_comments (desc tail-probe path) on a missing ticket wraps the 404
    with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().list_comments(
            _project(), token="t", ticket_id="42", order="desc",
        )
    assert exc.value.status == 404
    assert "ticket 'acme#42' not found" in exc.value.message


# ---------- Ticket #57: R5 — add_relation 404 names the right resource -------


def _minimal_issue_payload(number: int, internal_id: int = 1001) -> dict:
    """Minimal GitHub issue payload accepted by _fetch_issue_internal_id."""
    return {
        "id": internal_id,
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "body": "",
        "user": {"login": "alice"},
        "labels": [],
        "assignees": [],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def test_add_relation_bogus_target_names_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation with a missing target issue produces 'target #NN not found'."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Target fetch (issue/99) → 404
        if "/issues/99" in req.url.path:
            return _json({"message": "Not Found"}, status_code=404)
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="child", target="#99",
        )
    assert exc.value.status == 404
    assert "target issue #99 not found in acme/backend" in exc.value.message


def test_add_relation_bogus_ticket_id_names_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(kind='parent') with a missing ticket_id produces
    'ticket acme#1 not found'."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Target (#99) resolves fine
        if path.endswith("/issues/99"):
            return _json(_minimal_issue_payload(99, internal_id=9009))
        # Source ticket (#1) → 404
        if path.endswith("/issues/1"):
            return _json({"message": "Not Found"}, status_code=404)
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="parent", target="#99",
        )
    assert exc.value.status == 404
    assert "ticket 'acme#1' not found" in exc.value.message


def test_add_relation_blocks_bogus_ticket_id_names_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(kind='blocks') with a missing ticket_id (source issue)
    produces 'ticket acme#1 not found'."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Target (#99) resolves fine
        if path.endswith("/issues/99"):
            return _json(_minimal_issue_payload(99, internal_id=9009))
        # Source ticket (#1) → 404
        if path.endswith("/issues/1"):
            return _json({"message": "Not Found"}, status_code=404)
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="blocks", target="#99",
        )
    assert exc.value.status == 404
    assert "ticket 'acme#1' not found" in exc.value.message
