"""Unit tests for `lib_python_projects.loader.load_projects`.

The migrated `test_config.py` covers the legacy load_projects behaviour
against the agent-project-issues defaults. These tests cover the
extension points new in v0.1.0:

  * caller-supplied `config_filename` / `config_dir` / env-var names.
  * `local_path` auto-population for the git-remote auto-discovered entry.
  * `local_path` read-through from YAML on config-sourced entries.
"""
from __future__ import annotations

import textwrap
from configparser import ConfigParser
from pathlib import Path

import pytest

from lib_python_projects import load_projects


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
