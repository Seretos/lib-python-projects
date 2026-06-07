"""Tests for ticket #80 — token discovery foundation types.

Covers:
- Source Literal widening in ProjectConfig ("token-discovery").
- Importability of the three new types from lib_python_projects.providers.base.
- DiscoveredProject dataclass construction and round-trip.
- ProjectDiscoveryResult defaults, failure-contract taxonomy, and truncation.
- TokenProjectDiscoveryProvider interface (NotImplementedError, keyword-only
  limit, subclass contract).
"""
from __future__ import annotations

import dataclasses

import pytest


# ---------- helpers -----------------------------------------------------------


def _make_project_config(**kwargs):
    from lib_python_projects import ProjectConfig

    return ProjectConfig(id="x", provider="github", path="acme/backend", **kwargs)


def _make_capabilities(**kwargs):
    from lib_python_projects.providers.base import TokenCapabilities

    return TokenCapabilities(**kwargs)


# ---------- Source Literal widening ------------------------------------------


class TestSourceLiteralWidening:
    """ProjectConfig.source now accepts 'token-discovery' in addition to the
    two pre-existing values."""

    def test_token_discovery_source_is_valid(self):
        p = _make_project_config(source="token-discovery")
        assert p.source == "token-discovery"

    def test_config_source_still_valid(self):
        p = _make_project_config(source="config")
        assert p.source == "config"

    def test_git_remote_source_still_valid(self):
        p = _make_project_config(source="git-remote")
        assert p.source == "git-remote"

    def test_unknown_source_raises_validation_error(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_project_config(source="bogus-source")  # type: ignore[arg-type]


# ---------- importability of new types ----------------------------------------


class TestImportability:
    """All three new types must be importable from
    lib_python_projects.providers.base — and only from there (no top-level
    re-export)."""

    def test_discovered_project_importable(self):
        from lib_python_projects.providers.base import DiscoveredProject  # noqa: F401

    def test_project_discovery_result_importable(self):
        from lib_python_projects.providers.base import ProjectDiscoveryResult  # noqa: F401

    def test_token_project_discovery_provider_importable(self):
        from lib_python_projects.providers.base import TokenProjectDiscoveryProvider  # noqa: F401


# ---------- DiscoveredProject dataclass ---------------------------------------


class TestDiscoveredProject:
    def test_required_only_construction_yields_defaults(self):
        from lib_python_projects.providers.base import DiscoveredProject

        caps = _make_capabilities()
        dp = DiscoveredProject(
            provider="github",
            path="owner/repo",
            permissions=caps,
        )
        assert dp.provider == "github"
        assert dp.path == "owner/repo"
        assert dp.permissions is caps
        assert dp.description == ""
        assert dp.default_work_item_type is None
        assert dp.base_url is None

    def test_all_fields_round_trip(self):
        from lib_python_projects.providers.base import DiscoveredProject

        caps = _make_capabilities(issues_create=True, pulls_create=True)
        dp = DiscoveredProject(
            provider="gitlab",
            path="namespace/project",
            permissions=caps,
            description="My project",
            default_work_item_type="Issue",
            base_url="https://gitlab.example.com",
        )
        assert dp.provider == "gitlab"
        assert dp.path == "namespace/project"
        assert dp.permissions.issues_create is True
        assert dp.permissions.pulls_create is True
        assert dp.description == "My project"
        assert dp.default_work_item_type == "Issue"
        assert dp.base_url == "https://gitlab.example.com"

    def test_is_dataclass(self):
        from lib_python_projects.providers.base import DiscoveredProject

        assert dataclasses.is_dataclass(DiscoveredProject)

    def test_not_frozen(self):
        """DiscoveredProject must be mutable (mirrors TokenCapabilities pattern)."""
        from lib_python_projects.providers.base import DiscoveredProject

        caps = _make_capabilities()
        dp = DiscoveredProject(provider="github", path="a/b", permissions=caps)
        dp.description = "updated"  # must not raise FrozenInstanceError
        assert dp.description == "updated"

    def test_field_order(self):
        """Required fields come before defaulted ones (dataclass declaration order)."""
        from lib_python_projects.providers.base import DiscoveredProject

        field_names = [f.name for f in dataclasses.fields(DiscoveredProject)]
        assert field_names[:3] == ["provider", "path", "permissions"]
        assert set(field_names[3:]) == {"description", "default_work_item_type", "base_url"}


# ---------- ProjectDiscoveryResult dataclass ----------------------------------


class TestProjectDiscoveryResult:
    def test_bare_defaults(self):
        from lib_python_projects.providers.base import ProjectDiscoveryResult

        r = ProjectDiscoveryResult(projects=[])
        assert r.projects == []
        assert r.truncated is False
        assert r.reason is None

    def test_is_dataclass(self):
        from lib_python_projects.providers.base import ProjectDiscoveryResult

        assert dataclasses.is_dataclass(ProjectDiscoveryResult)

    # --- failure-contract taxonomy ---

    @pytest.mark.parametrize("reason", [
        "bad_credentials",
        "network_error",
        "http_403",
        "repo_invisible_to_token",
        "permissions_field_missing",
        "insufficient_scope",
    ])
    def test_failure_reason_taxonomy(self, reason: str):
        """On failure: projects=[], reason set, truncated=False."""
        from lib_python_projects.providers.base import ProjectDiscoveryResult

        r = ProjectDiscoveryResult(projects=[], reason=reason)
        assert r.projects == []
        assert r.reason == reason
        assert r.truncated is False

    def test_truncated_true_with_non_empty_projects_is_valid(self):
        """truncated=True is NOT a failure — projects may be non-empty."""
        from lib_python_projects.providers.base import (
            DiscoveredProject,
            ProjectDiscoveryResult,
        )

        caps = _make_capabilities()
        dp = DiscoveredProject(provider="github", path="a/b", permissions=caps)
        r = ProjectDiscoveryResult(projects=[dp], truncated=True)
        assert len(r.projects) == 1
        assert r.truncated is True
        assert r.reason is None

    def test_happy_path_reason_is_none(self):
        from lib_python_projects.providers.base import (
            DiscoveredProject,
            ProjectDiscoveryResult,
        )

        caps = _make_capabilities(issues_create=True)
        dp = DiscoveredProject(provider="azuredevops", path="org/proj/repo", permissions=caps)
        r = ProjectDiscoveryResult(projects=[dp])
        assert r.reason is None


# ---------- TokenProjectDiscoveryProvider interface ---------------------------


class TestTokenProjectDiscoveryProvider:
    def test_base_raises_not_implemented(self):
        from lib_python_projects.providers.base import TokenProjectDiscoveryProvider

        provider = TokenProjectDiscoveryProvider()
        with pytest.raises(NotImplementedError):
            provider.discover_projects(token="x", limit=50)

    def test_limit_is_keyword_only(self):
        """discover_projects(token, limit) as positional must raise TypeError."""
        from lib_python_projects.providers.base import TokenProjectDiscoveryProvider

        provider = TokenProjectDiscoveryProvider()
        with pytest.raises(TypeError):
            provider.discover_projects("tok", 50)  # type: ignore[misc]

    def test_subclass_returning_fixed_result_works(self):
        """A minimal inline subclass that returns a real result is accepted."""
        from lib_python_projects.providers.base import (
            ProjectDiscoveryResult,
            TokenProjectDiscoveryProvider,
        )

        class _Stub(TokenProjectDiscoveryProvider):
            def discover_projects(
                self, token: str, *, limit: int
            ) -> ProjectDiscoveryResult:
                return ProjectDiscoveryResult(projects=[])

        stub = _Stub()
        result = stub.discover_projects(token="secret", limit=100)
        assert isinstance(result, ProjectDiscoveryResult)
        assert result.projects == []
        assert result.truncated is False
        assert result.reason is None

    def test_subclass_still_enforces_keyword_only_limit(self):
        """Even after subclassing the keyword-only contract must hold."""
        from lib_python_projects.providers.base import (
            ProjectDiscoveryResult,
            TokenProjectDiscoveryProvider,
        )

        class _Stub(TokenProjectDiscoveryProvider):
            def discover_projects(
                self, token: str, *, limit: int
            ) -> ProjectDiscoveryResult:
                return ProjectDiscoveryResult(projects=[])

        stub = _Stub()
        with pytest.raises(TypeError):
            stub.discover_projects("tok", 50)  # type: ignore[misc]
