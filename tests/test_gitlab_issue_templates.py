"""Driving tests for `GitLabProvider.list_issue_templates` (ticket #259,
`IssueTemplateProvider` mixin).

Mocking follows `tests/test_gitlab_issues.py`'s `httpx.MockTransport`
convention exactly (`_install_mock`/`_json`, `gitlab_mod._client(project,
token)` signature).

`GitLabProvider.list_issue_templates` doesn't exist yet (phase=implement
writes it) -- every test here is expected to fail with `AttributeError`:
`'GitLabProvider' object has no attribute 'list_issue_templates'`.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
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
        headers = {"Accept": "application/json"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(
            base_url=gitlab_mod._base_url(project), headers=headers, transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


_LISTING_PATH_SUFFIX = "/templates/issues"


def _standard_handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path.endswith(_LISTING_PATH_SUFFIX):
        return _json([
            {"key": "bug", "name": "bug"},
            {"key": "feature request", "name": "feature request"},
        ])
    if path.endswith(f"{_LISTING_PATH_SUFFIX}/bug"):
        return _json({"name": "bug", "content": "## Summary\n\nDescribe the bug.\n"})
    if path.endswith(f"{_LISTING_PATH_SUFFIX}/feature%20request") or path.endswith(
        f"{_LISTING_PATH_SUFFIX}/feature request"
    ):
        return _json({"name": "feature request", "content": "## Proposal\n\nDescribe the feature.\n"})
    raise AssertionError(f"unexpected request: {req.method} {path}")


# ---------- requirement 1: parses templates listing -------------------------


def test_list_issue_templates_returns_markdown_kind_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(monkeypatch, _standard_handler)
    result = GitLabProvider().list_issue_templates(_project(), "tok")

    by_name = {t.name: t for t in result}
    assert set(by_name) == {"bug", "feature request"}

    bug = by_name["bug"]
    assert bug.filename == "bug"
    assert bug.kind == "markdown"
    assert bug.fields == []
    assert "Describe the bug." in bug.raw_body
    # title_prefix/labels default to "" / [] (never None) for cross-provider
    # consistency, even though GitLab issue templates have no such concept.
    assert bug.title_prefix == ""
    assert bug.labels == []

    feature = by_name["feature request"]
    assert feature.filename == "feature request"
    assert "Describe the feature." in feature.raw_body


# ---------- requirement 2: error contract -----------------------------------


def test_list_issue_templates_404_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404 Project Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    assert GitLabProvider().list_issue_templates(_project(), "tok") == []


def test_list_issue_templates_empty_listing_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    assert GitLabProvider().list_issue_templates(_project(), "tok") == []


@pytest.mark.parametrize("status", [401, 403, 500])
def test_list_issue_templates_auth_and_server_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "boom"}, status_code=status)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().list_issue_templates(_project(), "tok")
    assert exc.value.status == status
