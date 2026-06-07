"""Unit tests for `lib_python_projects.loader.load_projects`.

The migrated `test_config.py` covers the legacy load_projects behaviour
against the agent-project-issues defaults. These tests cover the
extension points new in v0.1.0:

  * caller-supplied `config_filename` / `config_dir` / env-var names.
  * `local_path` auto-population for the git-remote auto-discovered entry.
  * `local_path` read-through from YAML on config-sourced entries.
  * token-driven project discovery fallback (no config file present).
"""
from __future__ import annotations

import textwrap
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import lib_python_projects.loader as _loader_mod
from lib_python_projects import load_projects
from lib_python_projects.providers.base import (
    DiscoveredProject,
    ProjectDiscoveryResult,
    TokenCapabilities,
    TokenProjectDiscoveryProvider,
)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def _make_git_repo(path: Path, remote_url: str | None = None) -> None:
    (path / ".git").mkdir(parents=True, exist_ok=True)
    if remote_url is not None:
        cp = ConfigParser()
        cp.add_section('remote "origin"')
        cp.set('remote "origin"', "url", remote_url)
        with (path / ".git" / "config").open("w", encoding="utf-8") as fh:
            cp.write(fh)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "_fake_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    for var in (
        "PROJECT_ISSUES_CONFIG",
        "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD",
        "CLAUDE_PROJECT_DIR",
        "RELEASE_PLUGIN_CONFIG",
        "RELEASE_PLUGIN_ROOT",
        "RELEASE_PLUGIN_CWD",
        # Token vars — strip so discovery tests start from a clean slate.
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "AZURE_DEVOPS_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return fake_home


class TestCustomConfigFilename:
    """Other plugins reuse the loader with their own filenames."""

    def test_custom_filename_is_resolved(self, tmp_path: Path) -> None:
        _make_git_repo(tmp_path)
        cfg = tmp_path / ".seretos" / "release.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: rel
                provider: github
                path: acme/release
        """)
        result = load_projects(
            cwd=tmp_path,
            config_filename="release.yml",
            config_filename_alt="release.yaml",
        )
        assert result.state == "ok"
        assert any(p.id == "rel" for p in result.projects)
        assert result.config_file == str(cfg)

    def test_custom_config_dir(self, tmp_path: Path) -> None:
        _make_git_repo(tmp_path)
        cfg = tmp_path / ".myplugin" / "config.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: mp
                provider: github
                path: a/b
        """)
        result = load_projects(
            cwd=tmp_path,
            config_dir=".myplugin",
            config_filename="config.yml",
            config_filename_alt="config.yaml",
        )
        assert result.state == "ok"
        assert any(p.id == "mp" for p in result.projects)

    def test_custom_override_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "explicit.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: explicit
                provider: github
                path: a/b
        """)
        monkeypatch.setenv("RELEASE_PLUGIN_CONFIG", str(cfg))
        result = load_projects(
            cwd=tmp_path,
            override_env="RELEASE_PLUGIN_CONFIG",
            plugin_root_env="RELEASE_PLUGIN_ROOT",
        )
        assert result.state == "ok"
        assert any(p.id == "explicit" for p in result.projects)

    def test_custom_search_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "work"
        _make_git_repo(repo)
        cfg = repo / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: r
                provider: github
                path: a/b
        """)
        monkeypatch.setenv("RELEASE_PLUGIN_CWD", str(repo))
        # cwd not passed → loader must pick up RELEASE_PLUGIN_CWD.
        result = load_projects(
            search_env_vars=("RELEASE_PLUGIN_CWD",),
        )
        assert result.state == "ok"
        assert str(repo) in result.search_root


class TestLocalPathAutoDiscovery:
    """`local_path` gets stamped on the auto-discovered CWD-repo entry."""

    def test_git_remote_entry_has_local_path(self, tmp_path: Path) -> None:
        _make_git_repo(tmp_path, remote_url="git@github.com:acme/backend.git")
        result = load_projects(cwd=tmp_path)
        assert result.state == "ok"
        autos = [p for p in result.projects if p.source == "git-remote"]
        assert len(autos) == 1
        # local_path matches the git-repo root we just created.
        assert autos[0].local_path == str(tmp_path.resolve())

    def test_config_sourced_project_local_path_default_none(
        self, tmp_path: Path
    ) -> None:
        _make_git_repo(tmp_path)
        cfg = tmp_path / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: a
                provider: github
                path: acme/backend
        """)
        result = load_projects(cwd=tmp_path)
        assert result.state == "ok"
        configured = [p for p in result.projects if p.source == "config"]
        assert len(configured) == 1
        assert configured[0].local_path is None

    def test_config_local_path_round_trips_from_yaml(
        self, tmp_path: Path
    ) -> None:
        _make_git_repo(tmp_path)
        cfg = tmp_path / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: a
                provider: github
                path: acme/backend
                local_path: /repos/backend
        """)
        result = load_projects(cwd=tmp_path)
        assert result.state == "ok"
        configured = [p for p in result.projects if p.source == "config"]
        assert configured[0].local_path == "/repos/backend"

    def test_local_path_unset_when_no_git_repo(self, tmp_path: Path) -> None:
        # No .git/ → no auto-discovered entry, no local_path to stamp.
        cfg = tmp_path / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: a
                provider: github
                path: acme/backend
        """)
        # Patch find_git_repo_root so the resolver can still locate
        # the config via the home-default chain — easier than wiring
        # plugin_root.
        import lib_python_config.discovery as _disc
        original = _disc.find_git_repo_root

        # Make boundary walker yield no candidates; instead use
        # plugin_root override to point at the config dir.
        import os
        os.environ["PROJECT_ISSUES_PLUGIN_ROOT"] = str(tmp_path / ".seretos")
        try:
            result = load_projects(cwd=tmp_path)
        finally:
            os.environ.pop("PROJECT_ISSUES_PLUGIN_ROOT", None)

        # Should resolve the config and find one project — no auto entry
        # because no git repo at cwd.
        # (find_git_repo_root may still find tmp_path if a parent has
        # .git — guard by just asserting the configured project lacks
        # local_path.)
        assert result.state == "ok"
        configured = [p for p in result.projects if p.source == "config"]
        assert configured[0].local_path is None
        _ = original  # touch to silence ruff


class TestTokenDiscoveryFallback:
    """Token-driven project discovery runs when no config file is found."""

    # ------------------------------------------------------------------
    # Helpers / stub factories
    # ------------------------------------------------------------------

    @staticmethod
    def _make_stub_provider(
        projects: list[DiscoveredProject],
        *,
        truncated: bool = False,
        reason: str | None = None,
        raise_exc: Exception | None = None,
    ) -> type:
        """Return a fresh provider class (subclass of TokenProjectDiscoveryProvider)."""
        _projects = projects
        _truncated = truncated
        _reason = reason
        _raise_exc = raise_exc
        calls: list[dict] = []

        class _StubProvider(TokenProjectDiscoveryProvider):
            call_log = calls

            def discover_projects(
                self, token: str, *, limit: int
            ) -> ProjectDiscoveryResult:
                calls.append({"token": token, "limit": limit})
                if _raise_exc is not None:
                    raise _raise_exc
                return ProjectDiscoveryResult(
                    projects=_projects,
                    truncated=_truncated,
                    reason=_reason,
                )

        return _StubProvider

    @staticmethod
    def _full_caps() -> TokenCapabilities:
        return TokenCapabilities(
            issues_create=True,
            issues_modify=True,
            pulls_create=True,
            pulls_modify=True,
            pulls_merge=True,
        )

    @staticmethod
    def _patch_registry(
        monkeypatch: pytest.MonkeyPatch,
        entries: list[tuple[str, str, type]],
    ) -> None:
        monkeypatch.setattr(_loader_mod, "_TOKEN_PROVIDERS", entries)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_no_config_token_present_returns_discovered_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GITHUB_TOKEN set, stub returns one project — state ok, project mapped."""
        dp = DiscoveredProject(
            provider="github",
            path="acme/backend",
            permissions=self._full_caps(),
            description="Backend repo",
        )
        stub_cls = self._make_stub_provider([dp])
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.state == "ok"
        assert len(result.projects) == 1
        p = result.projects[0]
        assert p.id == "github:acme/backend"
        assert p.source == "token-discovery"
        assert p.token_env == "GITHUB_TOKEN"
        assert p.permissions.issues.create is True
        assert p.permissions.pulls.merge is True
        assert result.discovery_truncated is False

    def test_config_present_discovery_does_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a valid config exists, discovery is never called."""
        stub_cls = self._make_stub_provider([])
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        # A git repo is needed so resolve_config_path can anchor the walk.
        _make_git_repo(tmp_path)
        cfg = tmp_path / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: cfg-proj
                provider: github
                path: acme/configured
        """)

        result = load_projects(cwd=tmp_path)

        assert result.state == "ok"
        ids = [p.id for p in result.projects]
        assert "cfg-proj" in ids
        assert not any(p.source == "token-discovery" for p in result.projects)
        assert stub_cls.call_log == []  # type: ignore[attr-defined]

    def test_dedup_git_remote_and_token_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Git-remote entry and same-path token-discovery entry are deduped."""
        _make_git_repo(tmp_path, remote_url="git@github.com:acme/backend.git")
        dp_same = DiscoveredProject(
            provider="github",
            path="acme/backend",
            permissions=self._full_caps(),
        )
        dp_other = DiscoveredProject(
            provider="github",
            path="acme/other",
            permissions=self._full_caps(),
        )
        stub_cls = self._make_stub_provider([dp_same, dp_other])
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.state == "ok"
        assert len(result.projects) == 2  # not 3
        paths = [(p.provider, (p.path or "").lower()) for p in result.projects]
        assert paths.count(("github", "acme/backend")) == 1

    def test_truncation_flag_set_when_provider_truncates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """discovery_truncated is True when any provider signals truncation."""
        dp = DiscoveredProject(
            provider="github",
            path="acme/repo",
            permissions=self._full_caps(),
        )
        stub_cls = self._make_stub_provider([dp], truncated=True)
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.discovery_truncated is True
        assert result.state == "ok"

    def test_truncation_false_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """discovery_truncated is False when the provider does not truncate."""
        dp = DiscoveredProject(
            provider="github",
            path="acme/repo",
            permissions=self._full_caps(),
        )
        stub_cls = self._make_stub_provider([dp], truncated=False)
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.discovery_truncated is False

    def test_provider_failure_reason_set_skips_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider returning a reason string yields no projects, state no_config."""
        stub_cls = self._make_stub_provider([], reason="bad_credentials")
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.state == "no_config"
        assert result.projects == []

    def test_provider_exception_skips_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider raising an exception is caught; no crash, state no_config."""
        stub_cls = self._make_stub_provider([], raise_exc=RuntimeError("boom"))
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.state == "no_config"
        assert result.projects == []

    def test_multiple_providers_merged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Projects from two providers are merged into one list."""
        dp_gh = DiscoveredProject(
            provider="github",
            path="acme/gh-repo",
            permissions=self._full_caps(),
        )
        dp_gl = DiscoveredProject(
            provider="gitlab",
            path="acme/gl-repo",
            permissions=self._full_caps(),
        )
        stub_gh = self._make_stub_provider([dp_gh])
        stub_gl = self._make_stub_provider([dp_gl])
        self._patch_registry(
            monkeypatch,
            [
                ("GITHUB_TOKEN", "github", stub_gh),
                ("GITLAB_TOKEN", "gitlab", stub_gl),
            ],
        )
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat_fake")

        result = load_projects(cwd=tmp_path)

        assert result.state == "ok"
        assert len(result.projects) == 2
        providers = {p.provider for p in result.projects}
        assert providers == {"github", "gitlab"}

    def test_malformed_discovered_path_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DiscoveredProject with invalid path for provider is silently skipped."""
        # azuredevops requires 3 segments; "only/two" has only 2.
        dp_bad = DiscoveredProject(
            provider="azuredevops",
            path="only/two",
            permissions=self._full_caps(),
        )
        stub_cls = self._make_stub_provider([dp_bad])
        self._patch_registry(
            monkeypatch, [("AZURE_DEVOPS_TOKEN", "azuredevops", stub_cls)]
        )
        monkeypatch.setenv("AZURE_DEVOPS_TOKEN", "ado_fake")

        result = load_projects(cwd=tmp_path)

        # Bad entry silently dropped; no crash.
        assert result.projects == []
        assert result.state == "no_config"

    def test_token_set_but_no_projects_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider returning empty projects with no reason → state no_config."""
        stub_cls = self._make_stub_provider([])
        self._patch_registry(monkeypatch, [("GITHUB_TOKEN", "github", stub_cls)])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.state == "no_config"
        assert result.projects == []

    def test_non_discovery_provider_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider not implementing TokenProjectDiscoveryProvider is silently skipped."""

        class _NonDiscoveryProvider:
            """No TokenProjectDiscoveryProvider in MRO."""

        self._patch_registry(
            monkeypatch, [("GITHUB_TOKEN", "github", _NonDiscoveryProvider)]
        )
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        result = load_projects(cwd=tmp_path)

        assert result.state == "no_config"
        assert result.projects == []
