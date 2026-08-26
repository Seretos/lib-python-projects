"""Tests for GitHubProvider label management methods (ticket #35).

Covers:
- list_labels — happy path, empty repo
- create_label — success, custom color/description, 422-already-exists, 403
- update_label — success, 404, no-fields ValueError
- delete_label — success (204), 404
- regression: create_label 422 is always raised (divergent from _ensure_label)
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import AutoLabels, ProjectConfig
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers.github import GitHubError, GitHubProvider
from lib_python_projects.providers.base import Label


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _project_with_custom_labels() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        auto_labels=AutoLabels(
            ai_generated="robot-made", ai_modified="robot-touched"
        ),
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


def _label_payload(name: str = "bug", color: str = "ee0701", description: str = "") -> dict:
    return {"id": 1, "name": name, "color": color, "description": description}


# ---------- list_labels -------------------------------------------------------


def test_list_labels_returns_label_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /labels returns two items → list[Label] with correct fields."""
    items = [
        _label_payload("bug", "ee0701", "Something is broken"),
        _label_payload("enhancement", "84b6eb", "New feature"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert "/repos/acme/backend/labels" in req.url.path
        assert req.url.params.get("per_page") == "100"
        return _json(items)

    _install_mock(monkeypatch, handler)
    labels = GitHubProvider().list_labels(_project(), token="t")
    assert isinstance(labels, list)
    assert len(labels) == 2
    assert all(isinstance(lbl, Label) for lbl in labels)
    assert labels[0].name == "bug"
    assert labels[0].color == "ee0701"
    assert labels[0].description == "Something is broken"
    assert labels[1].name == "enhancement"


def test_list_labels_empty_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty array from GitHub → empty list, no error."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    labels = GitHubProvider().list_labels(_project(), token="t")
    assert labels == []


# ---------- create_label ------------------------------------------------------


def test_create_label_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST 201 → Label with expected name and default color ededed."""
    response_payload = _label_payload("my-label", "ededed", "")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        body = json.loads(req.content)
        assert body["name"] == "my-label"
        assert body["color"] == "ededed"
        return _json(response_payload, status_code=201)

    _install_mock(monkeypatch, handler)
    label = GitHubProvider().create_label(_project(), token="t", name="my-label")
    assert isinstance(label, Label)
    assert label.name == "my-label"
    assert label.color == "ededed"
    assert label.description == ""


def test_create_label_custom_color_and_description(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller-supplied color is sent as-is (no # prefix added for GitHub)."""
    response_payload = _label_payload("fix", "ff0000", "fix description")

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["color"] == "ff0000"   # no # prefix for GitHub
        assert body["description"] == "fix description"
        return _json(response_payload, status_code=201)

    _install_mock(monkeypatch, handler)
    label = GitHubProvider().create_label(
        _project(), token="t", name="fix", color="ff0000", description="fix description"
    )
    assert label.color == "ff0000"
    assert label.description == "fix description"


def test_create_label_already_exists_raises_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """422 with already_exists code → GitHubError(422) with label name in message."""
    error_payload = {
        "message": "Validation Failed",
        "errors": [{"resource": "Label", "code": "already_exists", "field": "name"}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(error_payload, status_code=422)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_label(_project(), token="t", name="duplicate-label")
    assert exc.value.status == 422
    assert "duplicate-label" in exc.value.message


def test_create_label_non_conflict_422_surfaces_github_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 with a non-already_exists code (e.g. invalid color) → GitHubError
    whose message comes from GitHub's own error payload, not from the
    hard-coded 'already exists' string.

    This is a correctness guard: a 422 for an invalid color value must NOT
    be misreported as "already exists".
    """
    error_payload = {
        "message": "Validation Failed",
        "errors": [{"resource": "Label", "field": "color", "code": "invalid"}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(error_payload, status_code=422)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_label(
            _project(), token="t", name="my-label", color="not-a-hex-color"
        )
    assert exc.value.status == 422
    # The message must NOT claim the label already exists.
    assert "already exists" not in exc.value.message.lower()


def test_create_label_403_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 from GitHub → GitHubError(403) propagated."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Forbidden"}, status_code=403)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_label(_project(), token="t", name="blocked-label")
    assert exc.value.status == 403


# ---------- create_label / update_label color normalization (ticket #236) -----


def test_create_label_hash_prefixed_color_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_label: a '#'-prefixed color is stripped to bare hex before
    being sent — GitHub's API rejects a leading '#'."""
    response_payload = _label_payload("fix", "ff00ff", "")

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["color"] == "ff00ff"
        return _json(response_payload, status_code=201)

    _install_mock(monkeypatch, handler)
    label = GitHubProvider().create_label(
        _project(), token="t", name="fix", color="#ff00ff"
    )
    assert label.color == "ff00ff"


def test_create_label_hash_prefixed_and_bare_hex_produce_identical_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A '#'-prefixed color and the equivalent bare-hex color must produce
    an identical outgoing payload."""
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return _json(_label_payload("fix", "ff00ff", ""), status_code=201)

    _install_mock(monkeypatch, handler)
    GitHubProvider().create_label(_project(), token="t", name="fix", color="#ff00ff")
    GitHubProvider().create_label(_project(), token="t", name="fix", color="ff00ff")
    assert captured[0] == captured[1]


def test_create_label_garbage_color_passes_through_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-hex garbage colors are not validated/normalized locally beyond
    the leading-'#' strip — they pass through unchanged so GitHub's own
    422 validation error surfaces (see
    test_create_label_non_conflict_422_surfaces_github_message)."""
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return _json(_label_payload("x", "not-a-hex-color", ""), status_code=201)

    _install_mock(monkeypatch, handler)
    GitHubProvider().create_label(
        _project(), token="t", name="x", color="not-a-hex-color"
    )
    assert captured[0]["color"] == "not-a-hex-color"


# ---------- update_label ------------------------------------------------------


def test_update_label_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATCH 200 → Label with new_name reflected."""
    response_payload = _label_payload("renamed-label", "ededed", "")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "PATCH"
        assert "/repos/acme/backend/labels/old-label" in req.url.path
        body = json.loads(req.content)
        assert body["new_name"] == "renamed-label"
        return _json(response_payload)

    _install_mock(monkeypatch, handler)
    label = GitHubProvider().update_label(
        _project(), token="t", name="old-label", new_name="renamed-label"
    )
    assert isinstance(label, Label)
    assert label.name == "renamed-label"


def test_update_label_not_found_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 → GitHubError(404) with label name in message."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().update_label(
            _project(), token="t", name="missing-label", color="ffffff"
        )
    assert exc.value.status == 404
    assert "missing-label" in exc.value.message


def test_update_label_no_fields_raises_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """No optional fields supplied → ValueError, no HTTP call made."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="at least one of"):
        GitHubProvider().update_label(_project(), token="t", name="some-label")
    assert seen == [], "no HTTP call should have been made"


def test_update_label_hash_prefixed_color_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_label: a '#'-prefixed color is stripped to bare hex before
    being sent, same normalization as create_label."""
    response_payload = _label_payload("relabeled", "00ff00", "")

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["color"] == "00ff00"
        return _json(response_payload)

    _install_mock(monkeypatch, handler)
    label = GitHubProvider().update_label(
        _project(), token="t", name="old-label", color="#00ff00"
    )
    assert label.color == "00ff00"


def test_update_label_no_color_sends_no_color_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_label with color=None (only new_name supplied) sends no
    'color' key at all — no default is injected, unlike create_label."""
    captured: list[dict] = []
    response_payload = _label_payload("renamed-only", "ededed", "")

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return _json(response_payload)

    _install_mock(monkeypatch, handler)
    GitHubProvider().update_label(
        _project(), token="t", name="old-label", new_name="renamed-only"
    )
    assert "color" not in captured[0]


# ---------- delete_label ------------------------------------------------------


def test_delete_label_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE 204 → returns None."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        assert "/repos/acme/backend/labels/to-delete" in req.url.path
        return httpx.Response(status_code=204, content=b"")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().delete_label(_project(), token="t", name="to-delete")
    assert result is None


def test_delete_label_not_found_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 on DELETE → GitHubError(404) with label name in message."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().delete_label(_project(), token="t", name="gone-label")
    assert exc.value.status == 404
    assert "gone-label" in exc.value.message


# ---------- regression: create_label 422 semantics diverge from _ensure_label -


def test_create_label_consistency_with_ensure_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: create_label always raises GitHubError(422) when the
    label already exists. _ensure_label silently ignores 422 — these are
    intentionally different surfaces. Callers using create_label expect an
    explicit error on conflict."""
    error_payload = {
        "message": "Validation Failed",
        "errors": [{"resource": "Label", "code": "already_exists", "field": "name"}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(error_payload, status_code=422)

    _install_mock(monkeypatch, handler)
    # Must raise — not silently succeed like _ensure_label does.
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_label(_project(), token="t", name="ai-generated")
    assert exc.value.status == 422


# ---------- label existence validation (ticket #56) ---------------------------


def _issue_payload(number: int = 1) -> dict:
    """Minimal GitHub issue REST payload accepted by _map_issue."""
    return {
        "number": number,
        "state": "open",
        "title": "Test Issue",
        "body": "<!-- #ai-generated -->\nDescription.",
        "user": {"login": "bot"},
        "assignees": [],
        "labels": [],
        "milestone": None,
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _pr_payload(number: int = 42) -> dict:
    """Minimal GitHub PR REST payload accepted by _map_pr."""
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


def test_create_ticket_unknown_label_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_ticket: label GET 404 → GitHubError(404), no POST to /issues."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        # Label validation GET for caller-supplied label → 404
        if req.method == "GET" and path.endswith("/labels/typo-label"):
            return _json({"message": "Not Found"}, status_code=404)
        # ai-generated ensure (GET or POST)
        if "/labels" in path and req.method in ("GET", "POST"):
            return _json({"name": "ai-generated", "color": "0075ca"})
        # The POST to /issues must NOT be reached
        if path.endswith("/issues") and req.method == "POST":
            raise AssertionError("POST /issues should not be called")
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_ticket(
            _project(), token="t",
            title="T", body="B", labels=["typo-label"], assignees=[],
        )
    assert exc.value.status == 404
    assert "typo-label" in exc.value.message
    assert not any(
        r.method == "POST" and r.url.path.endswith("/issues") for r in seen
    ), "POST /issues must not be called when label validation fails"


def test_create_ticket_known_label_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_ticket: label GET 200 → proceeds normally, returns Ticket."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Label validation GET or ai-generated ensure
        if "/labels" in path and req.method in ("GET", "POST"):
            return _json({"name": "bug", "color": "ee0701"})
        if path.endswith("/issues") and req.method == "POST":
            return _json(_issue_payload(7), status_code=201)
        return _json({})

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.base import Ticket
    ticket = GitHubProvider().create_ticket(
        _project(), token="t",
        title="T", body="B", labels=["bug"], assignees=[],
    )
    assert isinstance(ticket, Ticket)
    assert ticket.id == "7"


def test_create_ticket_empty_labels_no_validation_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_ticket with labels=[] makes no label-validation GET."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        # ai-generated ensure (POST is fine)
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        if path.endswith("/issues") and req.method == "POST":
            return _json(_issue_payload(3), status_code=201)
        return _json({})

    _install_mock(monkeypatch, handler)
    GitHubProvider().create_ticket(
        _project(), token="t",
        title="T", body="B", labels=[], assignees=[],
    )
    # Only the ai-generated ensure POST (or GET) is allowed — no per-label GET
    label_gets = [
        r for r in seen
        if r.method == "GET" and "/labels/" in r.url.path
        and "ai-generated" not in r.url.path
    ]
    assert label_gets == [], "empty labels list must not trigger validation calls"


def test_update_ticket_labels_add_unknown_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket: labels_add with unknown label 404s → raises, no PATCH."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        # Current-state GET
        if req.method == "GET" and path.endswith("/issues/5"):
            return _json(_issue_payload(5))
        # Label validation GET → 404
        if req.method == "GET" and path.endswith("/labels/typo-label"):
            return _json({"message": "Not Found"}, status_code=404)
        # PATCH must NOT be reached
        if req.method == "PATCH" and "/issues/5" in path:
            raise AssertionError("PATCH must not be called when label validation fails")
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().update_ticket(
            _project(), token="t", ticket_id="5",
            labels_add=["typo-label"],
        )
    assert exc.value.status == 404
    assert "typo-label" in exc.value.message
    assert not any(
        r.method == "PATCH" and "/issues/5" in r.url.path for r in seen
    )


def test_update_ticket_ai_label_skips_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket: labels_add=['ai-modified'] does NOT call GET /labels/ai-modified
    (AI labels keep intentional best-effort auto-create)."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/5"):
            return _json(_issue_payload(5))
        # ai-modified ensure
        if "/labels" in path and req.method == "POST":
            return _json({"name": "ai-modified", "color": "ededed"})
        if req.method == "PATCH" and "/issues/5" in path:
            raw = _issue_payload(5)
            raw["labels"] = [{"name": "ai-modified"}]
            return _json(raw)
        return _json({})

    _install_mock(monkeypatch, handler)
    GitHubProvider().update_ticket(
        _project(), token="t", ticket_id="5",
        labels_add=["ai-modified"],
    )
    # Must NOT have called GET /labels/ai-modified as a validation step
    validation_gets = [
        r for r in seen
        if r.method == "GET" and r.url.path.endswith("/labels/ai-modified")
    ]
    assert validation_gets == [], (
        "GET /labels/ai-modified should be skipped — AI labels bypass validation"
    )


def test_create_pr_unknown_label_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pr: unknown label 404 → GitHubError(404), no POST to /pulls."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        # Label validation GET for caller-supplied label → 404
        if req.method == "GET" and path.endswith("/labels/typo-label"):
            return _json({"message": "Not Found"}, status_code=404)
        # ai-generated ensure
        if "/labels" in path:
            return _json({"name": "ai-generated", "color": "0075ca"})
        # POST /pulls must NOT be reached
        if path.endswith("/pulls") and req.method == "POST":
            raise AssertionError("POST /pulls should not be called")
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().create_pr(
            _project(), token="t",
            title="T", body="B", head="feature", base="main",
            labels=["typo-label"],
        )
    assert exc.value.status == 404
    assert "typo-label" in exc.value.message
    assert not any(
        r.method == "POST" and r.url.path.endswith("/pulls") for r in seen
    )


def test_update_pr_labels_add_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_pr: labels_add with unknown label 404s → raises before the PATCH."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        # Current-state GET
        if req.method == "GET" and path.endswith("/pulls/10"):
            return _json(_pr_payload(10))
        # Label validation GET → 404
        if req.method == "GET" and path.endswith("/labels/typo-label"):
            return _json({"message": "Not Found"}, status_code=404)
        # PATCH must NOT be reached
        if req.method == "PATCH" and "/pulls/10" in path:
            raise AssertionError("PATCH must not be called when label validation fails")
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().update_pr(
            _project(), token="t", pr_id="10",
            labels_add=["typo-label"],
        )
    assert exc.value.status == 404
    assert "typo-label" in exc.value.message
    assert not any(
        r.method == "PATCH" and "/pulls/10" in r.url.path for r in seen
    )


def test_update_pr_docstring_documents_non_atomicity() -> None:
    """update_pr's docstring (ticket #238) must state that labels_add
    existence is validated up front (nothing applied on failure), and that
    the stages past that point — title/body/base, labels, assignees,
    reviewers, draft toggle — commit in sequence with NO rollback, so a
    later-stage failure can leave earlier stages already applied. Must not
    claim atomicity or rollback anywhere, and must document the stages in
    their actual commit order."""
    import inspect

    doc = inspect.getdoc(GitHubProvider.update_pr)
    assert doc is not None
    doc_lower = doc.lower()
    # Up-front validation, nothing applied on failure.
    assert "_assert_labels_exist" in doc or "up front" in doc_lower
    # Explicit non-atomicity / no-rollback framing.
    assert "not atomic" in doc_lower
    assert "no rollback" in doc_lower

    # No claim of the opposite (rollback-on-failure) semantics, however
    # it's phrased — a docstring that says stages ARE rolled back must
    # fail here even though it also contains "no rollback" elsewhere.
    assert "rolled back" not in doc_lower
    assert "rolls back" not in doc_lower
    assert "roll back on" not in doc_lower

    # The staged commit order is spelled out in the actual commit order:
    # title/body/base, then labels, then assignees, then reviewers, then
    # the draft toggle. A docstring that lists the stages out of order
    # (e.g. reversed) must fail here, not just contain the five words
    # unordered somewhere in the text.
    #
    # Scope the ordering check to the stage-order sentence itself (the
    # "title/body/base -> labels -> assignees -> reviewers -> draft
    # toggle" list), not the whole docstring: earlier text mentions
    # "labels_add"/"_assert_labels_exist" (both contain "labels") ahead
    # of the actual stage list, so a whole-docstring first-occurrence
    # .index() would find those incidental mentions instead of the real
    # staged order and could pass even for a docstring listing the
    # stages out of order.
    stage_order_marker = "title/body/base"
    assert stage_order_marker in doc_lower
    stage_order_text = doc_lower[doc_lower.index(stage_order_marker):]
    title_idx = stage_order_text.index("title")
    labels_idx = stage_order_text.index("labels")
    assignees_idx = stage_order_text.index("assignees")
    reviewers_idx = stage_order_text.index("reviewers")
    draft_idx = stage_order_text.index("draft")
    assert title_idx < labels_idx < assignees_idx < reviewers_idx < draft_idx


# ---------- per-project configurable auto_labels (ticket #153) ---------------


def test_create_ticket_applies_custom_ai_generated_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_ticket: a project with a custom `auto_labels.ai_generated`
    applies that name instead of the literal 'ai-generated' — both as the
    ensured/created label and in the labels POST payload."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if "/labels" in path and req.method == "POST":
            body = json.loads(req.content)
            assert body["name"] == "robot-made"
            return _json({"name": "robot-made", "color": "0e8a16"}, status_code=201)
        if path.endswith("/issues") and req.method == "POST":
            body = json.loads(req.content)
            assert "robot-made" in body["labels"]
            assert "ai-generated" not in body["labels"]
            raw = _issue_payload(9)
            raw["labels"] = [{"name": "robot-made"}]
            return _json(raw, status_code=201)
        return _json({})

    _install_mock(monkeypatch, handler)
    GitHubProvider().create_ticket(
        _project_with_custom_labels(), token="t",
        title="T", body="B", labels=[], assignees=[],
    )
    label_posts = [
        r for r in seen if r.method == "POST" and "/labels" in r.url.path
    ]
    assert len(label_posts) == 1


def test_update_ticket_applies_custom_ai_modified_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_ticket: marking a not-yet-AI-generated ticket as AI-modified
    applies the project's custom `auto_labels.ai_modified` name."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if req.method == "GET" and path.endswith("/issues/5"):
            return _json(_issue_payload(5))  # no labels -> not ai-generated
        if "/labels" in path and req.method == "POST":
            body = json.loads(req.content)
            assert body["name"] == "robot-touched"
            return _json({"name": "robot-touched", "color": "fbca04"}, status_code=201)
        if req.method == "PATCH" and "/issues/5" in path:
            raw = _issue_payload(5)
            raw["labels"] = [{"name": "robot-touched"}]
            return _json(raw)
        return _json({})

    _install_mock(monkeypatch, handler)
    GitHubProvider().update_ticket(
        _project_with_custom_labels(), token="t", ticket_id="5",
        body="edited body",
    )
    label_posts = [
        r for r in seen if r.method == "POST" and "/labels" in r.url.path
    ]
    assert len(label_posts) == 1


def test_ensure_label_resolves_color_and_description_by_role_not_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renamed generated/modified label still resolves to the correct
    role-based color/description, not the grey/empty fallback that a
    name-keyed lookup would produce for an unrecognized literal name."""
    posted_payloads: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/labels" in req.url.path and req.method == "POST":
            body = json.loads(req.content)
            posted_payloads.append(body)
            return _json(body, status_code=201)
        return _json({})

    _install_mock(monkeypatch, handler)
    project = _project_with_custom_labels()
    with github_mod._client("t") as client:
        github_mod._ensure_label(client, project, "robot-made", "generated")
        github_mod._ensure_label(client, project, "robot-touched", "modified")

    assert posted_payloads[0]["name"] == "robot-made"
    assert posted_payloads[0]["color"] == "0e8a16"
    assert posted_payloads[0]["description"] != ""

    assert posted_payloads[1]["name"] == "robot-touched"
    assert posted_payloads[1]["color"] == "fbca04"
    assert posted_payloads[1]["description"] != ""
