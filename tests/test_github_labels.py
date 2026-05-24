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

from lib_python_projects import ProjectConfig
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
