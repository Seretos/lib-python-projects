"""Tests for `GitLabProvider.probe_token_capabilities`.

Covers the scope → capability mapping and every documented failure
mode: bad_credentials (401/404), network_error, permissions_field_missing.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.base import TokenCapabilities
from lib_python_projects.providers.gitlab import GitLabProvider


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="gitlab", path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)

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


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


# ---------- success paths ---------------------------------------------------


def test_probe_api_scope_grants_full_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v4/personal_access_tokens/self"
        return _json({"scopes": ["api"]})

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.issues_create is True
    assert caps.issues_modify is True
    assert caps.pulls_create is True
    assert caps.pulls_modify is True
    assert caps.pulls_merge is True
    assert caps.reason is None


def test_probe_read_api_scope_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`read_api` is read-only — no write capabilities granted."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"scopes": ["read_api"]})

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.issues_create is False
    assert caps.issues_modify is False
    assert caps.pulls_create is False
    assert caps.reason == "insufficient_scope"


def test_probe_unknown_scope_combo_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"scopes": ["read_repository", "read_registry"]})

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.reason == "insufficient_scope"
    assert caps.issues_create is False


# ---------- failure modes ---------------------------------------------------


def test_probe_401_is_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "401 Unauthorized"}, status_code=401)

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.reason == "bad_credentials"
    assert caps.issues_create is False
    assert caps.pulls_merge is False


def test_probe_404_is_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/personal_access_tokens/self` returning 404 means the token is
    invalid (the endpoint exists on any reasonable GitLab version).
    Map to `bad_credentials` rather than a misleading network_error."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404"}, status_code=404)

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.reason == "bad_credentials"


def test_probe_500_is_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "boom"}, status_code=500)

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.reason == "network_error"


def test_probe_transport_error_is_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection failure raises httpx.HTTPError; the probe must
    not propagate it — it returns reason='network_error' instead."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.reason == "network_error"


def test_probe_response_missing_scopes_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"name": "my-token"})  # no `scopes` key

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.reason == "permissions_field_missing"


def test_probe_non_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    _install_mock(monkeypatch, handler)
    caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
    assert caps.reason == "permissions_field_missing"


# ---------- safety net ------------------------------------------------------


def test_probe_failure_means_all_flags_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across every failure path, NO operation may be granted."""
    cases = [
        lambda req: _json({}, 401),
        lambda req: _json({}, 500),
        lambda req: _json({"no_scopes": True}),
    ]
    for handler in cases:
        _install_mock(monkeypatch, handler)
        caps = GitLabProvider().probe_token_capabilities(_project(), "tok")
        for flag in (
            caps.issues_create, caps.issues_modify,
            caps.pulls_create, caps.pulls_modify, caps.pulls_merge,
        ):
            assert flag is False, f"granted on failure path: {caps}"
        assert caps.reason is not None
