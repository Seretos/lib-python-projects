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

from lib_python_projects import (
    AutoLabels,
    AzureBoardsBinding,
    Board,
    BoardAutoLabels,
    ProjectConfig,
)
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.base import TicketFilters
from lib_python_projects.providers.gitlab import GitLabError, GitLabProvider


def _project(path: str = "acme/backend", **kwargs) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="gitlab",
        path=path,
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
    tickets, has_more = GitLabProvider().list_tickets(
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


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_tickets_nonpositive_limit_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError without any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        GitLabProvider().list_tickets(
            _project(), "t", TicketFilters(limit=bad_limit),
        )


def test_list_tickets_area_path_raises_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """area_path is an Azure DevOps concept — GitLab must fail fast."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when area_path is set")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="area_path is not supported on GitLab"):
        GitLabProvider().list_tickets(
            _project(), "t", TicketFilters(area_path="MyProj\\Team A"),
        )


def test_list_tickets_board_column_raises_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """board_column is a GitHub Projects v2 concept — GitLab must fail fast."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when board_column is set")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="board_column is not supported on GitLab"):
        GitLabProvider().list_tickets(
            _project(), "t", TicketFilters(board_column="Review"),
        )


def test_list_tickets_has_more_true_when_full_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_more is True when the API returns exactly per_page items."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Return exactly 2 items matching limit=2.
        return _json([_issue_payload(1), _issue_payload(2)])

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitLabProvider().list_tickets(
        _project(),
        token="tok",
        filters=TicketFilters(limit=2),
    )
    assert len(tickets) == 2
    assert has_more is True


def test_list_tickets_has_more_false_when_partial_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_more is False when the API returns fewer than per_page items."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Return 1 item when limit=5.
        return _json([_issue_payload(1)])

    _install_mock(monkeypatch, handler)
    tickets, has_more = GitLabProvider().list_tickets(
        _project(),
        token="tok",
        filters=TicketFilters(limit=5),
    )
    assert len(tickets) == 1
    assert has_more is False


def test_list_tickets_propagates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404 Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().list_tickets(_project(), "t", TicketFilters())
    assert exc.value.status == 404


# ---------- list_tickets: states (ticket #115) -------------------------------


def test_list_tickets_states_closed_maps_to_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("state") == "closed"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t", TicketFilters(states=["closed"]),
    )


def test_list_tickets_states_open_maps_to_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("state") == "opened"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t", TicketFilters(states=["open"]),
    )


def test_list_tickets_states_both_maps_to_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("state") == "all"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t", TicketFilters(states=["open", "closed"]),
    )


def test_list_tickets_states_takes_precedence_over_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("state") == "closed"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t", TicketFilters(status="open", states=["closed"]),
    )


def test_list_tickets_states_invalid_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an invalid states value")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="Accepted: open, closed") as exc:
        GitLabProvider().list_tickets(
            _project(), "t", TicketFilters(states=["in progress"]),
        )
    assert "use list_ticket_statuses to discover valid values" in str(exc.value)


def test_list_tickets_states_empty_list_unchanged_status_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("state") == "opened"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t", TicketFilters(status="open", states=[]),
    )


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
    # GitLab has no AcceptanceCriteria equivalent — locks in the defaulted
    # `Ticket.acceptance_criteria` contract (#113).
    assert ticket.acceptance_criteria == ""


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


def test_create_ticket_applies_custom_auto_label_and_body_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #153: a project with custom `auto_labels` gets its own
    configured label name and body-marker prefix, not the defaults."""
    captured: dict = {}
    project = _project(
        auto_labels=AutoLabels(ai_generated="robot-made", ai_modified="robot-touched"),
    )

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
    GitLabProvider().create_ticket(
        project, "t", title="New issue", body="content",
        labels=["bug"], assignees=[],
    )
    assert captured["body"]["description"].startswith("#robot-made")
    assert "robot-made" in captured["body"]["labels"]
    assert "ai-generated" not in captured["body"]["labels"]


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


# ---------- ticket #123: custom_fields (labels + milestone) -----------------


def test_get_ticket_include_custom_fields_returns_labels_and_milestone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("acme%2Fbackend/issues/5"):
            return _json(_issue_payload(
                5, labels=["bug", "urgent"],
                milestone={"id": 9, "title": "v2.0"},
            ))
        if "acme%2Fbackend/issues/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitLabProvider().get_ticket(
        _project(), "t", "5", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields == {
        "labels": ["bug", "urgent"], "milestone": "v2.0",
    }


def test_get_ticket_include_custom_fields_milestone_unset_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No milestone on the issue -> `custom_fields["milestone"]` is `None`,
    not omitted."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("acme%2Fbackend/issues/5"):
            return _json(_issue_payload(5, labels=[], milestone=None))
        if "acme%2Fbackend/issues/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitLabProvider().get_ticket(
        _project(), "t", "5", include_relations=False, include_custom_fields=True,
    )
    assert ticket.custom_fields == {"labels": [], "milestone": None}


def test_get_ticket_without_include_custom_fields_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `include_custom_fields=False` leaves `custom_fields` at its
    `None` default — this is the "not requested" sentinel."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("acme%2Fbackend/issues/5"):
            return _json(_issue_payload(5))
        if "acme%2Fbackend/issues/5/notes" in url:
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitLabProvider().get_ticket(
        _project(), "t", "5", include_relations=False,
    )
    assert ticket.custom_fields is None


def test_create_ticket_custom_fields_labels_replaces_positional_and_keeps_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`custom_fields["labels"]` replaces the positional `labels` arg
    entirely, but the `ai-generated` marker is still merged in — the
    attribution marker must never be dropped."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(70))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b",
        labels=["ignored-positional-label"], assignees=[],
        custom_fields={"labels": ["from-custom-fields"]},
    )
    sent_labels = captured["body"]["labels"].split(",")
    assert "ignored-positional-label" not in sent_labels
    assert "from-custom-fields" in sent_labels
    assert "ai-generated" in sent_labels


def test_create_ticket_custom_fields_milestone_resolves_to_milestone_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            assert req.url.params.get("title") == "v2.0"
            return _json([{"id": 9, "title": "v2.0"}])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(71))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[], assignees=[],
        custom_fields={"milestone": "v2.0"},
    )
    assert captured["body"]["milestone_id"] == 9


def test_create_ticket_custom_fields_milestone_none_omits_milestone_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            raise AssertionError("no milestone lookup expected for milestone=None")
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(72))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[], assignees=[],
        custom_fields={"milestone": None},
    )
    assert "milestone_id" not in captured["body"]


def test_create_ticket_custom_fields_unknown_milestone_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            return _json([])
        raise AssertionError("no POST expected when milestone resolution fails")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not found"):
        GitLabProvider().create_ticket(
            _project(), "t", title="t", body="b", labels=[], assignees=[],
            custom_fields={"milestone": "does-not-exist"},
        )


def test_create_ticket_custom_fields_unrecognized_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only 'labels' and 'milestone' are recognized custom_fields keys on
    GitLab; anything else raises ValueError before any HTTP call."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an unknown key")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="some_field"):
        GitLabProvider().create_ticket(
            _project(), "t", title="t", body="b", labels=[], assignees=[],
            custom_fields={"some_field": "x"},
        )


# ---------- Ticket #74: create_ticket blank-title guard (GitLab) --------------


class TestCreateTicketBlankTitleGitLab:
    """create_ticket must raise ValueError before any HTTP call when title is blank."""

    def test_empty_string_title_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """title="" must raise ValueError(blank) with no HTTP request made."""
        seen = _install_mock(monkeypatch, lambda req: _json({}))
        with pytest.raises(ValueError, match="blank"):
            GitLabProvider().create_ticket(
                _project(), "t", title="", body="body", labels=[], assignees=[],
            )
        assert seen == [], "no HTTP request should have been made"

    def test_whitespace_only_title_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """title='   ' must raise ValueError(blank) with no HTTP request made."""
        seen = _install_mock(monkeypatch, lambda req: _json({}))
        with pytest.raises(ValueError, match="blank"):
            GitLabProvider().create_ticket(
                _project(), "t", title="   ", body="body", labels=[], assignees=[],
            )
        assert seen == [], "no HTTP request should have been made"


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


def test_update_ticket_adds_custom_ai_modified_label_for_non_ai_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #153: with custom `auto_labels`, the configured
    `ai_modified` name is added instead of the literal `ai-modified`."""
    captured: dict = {}
    project = _project(
        auto_labels=AutoLabels(ai_generated="robot-made", ai_modified="robot-touched"),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            # No "robot-made" label → "robot-touched" should be added.
            return _json(_issue_payload(5, labels=["bug"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(project, "t", "5", title="renamed")
    assert "robot-touched" in captured["body"]["add_labels"]
    assert "ai-modified" not in captured["body"]["add_labels"]


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


# ---------- ticket #154: board.auto_labels (on_create/on_update) ------------
#
# `on_create`/`on_update` are honored on GitLab (folded additively into the
# create POST / update PUT label sets). `on_move_to` is GitHub-only for this
# iteration — GitLab has no board-write path to hang it off of, so it stays
# a documented, validated no-op here.


def _board_with_auto_labels(**auto_label_kwargs) -> Board:
    return Board(
        columns=["Todo", "Doing", "Done"],
        binding=AzureBoardsBinding(kind="azure-boards"),
        auto_labels=BoardAutoLabels(**auto_label_kwargs),
    )


def test_create_ticket_on_create_labels_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    project = _project(board=_board_with_auto_labels(on_create=["triaged"]))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/issues" in req.url.path:
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(
                42,
                description=captured["body"]["description"],
                labels=captured["body"].get("labels", "").split(",")
                if captured["body"].get("labels") else [],
            ))
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        project, "t", title="New issue", body="content",
        labels=["bug"], assignees=[],
    )
    assert "triaged" in captured["body"]["labels"]
    assert "ai-generated" in captured["body"]["labels"]
    assert "bug" in captured["body"]["labels"]


def test_update_ticket_on_update_labels_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    project = _project(board=_board_with_auto_labels(on_update=["touched"]))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().update_ticket(project, "t", "5", title="renamed")
    assert ticket.id == "5"
    assert "touched" in captured["body"]["add_labels"]


def test_update_ticket_on_move_to_configured_but_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_move_to` validates and loads fine on a GitLab-bound board, but
    GitLab has no board-write path to fire it from: `update_ticket`
    neither adds the configured labels nor raises."""
    captured: dict = {}
    project = _project(board=_board_with_auto_labels(on_move_to={"Done": ["deployed"]}))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().update_ticket(project, "t", "5", title="renamed")
    assert ticket.id == "5"
    assert "deployed" not in captured["body"].get("add_labels", "")


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


def test_list_comments_since_filters_old_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Comments created before `since` must be excluded even when GitLab's
    `created_after` server hint doesn't filter them out."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Return both old and new notes regardless of created_after param —
        # simulating GitLab ignoring the server hint.
        return _json([
            {
                "id": 1, "body": "old comment", "system": False,
                "author": {"username": "alice"},
                "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": 2, "body": "new comment", "system": False,
                "author": {"username": "bob"},
                "created_at": "2024-03-01T00:00:00Z",
            },
        ])

    _install_mock(monkeypatch, handler)
    comments, has_more = GitLabProvider().list_comments(
        _project(), "t", "5", since="2024-02-01T00:00:00Z"
    )
    assert len(comments) == 1
    assert comments[0].body == "new comment"
    assert comments[0].id == "2"
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


# ---------- Ticket #69: delete_comment ---------------------------------------


def test_delete_comment_happy_path_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete_comment with a composite comment_id issues DELETE to the right URL
    and returns None."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status_code=204, content=b"")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().delete_comment(
        _project(), "t", comment_id="5/99",
    )
    assert result is None
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert "issues/5/notes/99" in str(seen[0].url)


def test_delete_comment_happy_path_bare_with_ticket_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete_comment also accepts a bare note id when ticket_id is supplied."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status_code=204, content=b"")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().delete_comment(
        _project(), "t", comment_id="99", ticket_id="5",
    )
    assert result is None
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert "issues/5/notes/99" in str(seen[0].url)


def test_delete_comment_404_names_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_comment on a missing note raises GitLabError 404 naming the comment."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().delete_comment(
            _project(), "t", comment_id="99", ticket_id="5",
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


# ---------- ticket #151: Ticket.milestone read + milestone= write kwarg ------


def test_map_issue_populates_milestone_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_map_issue` projects the issue's milestone title onto
    `Ticket.milestone`."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([_issue_payload(1, milestone={"title": "v2.0"})])

    _install_mock(monkeypatch, handler)
    tickets, _ = GitLabProvider().list_tickets(
        _project(), token="t", filters=TicketFilters(),
    )
    assert tickets[0].milestone == "v2.0"


def test_map_issue_milestone_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No milestone on the issue -> `Ticket.milestone is None`."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([_issue_payload(1)])

    _install_mock(monkeypatch, handler)
    tickets, _ = GitLabProvider().list_tickets(
        _project(), token="t", filters=TicketFilters(),
    )
    assert tickets[0].milestone is None


def test_create_ticket_milestone_kwarg_resolves_to_milestone_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            assert req.url.params.get("title") == "v3.0"
            return _json([{"id": 11, "title": "v3.0"}])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(80))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[], assignees=[],
        milestone="v3.0",
    )
    assert captured["body"]["milestone_id"] == 11


def test_create_ticket_milestone_kwarg_wins_over_custom_fields_milestone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both `milestone=` and `custom_fields['milestone']` are given,
    the explicit `milestone=` kwarg wins (ticket #151)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            assert req.url.params.get("title") == "kwarg-wins"
            return _json([{"id": 22, "title": "kwarg-wins"}])
        if req.method == "POST" and req.url.path.endswith("/issues"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(81))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[], assignees=[],
        custom_fields={"milestone": "custom-fields-loses"},
        milestone="kwarg-wins",
    )
    assert captured["body"]["milestone_id"] == 22


def test_create_ticket_milestone_omitted_issues_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`milestone=` omitted (default `_UNSET`) issues no milestone lookup
    or write at all."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            raise AssertionError("no milestone lookup expected when omitted")
        if req.method == "POST" and req.url.path.endswith("/issues"):
            return _json(_issue_payload(82))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[], assignees=[],
    )
    assert ticket.id == "82"


def test_update_ticket_milestone_kwarg_resolves_to_milestone_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`update_ticket` previously had no milestone support at all —
    ticket #151 adds it."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/issues/5"):
            return _json(_issue_payload(5))
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            assert req.url.params.get("title") == "v4.0"
            return _json([{"id": 33, "title": "v4.0"}])
        if req.method == "PUT" and req.url.path.endswith("/issues/5"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", milestone="v4.0")
    assert captured["body"]["milestone_id"] == 33


def test_update_ticket_milestone_none_clears_via_milestone_id_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`milestone=None` on update clears the milestone via GitLab's
    `milestone_id: 0` sentinel."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/issues/5"):
            return _json(_issue_payload(5))
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            raise AssertionError("no milestone lookup expected for milestone=None")
        if req.method == "PUT" and req.url.path.endswith("/issues/5"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", milestone=None)
    assert captured["body"]["milestone_id"] == 0


def test_update_ticket_milestone_omitted_issues_no_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`milestone=` omitted (`_UNSET`) — with no other fields changed —
    issues no PUT at all (matches the existing "no payload -> no PUT"
    contract). Uses an `ai-generated`-labelled issue so the unrelated
    ai-modified-marker heuristic doesn't itself put something in the
    payload and mask what we're testing."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/issues/5"):
            return _json(_issue_payload(5, labels=["ai-generated"]))
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().update_ticket(_project(), "t", "5")
    assert ticket.id == "5"


def test_update_ticket_milestone_unknown_title_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/issues/5"):
            return _json(_issue_payload(5))
        if req.method == "GET" and req.url.path.endswith("/milestones"):
            return _json([])
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="not found"):
        GitLabProvider().update_ticket(_project(), "t", "5", milestone="nope")
