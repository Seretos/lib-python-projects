"""Tests for the GitLab provider's issue + comment surface.

Covers:
- `list_tickets` filter param translation (state, labels, not_labels,
  assignee, author, search, dates, sort)
- `get_ticket` issue fetch + system-note filtering
- `create_ticket` ai-generated marker (body prefix + label) and
  assignee resolution
- `update_ticket` state_event mapping, label add/remove, ai-modified
  heuristic, assignee delta
- `add_comment`, `list_comments`, `get_comment`, `update_comment`
  composite-key handling
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.base import TicketFilters
from lib_python_projects.providers.gitlab import GitLabError, GitLabProvider


def _project(path: str = "acme/backend") -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="gitlab",
        path=path,
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
        headers = {
            "Accept": "application/json",
            "User-Agent": "test-agent",
        }
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


def _issue_payload(iid: int, **overrides) -> dict:
    base = {
        "iid": iid,
        "title": f"Issue {iid}",
        "description": "body",
        "state": "opened",
        "author": {"username": "alice"},
        "assignees": [],
        "labels": [],
        "web_url": f"https://gitlab.com/acme/backend/-/issues/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


# ---------- list_tickets -----------------------------------------------------


def test_list_tickets_default_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        # Path uses URL-encoded project path.
        assert "acme%2Fbackend" in str(req.url)
        # Defaults: state=opened, per_page=30, sort=desc, order_by=created_at.
        assert req.url.params.get("state") == "opened"
        assert req.url.params.get("per_page") == "30"
        assert req.url.params.get("order_by") == "created_at"
        assert req.url.params.get("sort") == "desc"
        # No filter params set in the default case.
        assert "labels" not in req.url.params
        assert "assignee_username" not in req.url.params
        return _json([_issue_payload(1), _issue_payload(2)])

    _install_mock(monkeypatch, handler)
    tickets = GitLabProvider().list_tickets(
        _project(), token="tok", filters=TicketFilters(),
    )
    assert len(tickets) == 2
    assert tickets[0].id == "1"


def test_list_tickets_state_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_states: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_states.append(req.url.params.get("state"))
        return _json([])

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().list_tickets(p, "t", TicketFilters(status="open"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(status="closed"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(status="any"))
    assert seen_states == ["opened", "closed", "all"]


def test_list_tickets_label_and_assignee_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("labels") == "bug,p1"
        assert req.url.params.get("not[labels]") == "wontfix"
        assert req.url.params.get("assignee_username") == "alice"
        assert req.url.params.get("author_username") == "bob"
        assert req.url.params.get("search") == "memory leak"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t",
        TicketFilters(
            labels=["bug", "p1"],
            not_labels=["wontfix"],
            assignee="alice",
            author="bob",
            search="memory leak",
        ),
    )


def test_list_tickets_date_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("created_after") == "2026-01-01"
        assert req.url.params.get("created_before") == "2026-12-31"
        assert req.url.params.get("updated_after") == "2026-05-01"
        assert req.url.params.get("updated_before") == "2026-05-31"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t",
        TicketFilters(
            created_after="2026-01-01",
            created_before="2026-12-31",
            updated_after="2026-05-01",
            updated_before="2026-05-31",
        ),
    )


def test_list_tickets_sort_by_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("order_by"))
        return _json([])

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().list_tickets(p, "t", TicketFilters(sort_by="created"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(sort_by="updated"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(sort_by="comments"))
    assert seen == ["created_at", "updated_at", "user_notes_count"]


def test_list_tickets_limit_capped_at_100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("per_page") == "100"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t", TicketFilters(limit=500),
    )


def test_list_tickets_propagates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404 Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().list_tickets(_project(), "t", TicketFilters())
    assert exc.value.status == 404


# ---------- get_ticket -------------------------------------------------------


def test_get_ticket_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("acme%2Fbackend/issues/5"):
            return _json(_issue_payload(5))
        if "acme%2Fbackend/issues/5/notes" in url:
            return _json([
                {
                    "id": 100, "body": "comment 1", "system": False,
                    "author": {"username": "alice"},
                    "created_at": "2024-01-01T00:00:00Z",
                },
                {
                    "id": 101, "body": "added label", "system": True,
                    "author": {"username": "alice"},
                    "created_at": "2024-01-01T00:01:00Z",
                },
                {
                    "id": 102, "body": "comment 2", "system": False,
                    "author": {"username": "bob"},
                    "created_at": "2024-01-01T00:02:00Z",
                },
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket, comments, relations, truncated = GitLabProvider().get_ticket(
        _project(), token="t", ticket_id="5",
    )
    assert ticket.id == "5"
    # System notes are filtered out — only the two user comments remain.
    assert [c.id for c in comments] == ["100", "102"]
    assert relations == []
    assert truncated is False


def test_get_ticket_skips_relations_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`include_relations=False` doesn't change the issue/notes calls,
    but it bypasses the (currently-stubbed) relations resolver. We
    document the call shape here so task #7 can extend it without
    breaking existing callers."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/issues/5"):
            return _json(_issue_payload(5))
        return _json([])

    _install_mock(monkeypatch, handler)
    _, _, relations, truncated = GitLabProvider().get_ticket(
        _project(), "t", "5", include_relations=False,
    )
    assert relations == []
    assert truncated is None


# ---------- create_ticket ----------------------------------------------------


def test_create_ticket_applies_marker_label_and_body_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/issues" in req.url.path:
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(
                42,
                description=captured["body"]["description"],
                labels=captured["body"].get("labels", "").split(",") if captured["body"].get("labels") else [],
            ))
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().create_ticket(
        _project(), "t", title="New issue", body="content",
        labels=["bug"], assignees=[],
    )
    assert ticket.id == "42"
    assert captured["body"]["title"] == "New issue"
    # Body prefix applied.
    assert captured["body"]["description"].startswith("#ai-generated")
    # ai-generated label included alongside caller-supplied "bug".
    assert "ai-generated" in captured["body"]["labels"]
    assert "bug" in captured["body"]["labels"]


def test_create_ticket_resolves_assignee_usernames_to_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            username = req.url.params.get("username")
            users = {"alice": 1, "bob": 2}
            if username in users:
                return _json([{"id": users[username], "username": username}])
            return _json([])
        if req.method == "POST" and "/issues" in req.url.path:
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(1))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[],
        assignees=["alice", "bob"],
    )
    assert captured["body"]["assignee_ids"] == [1, 2]


def test_create_ticket_with_closed_status_issues_put_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #42: `status="closed"` on GitLab issues a follow-up PUT
    with `state_event=close` (GitLab `POST /issues` always creates open)."""
    seen: list[str] = []
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            return _json(_issue_payload(60, state="opened"))
        if req.method == "PUT" and req.url.path.endswith("/issues/60"):
            captured["put"] = json.loads(req.content.decode())
            return _json(_issue_payload(60, state="closed"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b",
        labels=[], assignees=[], status="closed",
    )
    assert ticket.id == "60"
    assert captured["put"] == {"state_event": "close"}


def test_create_ticket_rejects_github_style_alias_on_gitlab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per #49 finding 5: GitLab no longer silently coerces GitHub's
    `closed:not_planned` into `state_event=close`. The agent must use a
    value from `list_ticket_statuses` for the project's provider.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="unsupported status 'closed:not_planned'"):
        GitLabProvider().create_ticket(
            _project(), "t", title="t", body="b",
            labels=[], assignees=[], status="closed:not_planned",
        )


def test_create_ticket_status_open_skips_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit `status="open"` matches GitLab's default — no PUT issued."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            return _json(_issue_payload(62))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b",
        labels=[], assignees=[], status="open",
    )
    assert not any(s.startswith("PUT ") for s in seen)


def test_create_ticket_default_status_skips_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted `status` — no follow-up PUT."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            return _json(_issue_payload(63))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[], assignees=[],
    )
    assert not any(s.startswith("PUT ") for s in seen)


def test_create_ticket_rejects_unknown_status_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid status raises ValueError before POST — no issue created."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        raise AssertionError("HTTP call should not happen on invalid status")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError) as exc_info:
        GitLabProvider().create_ticket(
            _project(), "t", title="t", body="b",
            labels=[], assignees=[], status="garbage",
        )
    msg = str(exc_info.value)
    assert "garbage" in msg
    assert seen == []


def test_create_ticket_rejects_unknown_assignees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per #49 finding 7: unresolvable usernames raise instead of being
    silently dropped — matches GitHub's behaviour and the
    AGENTS.md "clear failure beats silent success" principle.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.gitlab import GitLabError
    with pytest.raises(GitLabError, match="assignee 'ghost'"):
        GitLabProvider().create_ticket(
            _project(), "t", title="t", body="b", labels=[],
            assignees=["ghost"],
        )


# ---------- update_ticket ----------------------------------------------------


def test_update_ticket_status_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing the canonical GitLab status (`closed`) closes the ticket.
    Tests of the rejected GitHub-style aliases live in
    `test_update_ticket_rejects_github_style_alias_on_gitlab`.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5, state="closed"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().update_ticket(
        _project(), "t", "5", status="closed",
    )
    assert ticket.status == "closed"
    assert captured["body"]["state_event"] == "close"


def test_update_ticket_rejects_github_style_alias_on_gitlab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per #49 finding 5: `closed:completed` is no longer silently
    coerced to plain `closed` on GitLab. The rejection message mirrors
    `list_ticket_statuses` (no spurious aliases)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="unsupported status 'closed:completed'"):
        GitLabProvider().update_ticket(
            _project(), "t", "5", status="closed:completed",
        )


def test_update_ticket_status_reopen(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, state="closed", labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5, state="opened"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", status="open")
    assert captured["body"]["state_event"] == "reopen"


def test_update_ticket_rejects_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_issue_payload(5, labels=["ai-generated"]))

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="unsupported status"):
        GitLabProvider().update_ticket(
            _project(), "t", "5", status="In Progress",
        )


def test_update_ticket_adds_ai_modified_for_non_ai_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            # No ai-generated label → ai-modified should be added.
            return _json(_issue_payload(5, labels=["bug"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", title="renamed")
    assert "ai-modified" in captured["body"]["add_labels"]


def test_update_ticket_skips_ai_modified_when_already_ai_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", title="renamed")
    # Body shouldn't have add_labels at all — nothing to add.
    assert "add_labels" not in captured["body"]


def test_update_ticket_label_add_and_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated", "p2"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(
        _project(), "t", "5",
        labels_add=["bug"], labels_remove=["p2"],
    )
    assert captured["body"]["add_labels"] == "bug"
    assert captured["body"]["remove_labels"] == "p2"


def test_update_ticket_assignee_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "issues/5" in path:
            return _json(_issue_payload(
                5, labels=["ai-generated"],
                assignees=[{"username": "alice"}],
            ))
        if req.method == "GET" and path == "/api/v4/users":
            username = req.url.params.get("username")
            users = {"alice": 1, "bob": 2}
            if username in users:
                return _json([{"id": users[username], "username": username}])
            return _json([])
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(
        _project(), "t", "5",
        assignees_add=["bob"], assignees_remove=["alice"],
    )
    # Final list: alice removed, bob added → just [bob] → id=[2].
    assert captured["body"]["assignee_ids"] == [2]


def test_update_ticket_no_changes_returns_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fields changed AND ticket is ai-generated (so no ai-modified
    marker to inject) → no PUT, return the current snapshot."""
    puts: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            puts.append(req)
            return _json({}, status_code=500)
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().update_ticket(_project(), "t", "5")
    assert ticket.id == "5"
    assert puts == []


# ---------- comments / notes -------------------------------------------------


def test_add_comment_applies_marker_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 99, "body": captured["body"]["body"],
                "author": {"username": "alice"},
                "created_at": "2024-01-01T00:00:00Z",
                "noteable_iid": 5,
                "noteable_type": "Issue",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().add_comment(_project(), "t", "5", "comment text")
    assert c.id == "99"
    assert captured["body"]["body"].startswith("#ai-generated")
    # Ticket #41 addendum A: url is synthesised from project.web_url +
    # noteable_iid + note id.
    assert c.url == "https://gitlab.com/acme/backend/-/issues/5#note_99"


def test_add_comment_synthesises_mr_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Note attached to a merge request → `/-/merge_requests/<iid>#note_X`."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return _json({
                "id": 99, "body": "x",
                "author": {"username": "a"},
                "created_at": "2024-01-01T00:00:00Z",
                "noteable_iid": 7,
                "noteable_type": "MergeRequest",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().add_pr_comment(_project(), "t", "7", "comment text")
    assert c.url == "https://gitlab.com/acme/backend/-/merge_requests/7#note_99"


def test_list_comments_filters_system_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json([
            {
                "id": 1, "body": "user comment", "system": False,
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": 2, "body": "label added", "system": True,
                "author": {"username": "a"}, "created_at": "2024-01-01T00:01:00Z",
            },
        ])

    _install_mock(monkeypatch, handler)
    comments, has_more = GitLabProvider().list_comments(_project(), "t", "5")
    assert len(comments) == 1
    assert comments[0].body == "user comment"
    assert has_more is False


def test_get_comment_composite_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "acme%2Fbackend/issues/5/notes/99" in str(req.url)
        return _json({
            "id": 99, "body": "x",
            "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
        })

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().get_comment(_project(), "t", comment_id="5/99")
    assert c.id == "99"


def test_get_comment_plain_id_raises_without_ticket_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain note ids without a ticket_id aren't addressable — must
    surface a clear error so the caller adds the parent iid."""
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(GitLabError, match="issue_iid|ticket_id"):
        GitLabProvider().get_comment(_project(), "t", comment_id="99")


def test_get_comment_bare_id_with_ticket_id_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #41 addendum B/C: bare note id + ticket_id round-trips."""
    def handler(req: httpx.Request) -> httpx.Response:
        # Composite reconstructed internally → request hits the same
        # endpoint as the explicit composite form.
        assert "acme%2Fbackend/issues/5/notes/99" in str(req.url)
        return _json({
            "id": 99, "body": "x",
            "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            "noteable_iid": 5, "noteable_type": "Issue",
        })

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().get_comment(
        _project(), "t", comment_id="99", ticket_id="5",
    )
    assert c.id == "99"
    assert c.url == "https://gitlab.com/acme/backend/-/issues/5#note_99"


def test_update_comment_bare_id_with_ticket_id_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #41 addendum B/C: update_comment also accepts the bare-id
    round-trip when ticket_id is supplied."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "issues/5/notes/99" in path:
            return _json({
                "id": 99, "body": "human original",
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            })
        if req.method == "PUT" and "issues/5/notes/99" in path:
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 99, "body": captured["body"]["body"],
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
                "noteable_iid": 5, "noteable_type": "Issue",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().update_comment(
        _project(), "t", comment_id="99", body="new", ticket_id="5",
    )
    assert captured["body"]["body"] == "#ai-modified\n\nnew"
    assert c.url == "https://gitlab.com/acme/backend/-/issues/5#note_99"


def test_update_comment_stamps_modified_for_human_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #44: existing human-authored note → edit stamps `#ai-modified`."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "issues/5/notes/99" in path:
            return _json({
                "id": 99, "body": "human original",
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            })
        if req.method == "PUT" and "issues/5/notes/99" in path:
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 99, "body": captured["body"]["body"],
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_comment(_project(), "t", "5/99", "new content")
    assert captured["body"]["body"] == "#ai-modified\n\nnew content"


def test_update_comment_preserves_generated_for_ai_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #44: existing AI-generated note → edit preserves marker."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "issues/5/notes/99" in path:
            return _json({
                "id": 99, "body": "#ai-generated\n\nprev AI body",
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            })
        if req.method == "PUT" and "issues/5/notes/99" in path:
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 99, "body": captured["body"]["body"],
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_comment(_project(), "t", "5/99", "follow-up")
    assert captured["body"]["body"] == "#ai-generated\n\nfollow-up"


# ---------- Defect 3: empty body raises ValueError (GitLab) ------------------


def test_add_comment_empty_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_comment with body='' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitLabProvider().add_comment(_project(), "t", "5", "")


def test_add_comment_whitespace_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_comment with body='   ' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitLabProvider().add_comment(_project(), "t", "5", "   ")


def test_update_comment_empty_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment with body='' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitLabProvider().update_comment(_project(), "t", "5/99", "")


def test_update_comment_whitespace_body_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment with body='   ' must raise ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="empty"):
        GitLabProvider().update_comment(_project(), "t", "5/99", "   ")


# ---------- Issue #17 defect fixes -------------------------------------------


def test_update_ticket_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_ticket on a missing ticket wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().update_ticket(
            _project(), "t", "42", title="x",
        )
    assert exc.value.status == 404
    assert "ticket 'acme#42' not found" in exc.value.message


def test_add_comment_404_names_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_comment on a missing ticket wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404 Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().add_comment(_project(), "t", "42", "hello")
    assert exc.value.status == 404
    assert "ticket 'acme#42' not found" in exc.value.message


def test_update_comment_404_names_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_comment on a missing note wraps the 404 with the resource id."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().update_comment(
            _project(), "t", comment_id="99", body="x", ticket_id="5",
        )
    assert exc.value.status == 404
    assert "comment 'acme#99' not found" in exc.value.message


def test_check_no_double_status_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitLab returns {"message": "404 Not Found"}; the error string must NOT
    duplicate the status code (i.e. must not contain '404: 404')."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404 Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().list_tickets(_project(), "t", filters=TicketFilters())
    assert "404: 404" not in str(exc.value)
