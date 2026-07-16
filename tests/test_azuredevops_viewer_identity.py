"""Viewer-identity ("whoami") resolution tests (ticket #191).

Covers `AzureDevOpsProvider.resolve_viewer_login`'s happy path (reusing
the `connectionData` endpoint's `authenticatedUser` field) and every
documented failure mode: empty token, 401, 403/404, other non-2xx,
transport error, and an empty/unresolvable `authenticatedUser`.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)
from lib_python_projects.providers.base import ViewerIdentity


def _project(path: str = "seredos/azure-tests/azure-tests") -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path=path,
        token_env="AZURE_TOKEN",
    )


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    _cache_clear_all()


# ---------- happy path -------------------------------------------------------


def test_resolve_viewer_login_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/connectionData"):
            return _json({
                "authenticatedUser": {
                    "id": "u1",
                    "uniqueName": "alice@example.com",
                    "displayName": "Alice Example",
                },
            })
        raise AssertionError

    seen = _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity == ViewerIdentity(
        login="alice@example.com",
        display_name="Alice Example",
        provider="azuredevops",
        reason=None,
    )
    assert len(seen) == 1


# ---------- failure modes -----------------------------------------------------


def test_resolve_viewer_login_empty_token_makes_no_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty token")

    seen = _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "")
    assert identity.reason == "bad_credentials"
    assert identity.login is None
    assert len(seen) == 0


def test_resolve_viewer_login_401_means_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "TF400813"}, status_code=401)

    _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity.reason == "bad_credentials"
    assert identity.login is None


@pytest.mark.parametrize("status", [403, 404])
def test_resolve_viewer_login_403_404_means_invisible(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "forbidden"}, status_code=status)

    _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity.reason == "repo_invisible_to_token"
    assert identity.login is None


def test_resolve_viewer_login_empty_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"authenticatedUser": {}})

    _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity.reason == "identity_field_missing"
    assert identity.login is None


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
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity == ViewerIdentity(reason="identity_field_missing")
    assert identity.login is None


def test_resolve_viewer_login_non_dict_json_body_is_identity_field_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json([1, 2, 3])

    _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity == ViewerIdentity(reason="identity_field_missing")
    assert identity.login is None


def test_resolve_viewer_login_transport_error_is_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity.reason == "network_error"
    assert identity.login is None


def test_resolve_viewer_login_500_is_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "boom"}, status_code=500)

    _install_mock(monkeypatch, handler)
    identity = AzureDevOpsProvider().resolve_viewer_login(_project(), "PAT")
    assert identity.reason == "http_500"
