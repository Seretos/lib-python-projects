"""Project list loader — the entry point for downstream plugins.

`load_projects` resolves a config file using the generic
`lib_python_config` machinery (with caller-supplied filenames /
env-var names / config dir), validates the YAML against `ConfigDocument`,
converts each entry to a `ProjectConfig`, and additively appends the
CWD-repo auto-discovered entry when applicable.

Diagnostic provenance (config_file / git_config / searched_paths / state)
flows through `ProjectsLoadResult` unchanged.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from lib_python_config import (
    ConfigError,
    find_git_repo_root,
    load_env_file,
    load_yaml,
    resolve_config_path,
    resolve_search_root,
    walk_up,
)

from lib_python_projects.autodiscover import _autodiscover_from_git
from lib_python_projects.models import (
    ConfigDocument,
    ProjectConfig,
    ProjectsLoadResult,
)

log = logging.getLogger("lib_python_projects.loader")

# Module-level alias kept so tests / older callers can monkey-patch the
# helper at `lib_python_projects.loader._find_git_repo_root`. The loader
# itself uses the module-attribute lookup so the patch is honored.
_find_git_repo_root = find_git_repo_root


# Defaults baked into the resolver shim below. These match the
# `agent-project-issues` plugin so the migrated test_config.py keeps
# working without re-parameterising every call site.
_CONFIG_DIRNAME = ".seretos"
_CONFIG_FILENAME = "project-issues.yml"
_CONFIG_FILENAME_ALT = "project-issues.yaml"


def _resolve_config_path(cwd: Path) -> tuple[Path | None, list[Path]]:
    """Backwards-compat shim over `lib_python_config.resolve_config_path`.

    Pre-binds the `agent-project-issues` defaults so test fixtures keep
    calling `_resolve_config_path(tmp_path)` without threading config_dir
    / filename kwargs through every assertion. Production code goes
    through `load_projects` (which takes the same defaults but accepts
    overrides).
    """
    return resolve_config_path(
        cwd,
        config_dir=_CONFIG_DIRNAME,
        filenames=(_CONFIG_FILENAME, _CONFIG_FILENAME_ALT),
        override_env="PROJECT_ISSUES_CONFIG",
        plugin_root_env="PROJECT_ISSUES_PLUGIN_ROOT",
        home_default=True,
    )


def _load_yaml_projects(yaml_path: Path) -> list[ProjectConfig]:
    """Read + validate a single project-list YAML file.

    Returns a list of `ProjectConfig`. Side effect: loads any `env_file`
    declared in the document (or the conventional `.env` lookups) into
    the process environment.
    """
    data = load_yaml(yaml_path)

    try:
        doc = ConfigDocument.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{yaml_path}: schema error: {exc}") from exc

    if doc.version != 1:
        raise ConfigError(
            f"{yaml_path}: unsupported config schema version "
            f"{doc.version} (this server understands v1)"
        )

    # Locate the .env file. If `env_file` is explicitly set, respect it
    # (resolved relative to the config file). Otherwise check the project
    # root first (the conventional .env location) and then the config
    # directory as a fallback.
    if doc.env_file is not None:
        candidates = [(yaml_path.parent / doc.env_file).resolve()]
    else:
        candidates = [
            (yaml_path.parent.parent / ".env").resolve(),
            (yaml_path.parent / ".env").resolve(),
        ]
    for env_path in candidates:
        if env_path.exists():
            load_env_file(env_path)
            break

    projects: list[ProjectConfig] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(doc.projects):
        if not isinstance(item, dict):
            raise ConfigError(
                f"{yaml_path}: projects[{idx}]: must be a mapping"
            )
        try:
            project = ProjectConfig.model_validate({**item, "source": "config"})
        except ValidationError as exc:
            raise ConfigError(
                f"{yaml_path}: projects[{idx}]: {exc}"
            ) from exc
        if project.id == "_auto":
            raise ConfigError(
                f"{yaml_path}: project id '_auto' is reserved for "
                "auto-discovery"
            )
        if project.id in seen_ids:
            raise ConfigError(
                f"{yaml_path}: duplicate project id '{project.id}'"
            )
        seen_ids.add(project.id)
        projects.append(project)
    return projects


def load_projects(
    cwd: Path | None = None,
    *,
    config_filename: str = "project-issues.yml",
    config_filename_alt: str = "project-issues.yaml",
    config_dir: str = ".seretos",
    override_env: str = "PROJECT_ISSUES_CONFIG",
    plugin_root_env: str = "PROJECT_ISSUES_PLUGIN_ROOT",
    search_env_vars: tuple[str, ...] = ("PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR"),
) -> ProjectsLoadResult:
    """Resolve the project list for the configured working directory.

    Configs are **not merged**: the first-found
    `<config_dir>/<config_filename>` wins entirely. The CWD repo is the
    only exception — it's always auto-included via git-remote discovery
    if not already in the winning config. The strict whitelist still
    applies to every other repo.

    Defaults match the `agent-project-issues` plugin so the plugin's
    refactor is minimal-invasive. Pass overrides for other consumers.
    """
    cwd = resolve_search_root(cwd, env_vars=search_env_vars)
    git_path = walk_up(cwd, (".git/config",))

    try:
        config_path, searched = resolve_config_path(
            cwd,
            config_dir=config_dir,
            filenames=(config_filename, config_filename_alt),
            override_env=override_env,
            plugin_root_env=plugin_root_env,
            home_default=True,
        )
    except ConfigError as exc:
        # Explicit-override-to-missing-file: surface as `config_error`
        # rather than `no_config`, because the user gave the resolver a
        # concrete path and got nothing back.
        override_val = os.environ.get(override_env, "")
        return ProjectsLoadResult(
            projects=[],
            state="config_error",
            git_config=str(git_path) if git_path else None,
            search_root=str(cwd),
            error=str(exc),
            searched_paths=[str(Path(override_val).resolve())] if override_val else [],
        )

    searched_strs = [str(p) for p in searched]

    projects: list[ProjectConfig] = []
    if config_path:
        try:
            projects = _load_yaml_projects(config_path)
        except (ConfigError, OSError) as exc:
            # Parse failure: don't try to silently substitute auto-discovery —
            # the user told us where the config lives, and it's broken.
            return ProjectsLoadResult(
                projects=[],
                state="config_error",
                config_file=str(config_path),
                git_config=str(git_path) if git_path else None,
                search_root=str(cwd),
                error=f"failed to load {config_path}: {exc}",
                searched_paths=searched_strs,
            )
        log.info("loaded %d project(s) from %s", len(projects), config_path)

    # Additive CWD-repo auto-discovery. Dedup by (provider, path).
    # Whitelist semantics for fremde repos stay intact — `_autodiscover_from_git`
    # only ever returns the CWD repo, never anything else.
    auto = _autodiscover_from_git(cwd)
    if auto:
        # New in lib v0.1.0: pin the local checkout path on the auto entry.
        repo_root = find_git_repo_root(cwd)
        if repo_root is not None:
            auto = auto.model_copy(update={"local_path": str(repo_root)})
        key = (auto.provider, (auto.path or "").lower())
        existing = {(p.provider, (p.path or "").lower()) for p in projects}
        if key not in existing:
            log.info(
                "auto-discovered cwd repo (%s %s) — appending to project list",
                auto.provider, auto.display_path,
            )
            projects.append(auto)

    if projects:
        state: Literal["ok", "config_empty", "no_config", "config_error"] = "ok"
    elif config_path:
        state = "config_empty"
    else:
        log.info("no config and no usable git remote in %s", cwd)
        state = "no_config"

    return ProjectsLoadResult(
        projects=projects,
        state=state,
        config_file=str(config_path) if config_path else None,
        git_config=str(git_path) if git_path else None,
        search_root=str(cwd),
        searched_paths=searched_strs,
    )


def resolve_token(project: ProjectConfig) -> str | None:
    """Return the token value referenced by `project.token_env`, if any."""
    if not project.token_env:
        return None
    return os.environ.get(project.token_env)
