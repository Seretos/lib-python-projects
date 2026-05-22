"""Token-capability probe tests (ticket #32).

The GitHub provider learns what a token may actually do against a
project by calling `GET /repos/{owner}/{repo}` and reading the
`permissions` block GitHub attaches to the response. These tests
exercise the mapping (admin/maintain/push/triage/pull -> nested
issues/pulls flags) and every documented failure mode (401, 404,
network error, missing field).
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.base import TokenCapabilities
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


def _repo_payload(permissions: dict | None) -> dict:
    body: dict = {
        "id": 1,
        "name": "backend",
        "full_name": "acme/backend",
        "private": False,
    }
    if permissions is not None:
        body["permissions"] = permissions
    return body


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


# ---------- happy path: permission mapping -----------------------------------


def test_token_probe_admin_grants_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """`admin: true` -> every write capability is True, reason is None."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend"
        return _json(_repo_payload({
            "admin": True, "maintain": True, "push": True,
            "triage": True, "pull": True,
        }))

    seen = _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps == TokenCapabilities(
        issues_create=True, issues_modify=True,
        pulls_create=True, pulls_modify=True, pulls_merge=True,
        reason=None,
    )
    assert len(seen) == 1


def test_token_probe_push_grants_create_modify_not_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`push: true` (no maintain/admin) grants issues+pulls create/modify
    but NOT pulls.merge."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_repo_payload({
            "admin": False, "maintain": False, "push": True,
            "triage": True, "pull": True,
        }))

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.issues_create is True
    assert caps.issues_modify is True
    assert caps.pulls_create is True
    assert caps.pulls_modify is True
    assert caps.pulls_merge is False
    assert caps.reason is None


def test_token_probe_pull_only_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pull: true` alone grants NO write capability."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_repo_payload({
            "admin": False, "maintain": False, "push": False,
            "triage": False, "pull": True,
        }))

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.issues_create is False
    assert caps.issues_modify is False
    assert caps.pulls_create is False
    assert caps.pulls_modify is False
    assert caps.pulls_merge is False
    assert caps.reason is None


def test_token_probe_triage_grants_issues_modify_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`triage: true` (no push) lets the token re-label/assign issues
    but not create them or touch PRs."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_repo_payload({
            "admin": False, "maintain": False, "push": False,
            "triage": True, "pull": True,
        }))

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.issues_modify is True
    assert caps.issues_create is False
    assert caps.pulls_create is False
    assert caps.pulls_merge is False


def test_token_probe_maintain_grants_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`maintain: true` (no admin) grants pulls.merge, plus everything
    push implies."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_repo_payload({
            "admin": False, "maintain": True, "push": True,
            "triage": True, "pull": True,
        }))

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.pulls_merge is True
    assert caps.pulls_create is True
    assert caps.issues_create is True


# ---------- failure modes ----------------------------------------------------


def test_token_probe_401_sets_reason_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Bad credentials"}, status_code=401)

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.reason == "bad_credentials"
    # Every write flag must be False on failure (defensive default).
    assert caps.issues_create is False
    assert caps.issues_modify is False
    assert caps.pulls_create is False
    assert caps.pulls_modify is False
    assert caps.pulls_merge is False


def test_token_probe_404_sets_reason_invisible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub returns 404 when the token can't see the repo (whether or
    not it actually exists)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.reason == "repo_invisible_to_token"
    assert caps.issues_create is False
    assert caps.pulls_merge is False


def test_token_probe_network_error_sets_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport-level failures map to `network_error`, not a raise."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns resolution failed", request=req)

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.reason == "network_error"
    assert caps.issues_create is False


def test_token_probe_missing_permissions_field_preserves_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the response succeeds but `permissions` is absent (classic PAT
    response shape variant), the probe returns all-False + a stable
    reason so the caller can fall back gracefully."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_repo_payload(None))

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.reason == "permissions_field_missing"
    assert caps.issues_create is False
    assert caps.issues_modify is False
    assert caps.pulls_create is False
    assert caps.pulls_modify is False
    assert caps.pulls_merge is False


def test_token_probe_fine_grained_pat_with_contents_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents that fine-grained PATs DO populate `permissions` on
    `GET /repos/...` — the same code path as classic-PAT collaborator
    permissions applies. A fine-grained PAT with read-only repo
    contents looks like a `pull: true` response."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_repo_payload({
            "admin": False, "maintain": False, "push": False,
            "triage": False, "pull": True,
        }))

    _install_mock(monkeypatch, handler)
    caps = GitHubProvider().probe_token_capabilities(_project(), token="t")
    assert caps.reason is None
    assert caps.issues_create is False
    assert caps.pulls_create is False
