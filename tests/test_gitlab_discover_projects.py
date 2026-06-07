"""Tests for `GitLabProvider.discover_projects`.

Covers:
- Single-page happy path
- Pagination (stops at limit, exhausts pages)
- limit < page size
- Partial last page
- Access-level → TokenCapabilities mapping (unit)
- _extract_access_level (unit)
- Error paths: 401, 403, 500, network error
- Edge: empty membership list, null permissions field, missing description
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.base import (
    DiscoveredProject,
    ProjectDiscoveryResult,
    TokenCapabilities,
)
from lib_python_projects.providers.gitlab import (
    GitLabProvider,
    _capabilities_from_access_level,
    _extract_access_level,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project_item(
    path: str,
    access_level: int | None = 40,
    description: str = "A project",
) -> dict:
    """Build a minimal GitLab /projects list item."""
    if access_level is None:
        permissions = {"project_access": None, "group_access": None}
    else:
        permissions = {
            "project_access": {"access_level": access_level},
            "group_access": None,
        }
    return {
        "path_with_namespace": path,
        "description": description,
        "permissions": permissions,
    }


def _json_response(
    payload,
    status_code: int = 200,
    next_page: str = "",
) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if next_page:
        headers["X-Next-Page"] = next_page
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode(),
        headers=headers,
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Patch _discovery_client to use a mock transport backed by *handler*."""
    transport = httpx.MockTransport(handler)

    def fake_discovery_client(base_url: str, token: str) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            headers={"Accept": "application/json", "PRIVATE-TOKEN": token},
            transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_discovery_client", fake_discovery_client)


# ---------------------------------------------------------------------------
# Basic single-page test
# ---------------------------------------------------------------------------

def test_discover_projects_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single page with 2 projects (access_level=40), no X-Next-Page."""
    items = [
        _make_project_item("group/alpha", access_level=40),
        _make_project_item("group/beta", access_level=40),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/api/v4/projects" in str(req.url)
        return _json_response(items, next_page="")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=50)

    assert isinstance(result, ProjectDiscoveryResult)
    assert result.truncated is False
    assert result.reason is None
    assert len(result.projects) == 2

    alpha = result.projects[0]
    assert isinstance(alpha, DiscoveredProject)
    assert alpha.provider == "gitlab"
    assert alpha.path == "group/alpha"
    assert alpha.description == "A project"
    assert alpha.permissions.issues_create is True
    assert alpha.permissions.issues_modify is True
    assert alpha.permissions.pulls_create is True
    assert alpha.permissions.pulls_modify is True
    assert alpha.permissions.pulls_merge is True
    assert alpha.permissions.reason is None

    beta = result.projects[1]
    assert beta.path == "group/beta"


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------

def test_discover_pagination_stops_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Page 1 has 100 items + X-Next-Page:2; limit=100 → truncated=True."""
    page1 = [_make_project_item(f"g/p{i}") for i in range(100)]

    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        assert call_count[0] == 1, "Should only request one page when limit reached"
        return _json_response(page1, next_page="2")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=100)

    assert len(result.projects) == 100
    assert result.truncated is True
    assert result.reason is None


def test_discover_pagination_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two pages: page1=100 items next=2, page2=3 items no next; limit=200."""
    page1 = [_make_project_item(f"g/p{i}") for i in range(100)]
    page2 = [_make_project_item(f"g/q{i}") for i in range(3)]

    pages = {"1": (page1, "2"), "2": (page2, "")}
    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        page_num = req.url.params.get("page", "1")
        items, next_p = pages[str(page_num)]
        return _json_response(items, next_page=next_p)

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=200)

    assert len(result.projects) == 103
    assert result.truncated is False
    assert result.reason is None
    assert call_count[0] == 2


def test_discover_limit_smaller_than_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """limit=5 with page1=100 items + X-Next-Page:2 → 5 projects, truncated=True."""
    page1 = [_make_project_item(f"g/p{i}") for i in range(100)]

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(page1, next_page="2")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=5)

    assert len(result.projects) == 5
    assert result.truncated is True
    assert result.reason is None


def test_discover_partial_last_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two pages: page1=100+next, page2=3 no next; limit=200 → 103, truncated=False."""
    page1 = [_make_project_item(f"g/p{i}") for i in range(100)]
    page2 = [_make_project_item(f"g/q{i}") for i in range(3)]

    def handler(req: httpx.Request) -> httpx.Response:
        page_num = req.url.params.get("page", "1")
        if str(page_num) == "1":
            return _json_response(page1, next_page="2")
        return _json_response(page2, next_page="")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=200)

    assert len(result.projects) == 103
    assert result.truncated is False
    assert result.reason is None


# ---------------------------------------------------------------------------
# Access-level mapping unit tests (call helpers directly)
# ---------------------------------------------------------------------------

def test_capabilities_owner_50() -> None:
    caps = _capabilities_from_access_level(50)
    assert caps.issues_create is True
    assert caps.issues_modify is True
    assert caps.pulls_create is True
    assert caps.pulls_modify is True
    assert caps.pulls_merge is True
    assert caps.reason is None


def test_capabilities_maintainer_40() -> None:
    caps = _capabilities_from_access_level(40)
    assert caps.issues_create is True
    assert caps.issues_modify is True
    assert caps.pulls_create is True
    assert caps.pulls_modify is True
    assert caps.pulls_merge is True
    assert caps.reason is None


def test_capabilities_developer_30() -> None:
    caps = _capabilities_from_access_level(30)
    assert caps.issues_create is True
    assert caps.issues_modify is True
    assert caps.pulls_create is True
    assert caps.pulls_modify is False
    assert caps.pulls_merge is False
    assert caps.reason is None


def test_capabilities_reporter_20() -> None:
    caps = _capabilities_from_access_level(20)
    assert caps.issues_create is False
    assert caps.issues_modify is False
    assert caps.pulls_create is False
    assert caps.pulls_modify is False
    assert caps.pulls_merge is False
    assert caps.reason == "insufficient_scope"


def test_capabilities_guest_10() -> None:
    caps = _capabilities_from_access_level(10)
    assert caps.issues_create is False
    assert caps.reason == "insufficient_scope"


def test_capabilities_none_level() -> None:
    caps = _capabilities_from_access_level(None)
    assert caps.issues_create is False
    assert caps.issues_modify is False
    assert caps.pulls_create is False
    assert caps.pulls_modify is False
    assert caps.pulls_merge is False
    assert caps.reason == "permissions_field_missing"


# ---------------------------------------------------------------------------
# _extract_access_level unit tests
# ---------------------------------------------------------------------------

def test_extract_project_wins_over_group() -> None:
    """project_access=40, group_access=30 → 40."""
    perms = {
        "project_access": {"access_level": 40},
        "group_access": {"access_level": 30},
    }
    assert _extract_access_level(perms) == 40


def test_extract_group_wins_when_project_null() -> None:
    """project_access=None, group_access=30 → 30."""
    perms = {
        "project_access": None,
        "group_access": {"access_level": 30},
    }
    assert _extract_access_level(perms) == 30


def test_extract_both_null_returns_none() -> None:
    perms = {"project_access": None, "group_access": None}
    assert _extract_access_level(perms) is None


def test_extract_max_used() -> None:
    """group_access=40, project_access=30 → 40 (max wins)."""
    perms = {
        "project_access": {"access_level": 30},
        "group_access": {"access_level": 40},
    }
    assert _extract_access_level(perms) == 40


def test_extract_empty_dict() -> None:
    assert _extract_access_level({}) is None


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_discover_401_returns_bad_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response({"message": "401 Unauthorized"}, status_code=401)

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("bad-tok", limit=50)

    assert result.projects == []
    assert result.reason == "bad_credentials"
    assert result.reason != "http_401"  # 401 must be caught before the generic http_<code> branch


def test_discover_403_returns_http_403(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Forbidden"}, status_code=403)

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=50)

    assert result.projects == []
    assert result.reason == "http_403"


def test_discover_500_returns_http_500(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Internal Server Error"}, status_code=500)

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=50)

    assert result.projects == []
    assert result.reason == "http_500"


def test_discover_network_error_returns_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=50)

    assert result.projects == []
    assert result.reason == "network_error"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_discover_empty_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty project list → 0 projects, truncated=False, reason=None."""
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response([], next_page="")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=50)

    assert result.projects == []
    assert result.truncated is False
    assert result.reason is None


def test_discover_null_permissions_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Project with permissions=null → entry included, reason=permissions_field_missing."""
    items = [
        {
            "path_with_namespace": "group/no-perms",
            "description": "desc",
            "permissions": None,
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(items)

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=50)

    assert len(result.projects) == 1
    proj = result.projects[0]
    assert proj.path == "group/no-perms"
    assert proj.permissions.reason == "permissions_field_missing"
    assert proj.permissions.issues_create is False
    assert proj.permissions.pulls_merge is False


def test_discover_missing_description(monkeypatch: pytest.MonkeyPatch) -> None:
    """Project without description key → description=''."""
    items = [
        {
            "path_with_namespace": "group/nodesc",
            "permissions": {
                "project_access": {"access_level": 40},
                "group_access": None,
            },
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(items)

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().discover_projects("tok", limit=50)

    assert len(result.projects) == 1
    assert result.projects[0].description == ""


# ---------------------------------------------------------------------------
# limit validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_discover_nonpositive_limit_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError without any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        GitLabProvider().discover_projects("tok", limit=bad_limit)
