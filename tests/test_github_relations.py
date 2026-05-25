"""Tests for relation enrichment on the GitHub `get_ticket` path.

We use `httpx.MockTransport` to intercept HTTP calls and return canned
responses; the provider is monkey-patched so `_client(token)` returns a
client backed by our mock transport.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import GitHubError, GitHubProvider
from lib_python_projects.providers.base import RelationAlreadyExists, RelationNotFound


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _issue_payload(number: int, **overrides) -> dict:
    base = {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Replace `github._client` so calls go through MockTransport.

    Returns a list that will be populated with every intercepted request,
    for assertion convenience.
    """
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        # Mirror the real headers so anything the provider inspects works.
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


# ---------- tests ------------------------------------------------------------


def test_no_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ticket with no parent, no children, and an empty timeline yields []."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    ticket, comments, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert ticket.id == "42"
    assert comments == []
    assert relations == []
    assert truncated is False


def test_parent_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue payload's `parent` field surfaces as a `parent` relation."""

    parent_payload = {
        "number": 7,
        "title": "Epic",
        "state": "open",
        "html_url": "https://github.com/acme/backend/issues/7",
        "repository": {"full_name": "acme/backend"},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42, parent=parent_payload))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert truncated is False
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "parent"
    assert rel.ticket_id == "#7"
    assert rel.title == "Epic"
    assert rel.url == "https://github.com/acme/backend/issues/7"
    assert rel.state == "open"
    assert rel.is_pull_request is False


def test_child_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-issues surface as `child` relations."""

    child_a = _issue_payload(101, title="Sub A")
    child_b = _issue_payload(102, title="Sub B", state="closed")
    # `repository` is included in sub_issues responses; mimic that.
    child_a["repository"] = {"full_name": "acme/backend"}
    child_b["repository"] = {"full_name": "acme/backend"}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([child_a, child_b])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert truncated is False
    kinds = [r.kind for r in relations]
    assert kinds == ["child", "child"]
    assert {r.ticket_id for r in relations} == {"#101", "#102"}
    closed_child = next(r for r in relations if r.ticket_id == "#102")
    assert closed_child.state == "closed"


def test_pr_closes_via_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `connected` timeline event whose source is a merged PR yields `closed_by`."""

    pr_source = {
        "number": 55,
        "title": "Fix bug",
        "state": "closed",
        "merged_at": "2024-02-01T12:00:00Z",
        "html_url": "https://github.com/acme/backend/pull/55",
        "pull_request": {"url": "https://api.github.com/repos/acme/backend/pulls/55"},
        "repository": {"full_name": "acme/backend"},
    }
    timeline = [
        {
            "event": "connected",
            "source": {"type": "issue", "issue": pr_source},
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(timeline)
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="42")
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "closed_by"
    assert rel.ticket_id == "#55"
    assert rel.title == "Fix bug"
    assert rel.state == "merged"
    assert rel.is_pull_request is True


def test_duplicate_via_marked_as_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `marked_as_duplicate` event resolves direction from `canonical`/`dupe`."""

    canonical = _issue_payload(9, title="Canonical")
    canonical["repository"] = {"full_name": "acme/backend"}
    # This issue (42) is marked as duplicate of #9. The event on 42's
    # timeline therefore has `canonical=#9` and `dupe=#42` (this one).
    timeline = [
        {
            "event": "marked_as_duplicate",
            "canonical": canonical,
            "dupe": _issue_payload(42),
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(timeline)
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="42")
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "duplicate_of"
    assert rel.ticket_id == "#9"


def test_cross_repo_cross_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-referenced event from a different repo yields `owner/repo#N`."""

    source = {
        "number": 3,
        "title": "Mentioned over here",
        "state": "open",
        "html_url": "https://github.com/other-org/other-repo/issues/3",
        "repository": {"full_name": "other-org/other-repo"},
    }
    timeline = [
        {
            "event": "cross-referenced",
            "source": {"type": "issue", "issue": source},
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(timeline)
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="42")
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "mentioned_by"
    assert rel.ticket_id == "other-org/other-repo#3"
    assert rel.url == "https://github.com/other-org/other-repo/issues/3"
    assert rel.is_pull_request is False


def test_truncation_flag_when_link_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeline response that advertises `rel=\"next\"` sets relations_truncated=True."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(
                [],
                headers={
                    "Link": (
                        '<https://api.github.com/repos/acme/backend/issues/42/'
                        'timeline?page=2>; rel="next", '
                        '<https://api.github.com/repos/acme/backend/issues/42/'
                        'timeline?page=5>; rel="last"'
                    )
                },
            )
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert relations == []
    assert truncated is True


def test_sub_issues_404_falls_back_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 from `/sub_issues` (older GHES) is silently treated as empty."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json({"message": "Not Found"}, status_code=404)
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert relations == []
    assert truncated is False


def test_include_relations_false_skips_extra_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """`include_relations=False` avoids the sub-issues and timeline requests."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        # If we reach this branch with include_relations=False, the test
        # should fail loudly.
        raise AssertionError(
            f"unexpected extra request when include_relations=False: {req.url}"
        )

    seen = _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42", include_relations=False
    )
    assert relations == []
    assert truncated is None
    # We expect exactly two calls: the issue and the comments.
    paths = [r.url.path for r in seen]
    assert paths == [
        "/repos/acme/backend/issues/42",
        "/repos/acme/backend/issues/42/comments",
    ]


# ---------- new relation kinds (ticket #5) ---------------------------------


def test_outgoing_mentions_from_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """`#N` refs in the ticket's own body surface as outgoing `mentions`."""

    body = "This issue references #11 and other/repo#22."

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/3":
            return _json(_issue_payload(3, body=body))
        if path == "/repos/acme/backend/issues/3/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/3/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/3/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="3"
    )
    kinds = sorted(r.kind for r in relations)
    ticket_ids = sorted(r.ticket_id for r in relations)
    assert kinds == ["mentions", "mentions"]
    assert ticket_ids == ["#11", "other/repo#22"]


def test_outgoing_closes_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    """`closes #N` in a PR body emits a `closes` relation (not `mentions`)."""

    body = "Fixes #2 — see other/repo#7 for context."

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, body=body))
        if path == "/repos/acme/backend/issues/5/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/5/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/5/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="5"
    )
    by_kind = {(r.kind, r.ticket_id) for r in relations}
    assert ("closes", "#2") in by_kind
    # #2 should be promoted to `closes`, NOT also surface as `mentions`.
    assert ("mentions", "#2") not in by_kind
    # `other/repo#7` is a plain mention (no closing kw).
    assert ("mentions", "other/repo#7") in by_kind


def test_duplicate_of_from_own_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed-as-duplicate ticket emits `duplicate_of` from its body."""

    body = "Duplicate of #1"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/4":
            return _json(_issue_payload(
                4, body=body, state="closed", state_reason="duplicate",
            ))
        if path == "/repos/acme/backend/issues/4/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/4/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/4/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="4"
    )
    by_kind = {(r.kind, r.ticket_id) for r in relations}
    assert ("duplicate_of", "#1") in by_kind


def test_duplicate_of_no_extra_mentions_from_body_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body marker 'Duplicate of #1' must not produce a spurious 'mentions' entry.

    After add_relation(kind="duplicate_of") the body contains a
    'Duplicate of #1' line.  _dedupe_relations must suppress the 'mentions'
    entry that the plain-mention scanner would otherwise emit for the same
    target.
    """
    body = "Duplicate of #1"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(
                5, body=body, state="closed", state_reason="duplicate",
            ))
        if path == "/repos/acme/backend/issues/5/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/5/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/5/timeline":
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="5"
    )
    # Exactly one relation entry for #1, with kind "duplicate_of".
    entries_for_target = [(r.kind, r.ticket_id) for r in relations if r.ticket_id == "#1"]
    assert ("duplicate_of", "#1") in [(r.kind, r.ticket_id) for r in relations]
    # No spurious "mentions" for the same target.
    assert not any(r.kind == "mentions" and r.ticket_id == "#1" for r in relations)
    # Exactly one entry for target #1 total.
    assert len(entries_for_target) == 1


def test_duplicated_by_relabel_from_cross_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-ref from a closed-as-duplicate source becomes `duplicated_by`."""

    source = {
        "number": 4,
        "title": "Dup of #1",
        "state": "closed",
        "state_reason": "duplicate",
        "body": "Duplicate of #1",
        "html_url": "https://github.com/acme/backend/issues/4",
        "repository": {"full_name": "acme/backend"},
    }
    timeline = [
        {"event": "cross-referenced", "source": {"type": "issue", "issue": source}}
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/1":
            return _json(_issue_payload(1))
        if path == "/repos/acme/backend/issues/1/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/1/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/1/timeline":
            return _json(timeline)
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="1"
    )
    kinds = {r.kind for r in relations}
    assert "duplicated_by" in kinds
    # `mentioned_by` for the same target should be dropped by dedupe.
    assert not any(
        r.kind == "mentioned_by" and r.ticket_id == "#4" for r in relations
    )


def test_closed_by_relabel_from_merged_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-ref from a merged PR with `closes #N` becomes `closed_by`."""

    source = {
        "number": 5,
        "title": "Implement fix",
        "state": "closed",
        "merged_at": "2024-03-01T12:00:00Z",
        "body": "Closes #2",
        "html_url": "https://github.com/acme/backend/pull/5",
        "pull_request": {
            "url": "https://api.github.com/repos/acme/backend/pulls/5",
            "merged_at": "2024-03-01T12:00:00Z",
        },
        "repository": {"full_name": "acme/backend"},
    }
    timeline = [
        {"event": "cross-referenced", "source": {"type": "issue", "issue": source}}
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/2":
            return _json(_issue_payload(2))
        if path == "/repos/acme/backend/issues/2/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/2/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/2/timeline":
            return _json(timeline)
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="2"
    )
    by_kind = {(r.kind, r.ticket_id) for r in relations}
    assert ("closed_by", "#5") in by_kind
    assert ("mentioned_by", "#5") not in by_kind


def test_blocks_blocked_by_via_dependencies_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #41 read↔write symmetry: dependencies persisted via the
    REST API (`POST /issues/{n}/dependencies/blocked_by`) MUST surface
    on `get_ticket.relations` as typed `blocks` / `blocked_by` kinds.

    Reproduces the tester's smoke-test finding (commit 2a4c9eb): the
    write side persisted to the Dependencies API but the read side
    only saw timeline events, which the new API no longer emits.
    """
    blocker = {
        "number": 3,
        "title": "Blocker",
        "state": "open",
        "html_url": "https://github.com/acme/backend/issues/3",
        "repository": {"full_name": "acme/backend"},
    }
    blocked = {
        "number": 9,
        "title": "Downstream",
        "state": "open",
        "html_url": "https://github.com/acme/backend/issues/9",
        "repository": {"full_name": "acme/backend"},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/2":
            return _json(_issue_payload(2))
        if path == "/repos/acme/backend/issues/2/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/2/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/2/dependencies/blocked_by":
            return _json([blocker])
        if path == "/repos/acme/backend/issues/2/dependencies/blocking":
            return _json([blocked])
        if path == "/repos/acme/backend/issues/2/timeline":
            # Authoritative source is the REST API — timeline empty.
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="2"
    )
    by_kind = {(r.kind, r.ticket_id) for r in relations}
    assert ("blocked_by", "#3") in by_kind
    assert ("blocks", "#9") in by_kind


def test_dependencies_api_404_does_not_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older GitHub installations don't have the Dependencies endpoints
    — 404 must be tolerated, other relation kinds keep flowing."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/2":
            return _json(_issue_payload(2))
        if path == "/repos/acme/backend/issues/2/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/2/sub_issues":
            return _json([])
        if path.startswith(
            "/repos/acme/backend/issues/2/dependencies/"
        ):
            return _json({}, status_code=404)
        if path == "/repos/acme/backend/issues/2/timeline":
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="2"
    )
    # Empty is fine — no crash is the contract.
    assert relations == []


def test_blocks_blocked_by_dedupe_across_dependencies_and_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a dependency surfaces on BOTH the REST API and a legacy
    timeline event, the deduper collapses to a single Relation."""

    blocker = {
        "number": 3,
        "title": "Blocker",
        "state": "open",
        "html_url": "https://github.com/acme/backend/issues/3",
        "repository": {"full_name": "acme/backend"},
    }
    timeline = [
        {"event": "blocked_by_added", "blocked_by_issue": blocker},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/2":
            return _json(_issue_payload(2))
        if path == "/repos/acme/backend/issues/2/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/2/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/2/dependencies/blocked_by":
            return _json([blocker])
        if path == "/repos/acme/backend/issues/2/dependencies/blocking":
            return _json([])
        if path == "/repos/acme/backend/issues/2/timeline":
            return _json(timeline)
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="2"
    )
    blocked_by = [r for r in relations if r.kind == "blocked_by"]
    assert len(blocked_by) == 1
    assert blocked_by[0].ticket_id == "#3"


def test_blocks_blocked_by_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue-Dependencies timeline events emit `blocks` / `blocked_by`."""

    blocker = {
        "number": 3,
        "title": "Blocker",
        "state": "open",
        "html_url": "https://github.com/acme/backend/issues/3",
        "repository": {"full_name": "acme/backend"},
    }
    timeline = [
        {"event": "blocked_by_added", "blocked_by_issue": blocker},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/2":
            return _json(_issue_payload(2))
        if path == "/repos/acme/backend/issues/2/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/2/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/2/timeline":
            return _json(timeline)
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="2"
    )
    by_kind = {(r.kind, r.ticket_id) for r in relations}
    assert ("blocked_by", "#3") in by_kind


def test_mentions_scan_depth_body_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PROJECT_ISSUES_MENTIONS_SCAN_DEPTH=0` skips comment scanning."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/9":
            return _json(_issue_payload(9, body="see #11"))
        if path == "/repos/acme/backend/issues/9/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/9/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/9/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    seen = _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="9"
    )
    # The body mention is the only #11 reference.
    assert any(r.kind == "mentions" and r.ticket_id == "#11" for r in relations)
    # Comments endpoint should still be called once by `get_ticket` itself,
    # but NOT a second time by the scanner (depth=0). The scanner-call
    # would use ?per_page=N with N>0; the get_ticket call uses per_page=100.
    comment_paths = [
        r for r in seen
        if r.url.path == "/repos/acme/backend/issues/9/comments"
    ]
    assert len(comment_paths) == 1


def test_self_reference_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `#N` in the body that matches the ticket's own number is dropped."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42, body="this issue #42 and also #99"))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if "/dependencies/" in path:
            # Ticket #41 read-path: empty Dependencies API responses
            # keep these legacy fixtures focused on their original kind.
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    ticket_ids = {r.ticket_id for r in relations}
    assert "#99" in ticket_ids
    assert "#42" not in ticket_ids


# ---------- F4: remove_relation raises RelationNotFound ---------------------


def test_remove_relation_blocked_by_not_found_raises_relation_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a `blocked_by` link that doesn't exist raises RelationNotFound."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Return a valid issue payload for internal-id resolution.
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, id=5001))
        if path == "/repos/acme/backend/issues/3":
            return _json(_issue_payload(3, id=3001))
        # blocked_by list is empty — link doesn't exist.
        if path == "/repos/acme/backend/issues/5/dependencies/blocked_by":
            return _json([])
        raise AssertionError(f"unexpected {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationNotFound) as exc:
        GitHubProvider().remove_relation(
            _project(), token="t", ticket_id="5", kind="blocked_by", target="#3"
        )
    assert exc.value.kind == "blocked_by"
    assert exc.value.ticket_id == "5"
    assert "#3" in exc.value.target
    # Must also be a LookupError subclass for _safe wrapper compatibility.
    assert isinstance(exc.value, LookupError)


def test_remove_relation_blocks_not_found_raises_relation_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a `blocks` link that doesn't exist raises RelationNotFound."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, id=5001))
        if path == "/repos/acme/backend/issues/3":
            return _json(_issue_payload(3, id=3001))
        # source issue (#5) has no blocked_by links.
        if path == "/repos/acme/backend/issues/3/dependencies/blocked_by":
            return _json([])
        raise AssertionError(f"unexpected {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationNotFound) as exc:
        GitHubProvider().remove_relation(
            _project(), token="t", ticket_id="5", kind="blocks", target="#3"
        )
    assert exc.value.kind == "blocks"
    assert isinstance(exc.value, LookupError)
    assert exc.value.target == "#3"


def test_remove_relation_child_not_found_raises_relation_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a `child` sub-issue that doesn't exist raises RelationNotFound."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/9":
            return _json(_issue_payload(9, id=9001))
        if path == "/repos/acme/backend/issues/5/sub_issue":
            # GitHub returns 404 when the sub-issue relationship doesn't exist.
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationNotFound) as exc:
        GitHubProvider().remove_relation(
            _project(), token="t", ticket_id="5", kind="child", target="#9"
        )
    assert exc.value.kind == "child"
    assert isinstance(exc.value, LookupError)


def test_remove_relation_parent_not_found_raises_relation_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a `parent` relation where the target doesn't exist raises RelationNotFound."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # The target (parent) issue returns 404 — it doesn't exist.
        if path == "/repos/acme/backend/issues/7":
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationNotFound) as exc:
        GitHubProvider().remove_relation(
            _project(), token="t", ticket_id="5", kind="parent", target="#7"
        )
    assert exc.value.kind == "parent"
    assert "#7" in exc.value.target
    assert isinstance(exc.value, LookupError)


# ---------- F6: resolved field on relations -----------------------------------


def test_body_scan_relations_have_resolved_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relations from body scanning carry `resolved=False`."""
    body = "This closes #11 and mentions other/repo#22."

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/3":
            return _json(_issue_payload(3, body=body))
        if path == "/repos/acme/backend/issues/3/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/3/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/3/timeline":
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="3")
    body_scan_rels = [r for r in relations if r.kind in ("closes", "mentions")]
    assert body_scan_rels, "expected body-scan relations"
    for rel in body_scan_rels:
        assert rel.resolved is False, f"{rel.kind} should have resolved=False"


def test_api_fetched_relations_have_resolved_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relations fetched via API (parent, child, blocks, blocked_by) carry `resolved=True`."""
    parent_payload = {
        "number": 7, "title": "Epic", "state": "open",
        "html_url": "https://github.com/acme/backend/issues/7",
        "repository": {"full_name": "acme/backend"},
    }
    child = _issue_payload(101, title="Sub A")
    child["repository"] = {"full_name": "acme/backend"}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42, parent=parent_payload))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([child])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="42")
    api_rels = [r for r in relations if r.kind in ("parent", "child")]
    assert api_rels, "expected API-fetched relations"
    for rel in api_rels:
        assert rel.resolved is True, f"{rel.kind} should have resolved=True"


# ---------- Defect 2: _list_comments_tail has_more off-by-one ----------------


def _comment_payload(comment_id: int) -> dict:
    return {
        "id": comment_id,
        "user": {"login": "alice"},
        "body": f"comment {comment_id}",
        "html_url": f"https://github.com/acme/backend/issues/42#issuecomment-{comment_id}",
        "created_at": f"2024-01-0{comment_id}T00:00:00Z",
    }


def test_list_comments_desc_has_more_true_when_older_pages_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 comments across 2 pages (limit=2, order=desc): has_more=True.

    Scenario (per_page derived from limit=2):
      Page 1: [comment1, comment2]
      Page 2: [comment3]
    Walking backwards: fetch page 2 (1 item), fetch page 1 (2 items).
    collected = [comment1, comment2, comment3] — 3 items > limit=2.
    Returned tail is [comment3, comment2] (newest-first).
    has_more must be True because comment1 exists but was trimmed.
    """
    base_url = "https://api.github.com/repos/acme/backend/issues/42/comments"
    link_last_2 = (
        f'<{base_url}?per_page=2&page=1>; rel="first", '
        f'<{base_url}?per_page=2&page=2>; rel="last"'
    )

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        if page == 1:
            return _json(
                [_comment_payload(1), _comment_payload(2)],
                headers={"Link": link_last_2},
            )
        if page == 2:
            return _json([_comment_payload(3)])
        raise AssertionError(f"unexpected page {page}")

    _install_mock(monkeypatch, handler)
    comments, has_more = GitHubProvider().list_comments(
        _project(), token="t", ticket_id="42", limit=2, order="desc",
    )
    assert has_more is True
    assert [c.id for c in comments] == ["3", "2"]


def test_list_comments_desc_has_more_false_when_all_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 comments on 1 page (limit=2, order=desc): has_more=False.

    Single-page path: probe returns no 'last' link; all comments returned.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        return _json([_comment_payload(1), _comment_payload(2)])

    _install_mock(monkeypatch, handler)
    comments, has_more = GitHubProvider().list_comments(
        _project(), token="t", ticket_id="42", limit=2, order="desc",
    )
    assert has_more is False
    assert [c.id for c in comments] == ["2", "1"]


# ---------- Defect 3: empty body raises ValueError (GitHub) ------------------


def test_add_comment_empty_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_comment with body='' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitHubProvider().add_comment(_project(), token="t", ticket_id="42", body="")


def test_add_comment_whitespace_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_comment with body='   ' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitHubProvider().add_comment(_project(), token="t", ticket_id="42", body="   ")


def test_update_comment_empty_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment with body='' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitHubProvider().update_comment(
            _project(), token="t", comment_id="99", body="", ticket_id="42",
        )


def test_update_comment_whitespace_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment with body='   ' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitHubProvider().update_comment(
            _project(), token="t", comment_id="99", body="   ", ticket_id="42",
        )


# ---------- merge_pr tests ---------------------------------------------------


def _pr_payload(number: int, **overrides) -> dict:
    """Build a minimal full-PR payload (as returned by `/pulls` or `/pulls/{n}`)."""
    base: dict = {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "mergeable": None,
        "mergeable_state": "clean",
        "merge_commit_sha": None,
        "auto_merge": None,
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "requested_reviewers": [],
        "head": {
            "ref": "feat/branch",
            "sha": "abc123",
            "repo": {"full_name": "acme/backend"},
        },
        "base": {
            "ref": "main",
            "sha": "def456",
        },
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_merge_pr_success_returns_merged_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: PUT /pulls/7/merge returns 200, re-fetch returns merged PR.
    merge_pr must return a PullRequest with status='merged' and merged=True.
    """
    merged_payload = _pr_payload(
        7,
        state="closed",
        merged=True,
        merged_at="2024-05-01T12:00:00Z",
        merge_commit_sha="deadbeef",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and "/pulls/7/merge" in req.url.path:
            return _json({"sha": "deadbeef", "merged": True, "message": "Pull Request successfully merged"})
        if req.method == "GET" and "/pulls/7" in req.url.path:
            return _json(merged_payload)
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    pr = GitHubProvider().merge_pr(_project(), token="t", pr_id="7")
    assert pr.status == "merged"
    assert pr.merged is True
    assert pr.id == "7"


def test_merge_pr_already_merged_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub returns HTTP 405 when merging an already-merged PR.
    The provider must re-raise as GitHubError(405, '... already merged').
    The disambiguation probe GET returns merged=True.
    """
    from lib_python_projects.providers.github import GitHubError

    already_merged_payload = _pr_payload(
        7,
        state="closed",
        merged=True,
        merged_at="2024-05-01T12:00:00Z",
        mergeable_state="unknown",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and "/pulls/7/merge" in req.url.path:
            return _json({"message": "Pull Request is not mergeable"}, status_code=405)
        if req.method == "GET" and "/pulls/7" in req.url.path:
            return _json(already_merged_payload)
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().merge_pr(_project(), token="t", pr_id="7")
    assert exc.value.status == 405
    assert "already merged" in exc.value.message
    assert "acme#7" in exc.value.message


def test_merge_pr_405_conflict_raises_unmergeable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: GitHub returns 405 for a PR with a merge conflict.
    The provider must raise GitHubError(405) describing the unmergeable state,
    NOT the misleading 'already merged' message.
    """
    from lib_python_projects.providers.github import GitHubError

    conflict_payload = _pr_payload(
        7,
        state="open",
        merged=False,
        mergeable=False,
        mergeable_state="dirty",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and "/pulls/7/merge" in req.url.path:
            return _json({"message": "Pull Request is not mergeable"}, status_code=405)
        if req.method == "GET" and "/pulls/7" in req.url.path:
            return _json(conflict_payload)
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().merge_pr(_project(), token="t", pr_id="7")
    assert exc.value.status == 405
    assert "cannot be merged" in exc.value.message
    assert "dirty" in exc.value.message
    assert "rebase" in exc.value.message
    assert "acme#7" in exc.value.message


def test_merge_pr_405_unknown_state_raises_conflict_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub returns 405 and the probe GET returns merged=False with
    mergeable_state=None (unknown).  The provider must raise GitHubError(405)
    describing 'cannot be merged' and 'unknown' state.
    """
    from lib_python_projects.providers.github import GitHubError

    unknown_state_payload = _pr_payload(
        7,
        state="open",
        merged=False,
        mergeable=None,
        mergeable_state=None,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and "/pulls/7/merge" in req.url.path:
            return _json({"message": "Pull Request is not mergeable"}, status_code=405)
        if req.method == "GET" and "/pulls/7" in req.url.path:
            return _json(unknown_state_payload)
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().merge_pr(_project(), token="t", pr_id="7")
    assert exc.value.status == 405
    assert "cannot be merged" in exc.value.message
    assert "unknown" in exc.value.message
    assert "acme#7" in exc.value.message


# ---------- Case 3: add_relation already-exists / cycle normalization --------


def test_add_relation_child_duplicate_sub_issue_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation 'child' hitting a duplicate-sub-issue 422 must raise
    RelationAlreadyExists with kind and target info."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Target issue resolution (for internal id).
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, id=5001))
        # Sub-issues POST → duplicate 422.
        if "/sub_issues" in path and req.method == "POST":
            return _json(
                {
                    "message": "Issue may not contain duplicate sub-issues",
                    "errors": [{"message": "Issue may not contain duplicate sub-issues"}],
                },
                status_code=422,
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="child", target="#5"
        )
    assert exc.value.kind == "child"
    assert "#5" in exc.value.target
    assert isinstance(exc.value, ValueError)


def test_add_relation_blocked_by_cycle_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation 'blocked_by' hitting a cycle-would-be-created 422 must
    surface 'relation would create a cycle' with kind and target."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, id=5001))
        if "/dependencies/blocked_by" in path and req.method == "POST":
            return _json(
                {
                    "message": "this dependency would create a cycle",
                    "errors": [{"message": "this dependency would create a cycle"}],
                },
                status_code=422,
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="blocked_by", target="#5"
        )
    assert exc.value.status == 422
    assert "cycle" in exc.value.message
    assert "blocked_by" in exc.value.message
    assert "#5" in exc.value.message


# ---------- Defects from ticket #37 -----------------------------------------


def test_add_relation_child_duplicate_raises_relation_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation 'child' hitting a duplicate-sub-issue 422 must raise
    RelationAlreadyExists (a ValueError), not a raw GitHubError."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Target issue resolution (for internal id).
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, id=5001))
        # Sub-issues POST → duplicate 422.
        if "/sub_issues" in path and req.method == "POST":
            return _json(
                {
                    "message": "Issue may not contain duplicate sub-issues",
                    "errors": [{"message": "Issue may not contain duplicate sub-issues"}],
                },
                status_code=422,
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="child", target="#5"
        )
    assert exc.value.kind == "child"
    assert "#5" in exc.value.target
    # Must be a ValueError subclass for _safe wrapper compatibility.
    assert isinstance(exc.value, ValueError)


def test_add_relation_blocked_by_duplicate_raises_relation_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation 'blocked_by' hitting an already-exists 422 must raise
    RelationAlreadyExists, not a raw GitHubError."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, id=5001))
        if "/dependencies/blocked_by" in path and req.method == "POST":
            return _json(
                {
                    "message": "Dependency already assigned",
                    "errors": [{"message": "Dependency already assigned"}],
                },
                status_code=422,
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="blocked_by", target="#5"
        )
    assert exc.value.kind == "blocked_by"
    assert "#5" in exc.value.target
    assert isinstance(exc.value, ValueError)


def test_add_relation_self_relation_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation with ticket_id == target must raise ValueError with
    'self-relation' in the message — no HTTP call should be made."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for self-relation: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="self-relation"):
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="5", kind="child", target="#5"
        )


def test_add_relation_self_relation_github_no_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-relation guard fires when target is given without '#' prefix."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for self-relation: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="self-relation"):
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="5", kind="child", target="5"
        )


def test_duplicate_of_from_body_without_state_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_ticket for a ticket with 'Duplicate of #1' in body and state='open'
    (no state_reason) must surface ('duplicate_of', '#1') in relations,
    and '#1' must NOT appear as a plain 'mentions' entry."""

    body = "Duplicate of #1"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/4":
            return _json(_issue_payload(
                4, body=body, state="open",
                # No state_reason — the old gated-path would suppress this.
            ))
        if path == "/repos/acme/backend/issues/4/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/4/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/4/timeline":
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="4")
    by_kind = {(r.kind, r.ticket_id) for r in relations}
    # duplicate_of must be present even without state_reason='duplicate'.
    assert ("duplicate_of", "#1") in by_kind, (
        f"expected duplicate_of #1, got {by_kind}"
    )
    # The '#1' in the 'Duplicate of #1' text must NOT also appear as 'mentions'.
    assert ("mentions", "#1") not in by_kind, (
        f"'#1' must not also surface as 'mentions'; got {by_kind}"
    )


def test_duplicate_of_from_body_with_state_reason_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When body contains 'Duplicate of #1' AND state_reason='duplicate',
    exactly one duplicate_of relation for #1 results (no double)."""

    body = "Duplicate of #1"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/4":
            return _json(_issue_payload(
                4, body=body, state="closed", state_reason="duplicate",
            ))
        if path == "/repos/acme/backend/issues/4/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/4/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/4/timeline":
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="4")
    dup_rels = [(r.kind, r.ticket_id) for r in relations if r.ticket_id == "#1"]
    assert ("duplicate_of", "#1") in dup_rels, (
        f"expected duplicate_of #1, got {dup_rels}"
    )
    # Exactly one entry for #1 (no double due to both scan paths firing).
    assert len(dup_rels) == 1, (
        f"expected exactly 1 relation for #1, got {len(dup_rels)}: {dup_rels}"
    )


# ---------- Regression: remove_relation("duplicate_of") strips body marker ----


def test_remove_relation_duplicate_of_strips_body_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ticket #37 finding 1: remove_relation('duplicate_of')
    must strip the 'Duplicate of #N' body line (not just reopen the issue).

    Contract: after removal, get_ticket must not report duplicate_of because
    the body is the sole source of truth — reopening alone would leave the
    marker and keep reporting the relation.
    """
    # The issue body as written by add_relation("duplicate_of").
    original_body = "Duplicate of #99\n\nOriginal description."
    patched_bodies: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Target issue resolution for internal-id lookup.
        if path == "/repos/acme/backend/issues/99":
            return _json(_issue_payload(99, id=9901))
        # Source issue GET (to read current body before patching).
        if path == "/repos/acme/backend/issues/10" and req.method == "GET":
            return _json(_issue_payload(
                10, id=1001,
                body=original_body,
                state="closed",
                state_reason="duplicate",
                labels=[],
            ))
        # PATCH to strip body + reopen.
        if path == "/repos/acme/backend/issues/10" and req.method == "PATCH":
            import json as _json_mod
            payload = _json_mod.loads(req.content)
            patched_bodies.append(payload.get("body", ""))
            assert payload.get("state") == "open", (
                "remove_relation must reopen the issue"
            )
            return _json(_issue_payload(10, state="open", body=payload.get("body", "")))
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().remove_relation(
        _project(), token="t", ticket_id="10", kind="duplicate_of", target="#99"
    )
    assert result == {"removed": True}

    # Confirm the PATCH was issued and the body no longer contains the marker.
    assert patched_bodies, "expected at least one PATCH body"
    final_body = patched_bodies[-1]
    assert "Duplicate of #99" not in final_body, (
        f"marker must be stripped from body; got: {final_body!r}"
    )
    # The original description should be preserved.
    assert "Original description." in final_body, (
        f"original description must be kept; got: {final_body!r}"
    )


# ---------- Regression: add_relation('blocks') duplicate raises correct id ----


def test_add_relation_blocks_duplicate_raises_relation_already_exists_with_caller_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ticket #37 finding 2 & 3: add_relation('blocks') hitting
    a duplicate 422 must raise RelationAlreadyExists with ticket_id equal to
    the *caller's* logical source ticket (A), not the wire-level source (B).

    For blocks(A→B), the wire call is POST /issues/B/dependencies/blocked_by,
    so source_issue_number on the wire is B.  Before the fix, the exception
    reported ticket_id=B; it must report ticket_id=A (the caller).
    """
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Resolve target issue (B = #5) for internal id.
        if path == "/repos/acme/backend/issues/5":
            return _json(_issue_payload(5, id=5001))
        # Resolve caller issue (A = #1) for internal id.
        if path == "/repos/acme/backend/issues/1":
            return _json(_issue_payload(1, id=1001))
        # Wire POST is to B's endpoint; return duplicate 422.
        if "/repos/acme/backend/issues/5/dependencies/blocked_by" in path and req.method == "POST":
            return _json(
                {
                    "message": "Dependency already assigned",
                    "errors": [{"message": "Dependency already assigned"}],
                },
                status_code=422,
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="1", kind="blocks", target="#5"
        )
    # ticket_id must be the caller's logical source (A="1"), NOT the wire source (B="5").
    assert exc.value.ticket_id == "1", (
        f"expected ticket_id='1' (caller's A), got {exc.value.ticket_id!r}"
    )
    assert exc.value.kind == "blocks"
    assert "#5" in exc.value.target
    assert isinstance(exc.value, ValueError)


def test_add_relation_parent_duplicate_raises_relation_already_exists_with_caller_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation('parent') hitting a duplicate 422 must raise
    RelationAlreadyExists with ticket_id equal to the caller's logical
    source ticket (A), not the wire-level parent (B).
    """
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Resolve target (parent, B = #7) for internal id.
        if path == "/repos/acme/backend/issues/7":
            return _json(_issue_payload(7, id=7001))
        # Resolve caller (child, A = #3) for internal id.
        if path == "/repos/acme/backend/issues/3":
            return _json(_issue_payload(3, id=3001))
        # Wire POST is to B's sub_issues endpoint; return duplicate 422.
        if "/repos/acme/backend/issues/7/sub_issues" in path and req.method == "POST":
            return _json(
                {
                    "message": "Issue may not contain duplicate sub-issues",
                    "errors": [{"message": "Issue may not contain duplicate sub-issues"}],
                },
                status_code=422,
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        GitHubProvider().add_relation(
            _project(), token="t", ticket_id="3", kind="parent", target="#7"
        )
    # ticket_id must be the caller's logical source (A="3").
    assert exc.value.ticket_id == "3", (
        f"expected ticket_id='3' (caller's A), got {exc.value.ticket_id!r}"
    )
    assert exc.value.kind == "parent"
    assert "#7" in exc.value.target
    assert isinstance(exc.value, ValueError)


# ---------- Regression: body prose must not be misclassified as duplicate_of --


def test_duplicate_of_body_prose_not_misclassified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ticket #37: 'duplicate of #N' embedded in prose must NOT
    yield a duplicate_of relation — only a dedicated marker line qualifies.

    The read-path regex must be line-anchored so sentences like
    "This is not a duplicate of #12, see discussion." do not produce a
    false-positive duplicate_of entry.  The #12 reference may still surface
    as a plain 'mentions' relation.
    """
    # Prose mention mid-sentence — must NOT trigger duplicate_of.
    body = "This is not a duplicate of #12, see discussion."

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/7":
            return _json(_issue_payload(7, body=body, state="open"))
        if path == "/repos/acme/backend/issues/7/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/7/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/7/timeline":
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="7")

    # No duplicate_of relation must appear.
    assert not any(r.kind == "duplicate_of" for r in relations), (
        f"prose 'duplicate of #12' must not yield duplicate_of; got {relations}"
    )
    # #12 may still appear as a plain mentions entry (body scan picks it up).
    # We only assert there is no duplicate_of — not that mentions is absent.


def test_duplicate_of_dedicated_line_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complementary check: a body where 'Duplicate of #N' is its own line
    (as written by add_relation) still produces a duplicate_of relation,
    confirming the line-anchor does not over-reject valid markers.
    """
    # Marker on its own line followed by additional prose.
    body = "Duplicate of #1\n\nSome other context here."

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/8":
            return _json(_issue_payload(8, body=body, state="open"))
        if path == "/repos/acme/backend/issues/8/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/8/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/8/timeline":
            return _json([])
        if "/dependencies/" in path:
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="8")

    assert any(r.kind == "duplicate_of" and r.ticket_id == "#1" for r in relations), (
        f"dedicated marker line must yield duplicate_of #1; got {relations}"
    )
