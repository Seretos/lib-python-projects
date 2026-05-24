"""Tests for GitLabProvider label management methods (ticket #35).

Covers:
- list_labels — happy path, empty project
- create_label — success with color normalization, 409 conflict
- update_label — success, 404, no-fields ValueError
- delete_label — success (204), 404
- _normalize_gitlab_color helper
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.gitlab import GitLabError, GitLabProvider, _normalize_gitlab_color
from lib_python_projects.providers.base import Label


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


def _label_payload(name: str = "bug", color: str = "#ee0701", description: str = "") -> dict:
    return {"id": 1, "name": name, "color": color, "description": description}


# ---------- list_labels -------------------------------------------------------


def test_list_labels_returns_label_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /labels returns two items; GitLab #RRGGBB color is passed through."""
    items = [
        _label_payload("bug", "#ee0701", "Something is broken"),
        _label_payload("feature", "#0075ca", "New feature"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert "/labels" in req.url.path
        assert req.url.params.get("per_page") == "100"
        return _json(items)

    _install_mock(monkeypatch, handler)
    labels = GitLabProvider().list_labels(_project(), token="t")
    assert isinstance(labels, list)
    assert len(labels) == 2
    assert all(isinstance(lbl, Label) for lbl in labels)
    assert labels[0].name == "bug"
    assert labels[0].color == "#ee0701"   # # prefix preserved as-is
    assert labels[0].description == "Something is broken"
    assert labels[1].name == "feature"


def test_list_labels_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty array from GitLab → empty list, no error."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    labels = GitLabProvider().list_labels(_project(), token="t")
    assert labels == []


# ---------- create_label ------------------------------------------------------


def test_create_label_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST → Label returned; bare hex color is prefixed with #."""
    response_payload = _label_payload("fix", "#ff0000", "")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        body = json.loads(req.content)
        assert body["name"] == "fix"
        assert body["color"] == "#ff0000"   # bare hex "ff0000" → "#ff0000"
        return _json(response_payload, status_code=201)

    _install_mock(monkeypatch, handler)
    label = GitLabProvider().create_label(
        _project(), token="t", name="fix", color="ff0000"
    )
    assert isinstance(label, Label)
    assert label.name == "fix"
    assert label.color == "#ff0000"


def test_create_label_already_hex_color_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Color already prefixed with # → not double-prefixed."""
    response_payload = _label_payload("ok", "#ff0000", "")

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        # Must be exactly "#ff0000", not "##ff0000".
        assert body["color"] == "#ff0000"
        return _json(response_payload, status_code=201)

    _install_mock(monkeypatch, handler)
    label = GitLabProvider().create_label(
        _project(), token="t", name="ok", color="#ff0000"
    )
    assert label.color == "#ff0000"


def test_create_label_default_color_is_hash_ededed(monkeypatch: pytest.MonkeyPatch) -> None:
    """color=None → default #ededed sent to API."""
    response_payload = _label_payload("default-color", "#ededed", "")

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["color"] == "#ededed"
        return _json(response_payload, status_code=201)

    _install_mock(monkeypatch, handler)
    label = GitLabProvider().create_label(_project(), token="t", name="default-color")
    assert label.color == "#ededed"


def test_create_label_conflict_409_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """409 Conflict → GitLabError(409) with label name in message."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Label already exists"}, status_code=409)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().create_label(_project(), token="t", name="existing-label")
    assert exc.value.status == 409
    assert "existing-label" in exc.value.message


# ---------- update_label ------------------------------------------------------


def test_update_label_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT → Label with updated name."""
    response_payload = _label_payload("renamed", "#ededed", "")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "PUT"
        assert "/labels/old-name" in req.url.path
        body = json.loads(req.content)
        assert body["new_name"] == "renamed"
        return _json(response_payload)

    _install_mock(monkeypatch, handler)
    label = GitLabProvider().update_label(
        _project(), token="t", name="old-name", new_name="renamed"
    )
    assert isinstance(label, Label)
    assert label.name == "renamed"


def test_update_label_not_found_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 → GitLabError(404) with label name in message."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404 Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().update_label(
            _project(), token="t", name="ghost-label", color="#ffffff"
        )
    assert exc.value.status == 404
    assert "ghost-label" in exc.value.message


def test_update_label_no_fields_raises_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """No optional fields supplied → ValueError, no HTTP call made."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="at least one of"):
        GitLabProvider().update_label(_project(), token="t", name="some-label")
    assert seen == [], "no HTTP call should have been made"


# ---------- delete_label ------------------------------------------------------


def test_delete_label_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE 204 → returns None.

    Regression for ticket #35 review finding 1: GitLab DELETE /labels must
    pass the label name as a query parameter (?name=...), NOT as a path
    segment (/labels/{name}). Asserting on the query param rather than the
    URL path catches a wrong-endpoint implementation that would 404 on real
    GitLab.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        # Name must be in query params, not the URL path.
        assert req.url.params.get("name") == "to-delete"
        # The path should end at /labels, not /labels/to-delete.
        assert req.url.path.endswith("/labels")
        return httpx.Response(status_code=204, content=b"")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().delete_label(_project(), token="t", name="to-delete")
    assert result is None


def test_delete_label_not_found_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 on DELETE → GitLabError(404) with label name in message.

    The 404 response comes from DELETE /projects/{id}/labels?name={name}
    (query-param form), not from a path-segment endpoint.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        # Verify corrected endpoint shape even in the 404 path.
        assert req.url.params.get("name") == "gone-label"
        assert req.url.path.endswith("/labels")
        return _json({"message": "404 Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().delete_label(_project(), token="t", name="gone-label")
    assert exc.value.status == 404
    assert "gone-label" in exc.value.message


# ---------- _normalize_gitlab_color helper ------------------------------------


def test_normalize_gitlab_color_none_returns_default() -> None:
    assert _normalize_gitlab_color(None) == "#ededed"


def test_normalize_gitlab_color_empty_returns_default() -> None:
    assert _normalize_gitlab_color("") == "#ededed"


def test_normalize_gitlab_color_bare_hex_prefixed() -> None:
    assert _normalize_gitlab_color("ff0000") == "#ff0000"


def test_normalize_gitlab_color_already_prefixed_unchanged() -> None:
    assert _normalize_gitlab_color("#ff0000") == "#ff0000"


def test_normalize_gitlab_color_no_double_hash() -> None:
    """Ensure # is not added twice."""
    result = _normalize_gitlab_color("#abcdef")
    assert result == "#abcdef"
    assert result.count("#") == 1
