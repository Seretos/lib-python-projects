from __future__ import annotations

import textwrap
from configparser import ConfigParser
from pathlib import Path

import pytest

from lib_python_projects import loader as cfg_mod
from lib_python_projects import (
    ConfigDocument,
    ConfigError,
    ProjectsLoadResult,
    Permissions,
    ProjectConfig,
    load_projects,
)
from lib_python_projects.autodiscover import _parse_remote_url
from lib_python_projects.loader import _resolve_config_path


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def _make_git_repo(path: Path) -> None:
    """Plant an empty `.git/` so `_find_git_repo_root` treats `path` as a
    git project root. The new project-boundary walker requires this —
    without `.git/`, the walker yields no candidates and the resolver
    falls straight through to `~/.seretos/`."""
    (path / ".git").mkdir(parents=True, exist_ok=True)


def _set_git_remote(repo_root: Path, url: str) -> None:
    """Write a minimal `.git/config` with an `origin` remote so
    `_autodiscover_from_git` returns a synthesized project."""
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    cp = ConfigParser()
    cp.add_section('remote "origin"')
    cp.set('remote "origin"', "url", url)
    with (repo_root / ".git" / "config").open("w", encoding="utf-8") as fh:
        cp.write(fh)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test runs against a fake home directory and a wiped env
    so the developer's real `~/.seretos/project-issues.yml` (if any)
    and locally-set `PROJECT_ISSUES_*` env vars never leak into the
    test outcome. `tmp_path` itself is marked as a git repo by default,
    so the project-boundary walk reaches the `.seretos/` fixtures the
    individual tests drop into it.
    """
    fake_home = tmp_path / "_fake_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(cfg_mod.Path, "home", lambda: fake_home)
    for var in (
        "PROJECT_ISSUES_CONFIG",
        "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD",
        "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME",
        "APPDATA",
        "USERPROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    _make_git_repo(tmp_path)
    return fake_home


def test_parse_remote_url_ssh():
    assert _parse_remote_url("git@github.com:acme/backend.git") == ("github.com", "acme/backend")


def test_parse_remote_url_https():
    assert _parse_remote_url("https://github.com/acme/backend") == ("github.com", "acme/backend")


def test_parse_remote_url_https_with_token():
    assert _parse_remote_url("https://x-access-token:abc@github.com/acme/backend.git") == ("github.com", "acme/backend")


def test_parse_remote_url_unknown():
    assert _parse_remote_url("ftp://example.com/foo") is None


# ---------- YAML loader: happy path -----------------------------------------


def test_load_yaml_returns_projects(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: acme
            provider: github
            path: acme/backend
            permissions:
              issues:
                create: true
                modify: false
    """)
    result = load_projects(cwd=tmp_path)
    assert isinstance(result, ProjectsLoadResult)
    assert result.state == "ok"
    assert len(result.projects) == 1
    p = result.projects[0]
    assert p.id == "acme"
    assert p.provider == "github"
    assert p.path == "acme/backend"
    # Backward-compat derived properties still work for internal code.
    assert p.owner == "acme"
    assert p.repo == "backend"
    assert p.display_path == "acme/backend"
    assert p.web_url == "https://github.com/acme/backend"
    assert p.permissions.issues.create is True
    assert p.permissions.issues.modify is False
    # pulls namespace defaults to all-false
    assert p.permissions.pulls.create is False
    assert p.permissions.pulls.modify is False
    assert p.permissions.pulls.merge is False


def test_load_yaml_yaml_extension_also_works(tmp_path: Path):
    """The loader accepts `.yaml` in addition to `.yml`."""
    cfg = tmp_path / ".seretos/project-issues.yaml"
    _write(cfg, """
        version: 1
        projects:
          - id: a
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "ok"
    assert result.projects[0].id == "a"


def test_load_yaml_omitted_version_defaults_to_one(tmp_path: Path):
    """`version` is optional and defaults to 1."""
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        projects:
          - id: nv
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "ok"


def test_load_yaml_rejects_unknown_top_level_key(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        version: 1
        oops_extra: 42
        projects:
          - id: x
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "oops_extra" in (result.error or "")


def test_load_yaml_rejects_unknown_project_key(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: x
            provider: github
            path: a/b
            owner: legacy    # legacy v0 field — must be rejected
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "owner" in (result.error or "")


def test_load_yaml_rejects_future_schema_version(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        version: 99
        projects:
          - id: x
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "version" in (result.error or "").lower()


def test_load_yaml_rejects_github_path_without_slash(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: bad
            provider: github
            path: justname
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "owner/repo" in (result.error or "")


def test_load_yaml_rejects_reserved_id(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: _auto
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "_auto" in (result.error or "")


def test_load_no_config_returns_no_config(tmp_path: Path):
    """No config, no resolvable git remote → empty no_config result.

    The autouse fixture marks `tmp_path` as a git repo but doesn't
    populate `origin`, so `_autodiscover_from_git` returns None.
    """
    result = load_projects(cwd=tmp_path)
    assert result.state == "no_config"
    assert result.projects == []


def test_load_config_empty(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, "# empty file\n")
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_empty"
    assert result.projects == []


def test_load_yaml_gitlab_path_is_passthrough(tmp_path: Path):
    cfg = tmp_path / ".seretos/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: gl
            provider: gitlab
            path: group/sub/project
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "ok"
    p = result.projects[0]
    assert p.path == "group/sub/project"
    assert p.project_path == "group/sub/project"
    assert p.owner is None
    assert p.repo is None
    assert p.web_url == "https://gitlab.com/group/sub/project"


# ---------- Permissions model: strict-only (no flat migration) --------------


def test_permissions_nested_form_loads_correctly():
    """The nested form is the canonical (and only) shape."""
    perms = Permissions.model_validate({
        "issues": {"create": True, "modify": True},
        "pulls": {"create": True, "modify": False, "merge": False},
    })
    assert perms.issues.create is True
    assert perms.issues.modify is True
    assert perms.pulls.create is True
    assert perms.pulls.modify is False
    assert perms.pulls.merge is False


def test_permissions_legacy_flat_form_is_rejected():
    """Flat `{create, modify}` is no longer accepted in v1."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Permissions.model_validate({"create": True, "modify": True})


def test_permissions_empty_defaults_all_false():
    perms = Permissions.model_validate({})
    assert perms.issues.create is False
    assert perms.issues.modify is False
    assert perms.pulls.create is False
    assert perms.pulls.modify is False
    assert perms.pulls.merge is False


# ---------- ConfigDocument model directly -----------------------------------


def test_config_document_strict():
    """ConfigDocument forbids unknown top-level keys."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ConfigDocument.model_validate({"version": 1, "unknown": True})


# ---------- list_projects response shape (no schema-internal leakage) -------


def test_list_projects_response_keeps_path_key(tmp_path: Path):
    """Smoke-test: the externally-visible list_projects response shape
    (which is `path`, NOT `owner`/`repo`) must be unchanged.

    TODO(ports-adapters): re-enable nach API-Stabilisierung —
    `_project_to_dict` lives in agent-project-issues (tool layer).
    This lib only owns the model + loader; the response-shape concern
    is plugin-side.
    """
    pytest.skip("tool-layer response-shape test — belongs in agent-project-issues")


# ---------- Config-path resolver: precedence + project-boundary walk -------


class TestConfigPathResolver:
    """Cover the resolver's precedence order and the new
    project-boundary walk introduced for the `.seretos/` refactor.
    """

    def test_env_override_wins_over_everything(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """`PROJECT_ISSUES_CONFIG` is checked first; even when a
        CWD-local config exists, the override wins."""
        override = tmp_path / "alt" / "explicit.yml"
        override.parent.mkdir(parents=True)
        override.write_text("version: 1\nprojects: []\n")
        # Also create a CWD-near config that would otherwise win.
        cwd_cfg = tmp_path / ".seretos" / "project-issues.yml"
        cwd_cfg.parent.mkdir(parents=True)
        cwd_cfg.write_text("version: 1\nprojects: []\n")

        monkeypatch.setenv("PROJECT_ISSUES_CONFIG", str(override))
        winner, searched = _resolve_config_path(tmp_path)
        assert winner == override.resolve()
        # The override is the only entry inspected — the resolver
        # short-circuits after the explicit hit.
        assert searched == [override.resolve()]

    def test_env_override_missing_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Missing override path is a hard error, not a silent fall-through."""
        bogus = tmp_path / "does-not-exist.yml"
        monkeypatch.setenv("PROJECT_ISSUES_CONFIG", str(bogus))
        with pytest.raises(ConfigError, match="non-existent"):
            _resolve_config_path(tmp_path)

    def test_env_override_missing_propagates_to_load_projects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """`load_projects` translates the resolver's `ConfigError`
        into a `config_error` ProjectsLoadResult so MCP callers see the
        diagnostic instead of a stack trace."""
        bogus = tmp_path / "missing.yml"
        monkeypatch.setenv("PROJECT_ISSUES_CONFIG", str(bogus))
        result = load_projects(cwd=tmp_path)
        assert result.state == "config_error"
        assert "non-existent" in (result.error or "")
        assert any("missing.yml" in p for p in result.searched_paths)

    def test_project_boundary_wins_over_home(self, tmp_path: Path, _isolated_env: Path):
        """A `.seretos/` config in an enclosing git repo wins over the
        user-level `~/.seretos/` fallback."""
        cwd_cfg = tmp_path / ".seretos" / "project-issues.yml"
        cwd_cfg.parent.mkdir(parents=True)
        cwd_cfg.write_text("version: 1\nprojects: []\n")

        # Plant a home-level config that would otherwise be picked.
        home_cfg = _isolated_env / ".seretos" / "project-issues.yml"
        home_cfg.parent.mkdir(parents=True)
        home_cfg.write_text("version: 1\nprojects: []\n")

        winner, searched = _resolve_config_path(tmp_path)
        assert winner == cwd_cfg.resolve()
        # Home candidate was never inspected — the resolver short-circuited.
        assert str(home_cfg.resolve()) not in {str(p) for p in searched}

    def test_walk_jumps_out_of_inner_repo_to_outer_repo(self, tmp_path: Path):
        """Project-boundary walk: when CWD is inside a nested repo and
        the inner repo has no `.seretos/` config, the walk jumps OUT
        of the inner repo and checks the enclosing repo's `.seretos/`.

        Layout:
            tmp_path/        ← outer repo (autouse fixture made .git/)
              .seretos/project-issues.yml   ← this should win
              inner/
                .git/                         ← inner repo
                workdir/                       ← CWD lives here
        """
        outer_cfg = tmp_path / ".seretos" / "project-issues.yml"
        outer_cfg.parent.mkdir(parents=True)
        outer_cfg.write_text(
            "version: 1\nprojects:\n  - id: outer\n    provider: github\n    path: o/x\n"
        )
        inner = tmp_path / "inner"
        _make_git_repo(inner)
        workdir = inner / "workdir"
        workdir.mkdir()

        winner, _ = _resolve_config_path(workdir)
        assert winner == outer_cfg.resolve()

        result = load_projects(cwd=workdir)
        assert result.state == "ok"
        assert any(p.id == "outer" for p in result.projects)

    def test_inner_repo_config_wins_over_outer(self, tmp_path: Path):
        """Inner repo's `.seretos/` beats the outer repo's — first
        boundary visited, first match wins. Configs are NOT merged."""
        outer_cfg = tmp_path / ".seretos" / "project-issues.yml"
        outer_cfg.parent.mkdir(parents=True)
        outer_cfg.write_text(
            "version: 1\nprojects:\n  - id: outer\n    provider: github\n    path: o/x\n"
        )
        inner = tmp_path / "inner"
        _make_git_repo(inner)
        inner_cfg = inner / ".seretos" / "project-issues.yml"
        inner_cfg.parent.mkdir(parents=True)
        inner_cfg.write_text(
            "version: 1\nprojects:\n  - id: inner\n    provider: github\n    path: i/y\n"
        )

        result = load_projects(cwd=inner)
        assert result.state == "ok"
        # Strict whitelist: only the inner project, NOT outer.
        assert {p.id for p in result.projects if p.source == "config"} == {"inner"}

    def test_home_fallback_when_no_enclosing_repo(self, tmp_path: Path, _isolated_env: Path, monkeypatch: pytest.MonkeyPatch):
        """No enclosing repo → walker yields no candidates → resolver
        falls through to `~/.seretos/project-issues.yml`. Mocked so
        the developer's own filesystem (parent .git dirs, real
        `.seretos/` configs higher up) can't influence the result.
        """
        # Patch the discovery helper inside lib_python_config — the
        # resolver calls walk_project_boundaries which in turn looks up
        # find_git_repo_root through lib_python_config.discovery.
        import lib_python_config.discovery as _disc
        monkeypatch.setattr(_disc, "find_git_repo_root", lambda _start: None)
        # Also patch the loader's module-level alias for callers that
        # reach it that way.
        monkeypatch.setattr(cfg_mod, "_find_git_repo_root", lambda _start: None)

        home_cfg = _isolated_env / ".seretos" / "project-issues.yml"
        home_cfg.parent.mkdir(parents=True)
        home_cfg.write_text(
            "version: 1\nprojects:\n  - id: home\n    provider: github\n    path: h/o\n"
        )

        winner, _ = _resolve_config_path(tmp_path)
        assert winner == home_cfg.resolve()

    def test_no_config_searched_paths_for_diagnostics(self, tmp_path: Path, _isolated_env: Path):
        """When nothing is found, `searched_paths` reports the
        candidate set so #15's `runtime.*` block can surface it."""
        result = load_projects(cwd=tmp_path)
        assert result.state == "no_config"
        # The project-boundary walk inspected `tmp_path/.seretos/...`
        # (both .yml and .yaml) and then the home candidate.
        normed = [p.replace("\\", "/") for p in result.searched_paths]
        assert any(".seretos/project-issues.yml" in p for p in normed)
        assert any("_fake_home/.seretos/project-issues.yml" in p for p in normed)

    def test_legacy_dotclaude_is_no_longer_searched(self, tmp_path: Path):
        """Hard cut: a `.claude/project-issues.yml` left over from the
        old layout must not be picked up."""
        legacy = tmp_path / ".claude" / "project-issues.yml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            "version: 1\nprojects:\n  - id: legacy\n    provider: github\n    path: l/g\n"
        )
        result = load_projects(cwd=tmp_path)
        # Legacy is invisible. With no `.seretos/` config and no remote
        # set, this collapses to no_config.
        assert result.state == "no_config"
        assert result.projects == []


# ---------- Additive auto-discovery for the CWD repo (ticket #33) -----------


class TestAdditiveAutoDiscovery:
    """The CWD git repo is always included in `list_projects` — either
    from the config (with explicit permissions) or via auto-discovery
    (read-only, token-probe-derived). Configs are NOT merged for
    foreign repos; the auto-entry only ever represents the CWD repo.
    """

    def test_auto_appended_when_cwd_repo_not_in_config(self, tmp_path: Path):
        """Reproduce ticket #33: config lists 5 projects, none of them
        is the CWD repo. Today's bug = auto-discovery suppressed. New
        behaviour = auto-entry appended on top of the 5 config entries."""
        _set_git_remote(tmp_path, "git@github.com:Seretos/agent-plugin-dev.git")
        cfg = tmp_path / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: other-1
                provider: github
                path: acme/backend
              - id: other-2
                provider: github
                path: acme/frontend
        """)
        result = load_projects(cwd=tmp_path)
        assert result.state == "ok"
        # Two config entries + one auto entry for the CWD repo.
        assert len(result.projects) == 3
        auto = [p for p in result.projects if p.source == "git-remote"]
        assert len(auto) == 1
        assert auto[0].id == "_auto"
        assert auto[0].path == "Seretos/agent-plugin-dev"
        assert auto[0].token_env == "GITHUB_TOKEN"

    def test_auto_suppressed_when_cwd_repo_is_in_config(self, tmp_path: Path):
        """Dedup by (provider, path): if the CWD repo is explicitly
        declared in the config, the config entry wins and no auto
        entry is added — the explicit permissions stay authoritative."""
        _set_git_remote(tmp_path, "git@github.com:acme/backend.git")
        cfg = tmp_path / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: acme
                provider: github
                path: acme/backend
                permissions:
                  issues:
                    create: true
                    modify: true
        """)
        result = load_projects(cwd=tmp_path)
        assert result.state == "ok"
        assert len(result.projects) == 1
        assert result.projects[0].id == "acme"
        assert result.projects[0].source == "config"
        # Auto entry must NOT shadow or duplicate the config.
        assert not any(p.source == "git-remote" for p in result.projects)

    def test_dedup_is_case_insensitive(self, tmp_path: Path):
        """GitHub paths are case-insensitive when comparing config vs.
        auto-discovery; differing capitalisation must still dedup."""
        _set_git_remote(tmp_path, "git@github.com:Seretos/Agent-Plugin-Dev.git")
        cfg = tmp_path / ".seretos" / "project-issues.yml"
        _write(cfg, """
            version: 1
            projects:
              - id: agent-plugin-dev
                provider: github
                path: seretos/agent-plugin-dev
        """)
        result = load_projects(cwd=tmp_path)
        assert result.state == "ok"
        assert len(result.projects) == 1
        assert result.projects[0].source == "config"

    def test_auto_only_when_no_config(self, tmp_path: Path):
        """No config exists but CWD repo has a recognised remote →
        the auto entry is the only project (today's pre-#33 behaviour
        is preserved for the no-config case)."""
        _set_git_remote(tmp_path, "git@github.com:Seretos/agent-plugin-dev.git")
        result = load_projects(cwd=tmp_path)
        assert result.state == "ok"
        assert len(result.projects) == 1
        assert result.projects[0].source == "git-remote"
        assert result.projects[0].path == "Seretos/agent-plugin-dev"

    def test_no_auto_when_remote_is_neither_github_nor_gitlab(self, tmp_path: Path):
        """Unrecognised host → no auto entry — strict whitelist
        still applies. Config is empty here, so state collapses to
        no_config rather than ok."""
        _set_git_remote(tmp_path, "https://example.com/foo/bar.git")
        result = load_projects(cwd=tmp_path)
        assert result.state == "no_config"
        assert result.projects == []
