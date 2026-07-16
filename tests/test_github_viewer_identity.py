"""Viewer-identity ("whoami") resolution tests (ticket #191).

The GitHub provider learns which account a token authenticates as by
calling `GET /user`. These tests exercise the happy path and every
documented failure mode (401, network error, other non-2xx, missing
`login` field).
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.base import ViewerIdentity
from lib_python_projects.providers.github import GitHubProvider


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
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
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
        assert req.url.path == "/user"
        return _json({"login": "octocat", "name": "The Octocat"})

    seen = _install_mock(monkeypatch, handler)
    identity = GitHubProvider().resolve_viewer_login(_project(), token="t")
    assert identity == ViewerIdentity(
        login="octocat", display_name="The Octocat", provider="github", reason=None,
    )
    assert len(seen) == 1


# ---------- failure modes -----------------------------------------------------


def test_resolve_viewer_login_401_sets_reason_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Bad credentials"}, status_code=401)

    _install_mock(monkeypatch, handler)
    identity = GitHubProvider().resolve_viewer_login(_project(), token="t")
    assert identity.reason == "bad_credentials"
    assert identity.login is None
    assert identity.display_name is None


def test_resolve_viewer_login_network_error_sets_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport-level failures map to `network_error`, not a raise."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns resolution failed", request=req)

    _install_mock(monkeypatch, handler)
    identity = GitHubProvider().resolve_viewer_login(_project(), token="t")
    assert identity.reason == "network_error"
    assert identity.login is None


def test_resolve_viewer_login_403_sets_reason_http_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Forbidden"}, status_code=403)

    _install_mock(monkeypatch, handler)
    identity = GitHubProvider().resolve_viewer_login(_project(), token="t")
    assert identity.reason == "http_403"
    assert identity.login is None


def test_resolve_viewer_login_missing_login_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"name": "No Login Here"})

    _install_mock(monkeypatch, handler)
    identity = GitHubProvider().resolve_viewer_login(_project(), token="t")
    assert identity.reason == "identity_field_missing"
    assert identity.login is None
    assert identity.display_name is None


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
    identity = GitHubProvider().resolve_viewer_login(_project(), token="t")
    assert identity.reason == "identity_field_missing"
    assert identity.login is None
    assert identity.display_name is None
