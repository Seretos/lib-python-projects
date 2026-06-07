"""Tests for AzureDevOpsProvider.discover_projects (ticket #83).

Mock infra: patch ``azure_mod._client`` with a ``fake_client`` that routes
requests to one of two ``httpx.MockTransport`` instances depending on whether
``base_url`` equals ``https://app.vssps.visualstudio.com`` (VSSPS) or
anything else (dev.azure.com).
"""
from __future__ import annotations

import json
from typing import Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest

from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
    _org_hint,
    _discover_orgs_via_api,
)
from lib_python_projects.providers.base import (
    DiscoveredProject,
    ProjectDiscoveryResult,
    TokenCapabilities,
    TokenProjectDiscoveryProvider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VSSPS_BASE = "https://app.vssps.visualstudio.com"


def _json_resp(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _profile_payload(member_id: str = "user-123") -> dict:
    return {"id": member_id, "displayName": "Test User"}


def _accounts_payload(org_names: list[str]) -> dict:
    return {"value": [{"accountName": n} for n in org_names]}


def _projects_payload(names: list[str]) -> dict:
    return {"value": [{"name": n} for n in names]}


def _repos_payload(names: list[str]) -> dict:
    return {
        "value": [
            {"name": n, "remoteUrl": f"https://dev.azure.com/org/{n}.git"}
            for n in names
        ]
    }


def _install_dual_mock(
    monkeypatch: pytest.MonkeyPatch,
    vssps_handler: Callable[[httpx.Request], httpx.Response],
    ado_handler: Callable[[httpx.Request], httpx.Response],
    vssps_calls: list | None = None,
    ado_calls: list | None = None,
) -> tuple[list[httpx.Request], list[httpx.Request]]:
    """Patch ``azure_mod._client`` to route requests by base_url."""
    seen_vssps: list[httpx.Request] = [] if vssps_calls is None else vssps_calls
    seen_ado: list[httpx.Request] = [] if ado_calls is None else ado_calls

    vssps_transport = httpx.MockTransport(
        lambda req: (seen_vssps.append(req), vssps_handler(req))[1]
    )
    ado_transport = httpx.MockTransport(
        lambda req: (seen_ado.append(req), ado_handler(req))[1]
    )

    from lib_python_projects.models import ProjectConfig

    def fake_client(
        project: ProjectConfig,
        token: str | None,
        *,
        base_url: str | None = None,
    ) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "test"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        effective_base = (
            base_url or project.base_url or "https://dev.azure.com"
        ).rstrip("/")
        if effective_base == VSSPS_BASE:
            transport = vssps_transport
        else:
            transport = ado_transport
        return httpx.Client(
            base_url=effective_base, headers=headers, transport=transport
        )

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen_vssps, seen_ado


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    _cache_clear_all()


# ---------------------------------------------------------------------------
# _org_hint unit tests
# ---------------------------------------------------------------------------


class TestOrgHint:
    def test_returns_org_from_base_url_path(self):
        assert _org_hint("https://dev.azure.com/myorg") == "myorg"

    def test_returns_first_segment_only(self):
        assert _org_hint("https://dev.azure.com/myorg/extra") == "myorg"

    def test_returns_none_when_no_path(self):
        # Plain host with no path segment
        assert _org_hint("https://dev.azure.com") is None
        assert _org_hint("https://dev.azure.com/") is None

    def test_returns_none_when_base_url_none(self, monkeypatch):
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)
        assert _org_hint(None) is None

    def test_env_var_used_when_no_base_url(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "envorg")
        assert _org_hint(None) == "envorg"

    def test_base_url_takes_priority_over_env_var(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "envorg")
        assert _org_hint("https://dev.azure.com/urlorg") == "urlorg"


# ---------------------------------------------------------------------------
# _discover_orgs_via_api unit tests
# ---------------------------------------------------------------------------


class TestDiscoverOrgsViaApi:
    def test_returns_org_list_on_success(self, monkeypatch):
        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("uid-1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["org1", "org2"]))
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, lambda r: _json_resp({}, 404))
        orgs, reason = _discover_orgs_via_api("token")
        assert reason is None
        assert orgs == ["org1", "org2"]

    def test_network_error_on_profile(self, monkeypatch):
        from lib_python_projects.models import ProjectConfig

        def fake_client(project, token, *, base_url=None):
            effective = (base_url or "https://dev.azure.com").rstrip("/")
            if effective == VSSPS_BASE:
                raise httpx.ConnectError("refused")
            return httpx.Client(base_url=effective)

        monkeypatch.setattr(azure_mod, "_client", fake_client)
        orgs, reason = _discover_orgs_via_api("token")
        assert orgs == []
        assert reason == "network_error"

    def test_401_on_profile_returns_bad_credentials(self, monkeypatch):
        _install_dual_mock(
            monkeypatch,
            lambda req: _json_resp({"message": "Unauthorized"}, 401),
            lambda req: _json_resp({}, 404),
        )
        orgs, reason = _discover_orgs_via_api("token")
        assert orgs == []
        assert reason == "bad_credentials"

    def test_403_on_profile_returns_http_403(self, monkeypatch):
        _install_dual_mock(
            monkeypatch,
            lambda req: _json_resp({"message": "Forbidden"}, 403),
            lambda req: _json_resp({}, 404),
        )
        orgs, reason = _discover_orgs_via_api("token")
        assert orgs == []
        assert reason == "http_403"

    def test_401_on_accounts_returns_bad_credentials(self, monkeypatch):
        call_count = [0]

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("uid-1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp({"message": "Unauthorized"}, 401)
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, lambda r: _json_resp({}, 404))
        orgs, reason = _discover_orgs_via_api("token")
        assert orgs == []
        assert reason == "bad_credentials"

    def test_empty_accounts_value_returns_repo_invisible(self, monkeypatch):
        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("uid-1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp({"value": []})
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, lambda r: _json_resp({}, 404))
        orgs, reason = _discover_orgs_via_api("token")
        assert orgs == []
        assert reason == "repo_invisible_to_token"


# ---------------------------------------------------------------------------
# discover_projects — happy-path tests
# ---------------------------------------------------------------------------


class TestDiscoverProjectsHappyPath:
    def test_env_var_org_hint(self, monkeypatch):
        """AZURE_DEVOPS_ORG env var → single org queried, no VSSPS call."""
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "myorg")

        seen_vssps: list[httpx.Request] = []
        seen_ado: list[httpx.Request] = []

        def ado_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/_apis/projects" in path and "/_apis/git" not in path:
                return _json_resp(_projects_payload(["MyProject"]))
            if "/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["MyRepo"]))
            return _json_resp({}, 404)

        _install_dual_mock(
            monkeypatch,
            lambda r: _json_resp({}, 404),
            ado_handler,
            seen_vssps,
            seen_ado,
        )

        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)

        # No VSSPS calls made
        assert seen_vssps == []
        assert isinstance(result, ProjectDiscoveryResult)
        assert len(result.projects) == 1
        dp = result.projects[0]
        assert dp.provider == "azuredevops"
        assert dp.path == "myorg/MyProject/MyRepo"
        assert result.truncated is False
        assert result.reason is None

    def test_full_accounts_api_path(self, monkeypatch):
        """No env var → VSSPS profile+accounts → discover repos."""
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["testorg"]))
            return _json_resp({}, 404)

        def ado_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/_apis/projects" in path and "/_apis/git" not in path:
                return _json_resp(_projects_payload(["Proj1"]))
            if "/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["Repo1"]))
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, ado_handler)

        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)

        assert len(result.projects) == 1
        assert result.projects[0].path == "testorg/Proj1/Repo1"
        assert result.reason is None

    def test_multiple_orgs_from_accounts_api(self, monkeypatch):
        """Repos from two orgs appear with correct org namespace."""
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["org-a", "org-b"]))
            return _json_resp({}, 404)

        def ado_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/org-a/_apis/projects" in path:
                return _json_resp(_projects_payload(["ProjA"]))
            if "/org-a/ProjA/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["RepoA"]))
            if "/org-b/_apis/projects" in path:
                return _json_resp(_projects_payload(["ProjB"]))
            if "/org-b/ProjB/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["RepoB"]))
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, ado_handler)

        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)

        paths = {dp.path for dp in result.projects}
        assert "org-a/ProjA/RepoA" in paths
        assert "org-b/ProjB/RepoB" in paths
        assert result.truncated is False

    def test_description_is_remote_url_when_present(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")

        def ado_handler(req: httpx.Request) -> httpx.Response:
            if "/_apis/projects" in req.url.path and "git" not in req.url.path:
                return _json_resp(_projects_payload(["P"]))
            if "/_apis/git/repositories" in req.url.path:
                return _json_resp({
                    "value": [
                        {
                            "name": "MyRepo",
                            "remoteUrl": "https://org@dev.azure.com/org/P/_git/MyRepo",
                        }
                    ]
                })
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=10)
        assert result.projects[0].description == (
            "https://org@dev.azure.com/org/P/_git/MyRepo"
        )

    def test_description_empty_when_remote_url_absent(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")

        def ado_handler(req: httpx.Request) -> httpx.Response:
            if "/_apis/projects" in req.url.path and "git" not in req.url.path:
                return _json_resp(_projects_payload(["P"]))
            if "/_apis/git/repositories" in req.url.path:
                return _json_resp({"value": [{"name": "MyRepo"}]})
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=10)
        assert result.projects[0].description == ""

    def test_permissions_wired_from_probe_token_capabilities(self, monkeypatch):
        """probe_token_capabilities result is reflected in DiscoveredProject.permissions."""
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")

        def ado_handler(req: httpx.Request) -> httpx.Response:
            if "/_apis/projects" in req.url.path and "git" not in req.url.path:
                return _json_resp(_projects_payload(["P"]))
            if "/_apis/git/repositories" in req.url.path:
                return _json_resp({"value": [{"name": "R"}]})
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), ado_handler)

        known_caps = TokenCapabilities(
            issues_create=True,
            issues_modify=False,
            pulls_create=True,
            pulls_modify=False,
            pulls_merge=False,
        )
        provider = AzureDevOpsProvider()
        with patch.object(provider, "probe_token_capabilities", return_value=known_caps):
            result = provider.discover_projects(token="tok", limit=50)

        assert result.projects[0].permissions is known_caps

    def test_org_with_zero_repos_yields_no_entries(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")

        def ado_handler(req: httpx.Request) -> httpx.Response:
            if "/_apis/projects" in req.url.path and "git" not in req.url.path:
                return _json_resp(_projects_payload(["Empty"]))
            if "/_apis/git/repositories" in req.url.path:
                return _json_resp({"value": []})
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.projects == []
        assert result.truncated is False
        assert result.reason is None


# ---------------------------------------------------------------------------
# limit tests
# ---------------------------------------------------------------------------


class TestDiscoverProjectsLimit:
    def _make_ado_handler(self, repos_per_project: dict[str, list[str]]):
        """Build an ADO handler that returns configurable repos per project."""

        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            # projects endpoint
            if "/_apis/projects" in path and "git" not in path:
                # extract org from path: /{org}/_apis/projects
                parts = [s for s in path.split("/") if s]
                org = parts[0]
                proj_names = list(repos_per_project.keys())
                return _json_resp(_projects_payload(proj_names))
            # repos endpoint: /{org}/{proj}/_apis/git/repositories
            for proj, repos in repos_per_project.items():
                if f"/{proj}/_apis/git/repositories" in path:
                    return _json_resp(_repos_payload(repos))
            return _json_resp({}, 404)

        return handler

    def test_limit_greater_than_total_no_truncation(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")
        handler = self._make_ado_handler({"P": ["R1", "R2", "R3"]})
        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=100)
        assert len(result.projects) == 3
        assert result.truncated is False

    def test_limit_caps_result(self, monkeypatch):
        """3 repos, limit=2 → truncated=True, exactly 2 entries."""
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")
        probe_calls: list = []

        handler = self._make_ado_handler({"P": ["R1", "R2", "R3"]})
        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), handler)

        provider = AzureDevOpsProvider()
        original_probe = provider.probe_token_capabilities

        def tracked_probe(cfg, tok):
            probe_calls.append(cfg.path)
            return original_probe(cfg, tok)

        with patch.object(provider, "probe_token_capabilities", side_effect=tracked_probe):
            # patch _client for probe calls too (already patched via monkeypatch)
            result = provider.discover_projects(token="tok", limit=2)

        assert len(result.projects) == 2
        assert result.truncated is True
        # R3 must NOT have been capability-probed
        assert not any("R3" in p for p in probe_calls)

    def test_limit_exact_match(self, monkeypatch):
        """Exactly limit repos → truncated=False."""
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")
        handler = self._make_ado_handler({"P": ["R1", "R2"]})
        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=2)
        assert len(result.projects) == 2
        assert result.truncated is False

    def test_limit_zero(self, monkeypatch):
        """limit=0 → empty, truncated=False, no ADO calls."""
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)
        seen_ado: list[httpx.Request] = []

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["org"]))
            return _json_resp({}, 404)

        def ado_handler(req: httpx.Request) -> httpx.Response:
            seen_ado.append(req)
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=0)
        assert result.projects == []
        assert result.truncated is False
        # The limit <= 0 guard fires at the top of discover_projects before any
        # HTTP call — neither VSSPS nor ADO endpoints are reached at all.
        repo_calls = [r for r in seen_ado if "git/repositories" in r.url.path]
        assert repo_calls == []

    def test_limit_two_orgs_caps_across_both(self, monkeypatch):
        """2 orgs × 2 repos each, limit=3 → 3 entries, truncated=True."""
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["org1", "org2"]))
            return _json_resp({}, 404)

        def ado_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/org1/_apis/projects" in path and "git" not in path:
                return _json_resp(_projects_payload(["P"]))
            if "/org1/P/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["R1", "R2"]))
            if "/org2/_apis/projects" in path and "git" not in path:
                return _json_resp(_projects_payload(["P"]))
            if "/org2/P/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["R1", "R2"]))
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=3)
        assert len(result.projects) == 3
        assert result.truncated is True


# ---------------------------------------------------------------------------
# Error / credential failure tests
# ---------------------------------------------------------------------------


class TestDiscoverProjectsErrors:
    def test_empty_token_returns_bad_credentials(self, monkeypatch):
        """Empty token → bad_credentials immediately, no HTTP calls."""
        seen: list[httpx.Request] = []

        def handler(req):
            seen.append(req)
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, handler, handler)
        result = AzureDevOpsProvider().discover_projects(token="", limit=50)
        assert result.reason == "bad_credentials"
        assert result.projects == []
        assert seen == []

    def test_network_error_on_profile_returns_network_error(self, monkeypatch):
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        from lib_python_projects.models import ProjectConfig

        def fake_client(project, token, *, base_url=None):
            effective = (base_url or "https://dev.azure.com").rstrip("/")
            if effective == VSSPS_BASE:
                raise httpx.ConnectError("refused")
            # Should not reach ADO
            transport = httpx.MockTransport(lambda r: _json_resp({}, 404))
            return httpx.Client(base_url=effective, transport=transport)

        monkeypatch.setattr(azure_mod, "_client", fake_client)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.reason == "network_error"
        assert result.projects == []

    def test_401_on_profile_returns_bad_credentials(self, monkeypatch):
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)
        _install_dual_mock(
            monkeypatch,
            lambda r: _json_resp({}, 401),
            lambda r: _json_resp({}, 404),
        )
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.reason == "bad_credentials"

    def test_403_on_profile_returns_http_403(self, monkeypatch):
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)
        _install_dual_mock(
            monkeypatch,
            lambda r: _json_resp({}, 403),
            lambda r: _json_resp({}, 404),
        )
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.reason == "http_403"

    def test_401_on_accounts_returns_bad_credentials(self, monkeypatch):
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        call_n = [0]

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            call_n[0] += 1
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            return _json_resp({}, 401)

        _install_dual_mock(monkeypatch, vssps_handler, lambda r: _json_resp({}, 404))
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.reason == "bad_credentials"

    def test_accounts_empty_value_returns_repo_invisible(self, monkeypatch):
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            return _json_resp({"value": []})

        _install_dual_mock(monkeypatch, vssps_handler, lambda r: _json_resp({}, 404))
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.reason == "repo_invisible_to_token"

    def test_per_org_projects_403_skips_org(self, monkeypatch):
        """403 on /{org}/_apis/projects → org skipped, other orgs included."""
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["bad-org", "good-org"]))
            return _json_resp({}, 404)

        def ado_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/bad-org/_apis/projects" in path:
                return _json_resp({}, 403)
            if "/good-org/_apis/projects" in path and "git" not in path:
                return _json_resp(_projects_payload(["P"]))
            if "/good-org/P/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["R"]))
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.reason is None
        paths = {dp.path for dp in result.projects}
        assert "good-org/P/R" in paths
        assert not any("bad-org" in p for p in paths)

    def test_per_project_repos_500_skips_project(self, monkeypatch):
        """500 on repos for one project → project skipped, others included."""
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "org")

        def ado_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/_apis/projects" in path and "git" not in path:
                return _json_resp(_projects_payload(["BadProj", "GoodProj"]))
            if "/BadProj/_apis/git/repositories" in path:
                return _json_resp({}, 500)
            if "/GoodProj/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["R"]))
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        paths = {dp.path for dp in result.projects}
        assert "org/GoodProj/R" in paths
        assert not any("BadProj" in p for p in paths)

    def test_network_error_on_per_org_call_skips_org(self, monkeypatch):
        """HTTPError when fetching org projects → org skipped gracefully."""
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        from lib_python_projects.models import ProjectConfig

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["net-fail-org", "ok-org"]))
            return _json_resp({}, 404)

        fail_transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")))
        ok_transport = httpx.MockTransport(lambda r: _json_resp(
            _projects_payload(["P"]) if "/_apis/projects" in r.url.path and "git" not in r.url.path
            else _repos_payload(["R"]) if "/_apis/git/repositories" in r.url.path
            else {}
        ))

        vssps_transport = httpx.MockTransport(lambda r: vssps_handler(r))

        def fake_client(project, token, *, base_url=None):
            effective = (base_url or project.base_url or "https://dev.azure.com").rstrip("/")
            if effective == VSSPS_BASE:
                return httpx.Client(base_url=effective, transport=vssps_transport)
            if "net-fail-org" in project.path:
                return httpx.Client(base_url=effective, transport=fail_transport)
            return httpx.Client(base_url=effective, transport=ok_transport)

        monkeypatch.setattr(azure_mod, "_client", fake_client)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        # ok-org repos must be present; net-fail-org skipped without exception
        assert result.reason is None
        paths = {dp.path for dp in result.projects}
        assert "ok-org/P/R" in paths
        assert not any("net-fail-org" in p for p in paths)

    def test_two_orgs_first_fails_second_succeeds(self, monkeypatch):
        """First org projects 500, second org succeeds — only second org's repos.

        Regression guard: multi-org runs where at least one org succeeds must
        return reason=None even if an earlier org errored.
        """
        monkeypatch.delenv("AZURE_DEVOPS_ORG", raising=False)

        def vssps_handler(req: httpx.Request) -> httpx.Response:
            if "profiles/me" in req.url.path:
                return _json_resp(_profile_payload("u1"))
            if "_apis/accounts" in req.url.path:
                return _json_resp(_accounts_payload(["fail-org", "ok-org"]))
            return _json_resp({}, 404)

        def ado_handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/fail-org/_apis/projects" in path:
                return _json_resp({}, 500)
            if "/ok-org/_apis/projects" in path and "git" not in path:
                return _json_resp(_projects_payload(["P"]))
            if "/ok-org/P/_apis/git/repositories" in path:
                return _json_resp(_repos_payload(["R"]))
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, vssps_handler, ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        # Second org succeeded → reason must be None (graceful degradation intact)
        assert result.reason is None
        paths = {dp.path for dp in result.projects}
        assert "ok-org/P/R" in paths
        assert not any("fail-org" in p for p in paths)

    def test_single_org_projects_403_returns_http_403(self, monkeypatch):
        """Regression: single org, projects call 403 → reason='http_403', not None.

        Before the fix this returned reason=None (indistinguishable from
        'token sees zero repos'), violating the ProjectDiscoveryResult contract.
        """
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "myorg")

        def ado_handler(req: httpx.Request) -> httpx.Response:
            if "/myorg/_apis/projects" in req.url.path:
                return _json_resp({}, 403)
            return _json_resp({}, 404)

        _install_dual_mock(monkeypatch, lambda r: _json_resp({}, 404), ado_handler)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.projects == []
        assert result.reason == "http_403"

    def test_single_org_projects_network_error_returns_network_error(self, monkeypatch):
        """Regression: single org, projects call raises ConnectError → reason='network_error'.

        Before the fix this returned reason=None, violating the contract.
        """
        monkeypatch.setenv("AZURE_DEVOPS_ORG", "myorg")

        from lib_python_projects.models import ProjectConfig

        def fake_client(project, token, *, base_url=None):
            effective = (base_url or project.base_url or "https://dev.azure.com").rstrip("/")
            if effective == VSSPS_BASE:
                # VSSPS not used when env var is set; return a dummy transport
                return httpx.Client(
                    base_url=effective,
                    transport=httpx.MockTransport(lambda r: _json_resp({}, 404)),
                )
            # ADO call → simulate network failure
            def _raise(req):
                raise httpx.ConnectError("refused")
            return httpx.Client(base_url=effective, transport=httpx.MockTransport(_raise))

        monkeypatch.setattr(azure_mod, "_client", fake_client)
        result = AzureDevOpsProvider().discover_projects(token="tok", limit=50)
        assert result.projects == []
        assert result.reason == "network_error"


# ---------------------------------------------------------------------------
# Inheritance / interface test
# ---------------------------------------------------------------------------


def test_azuredevops_provider_implements_token_discovery():
    assert issubclass(AzureDevOpsProvider, TokenProjectDiscoveryProvider)
