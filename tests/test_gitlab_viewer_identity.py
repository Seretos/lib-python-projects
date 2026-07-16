"""Viewer-identity ("whoami") resolution tests (ticket #191).

Covers `GitLabProvider.resolve_viewer_login`'s happy path and every
documented failure mode: bad_credentials (401/404), network_error,
other non-2xx, and a missing `username` field.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.base import ViewerIdentity
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


# ---------- happy path -------------------------------------------------------


def test_resolve_viewer_login_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v4/user"
        return _json({"username": "tanuki", "name": "Tanuki"})

    seen = _install_mock(monkeypatch, handler)
    identity = GitLabProvider().resolve_viewer_login(_project(), "tok")
    assert identity == ViewerIdentity(
        login="tanuki", display_name="Tanuki", provider="gitlab", reason=None,
    )
    assert len(seen) == 1


# ---------- failure modes -----------------------------------------------------


def test_resolve_viewer_login_401_is_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "401 Unauthorized"}, status_code=401)

    _install_mock(monkeypatch, handler)
    identity = GitLabProvider().resolve_viewer_login(_project(), "tok")
    assert identity.reason == "bad_credentials"
    assert identity.login is None


def test_resolve_viewer_login_404_is_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404"}, status_code=404)

    _install_mock(monkeypatch, handler)
    identity = GitLabProvider().resolve_viewer_login(_project(), "tok")
    assert identity.reason == "bad_credentials"


def test_resolve_viewer_login_transport_error_is_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock(monkeypatch, handler)
    identity = GitLabProvider().resolve_viewer_login(_project(), "tok")
    assert identity.reason == "network_error"
    assert identity.login is None


def test_resolve_viewer_login_missing_username_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"name": "no username here"})

    _install_mock(monkeypatch, handler)
    identity = GitLabProvider().resolve_viewer_login(_project(), "tok")
    assert identity.reason == "identity_field_missing"
    assert identity.login is None


def test_resolve_viewer_login_500_is_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "boom"}, status_code=500)

    _install_mock(monkeypatch, handler)
    identity = GitLabProvider().resolve_viewer_login(_project(), "tok")
    assert identity.reason == "http_500"


def test_resolve_viewer_login_non_json_body_is_identity_field_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            content=b"not json",
            headers={"Content-Type": "text/plain"},
        )

    _install_mock(monkeypatch, handler)
    identity = GitLabProvider().resolve_viewer_login(_project(), "tok")
    assert identity.reason == "identity_field_missing"
    assert identity.login is None
