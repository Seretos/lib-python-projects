"""lib-python-projects — projects-model, provider abstraction, loader.

Public re-exports. Providers are accessed through the
`lib_python_projects.providers` sub-package — not re-exported at the top
level because importing them would pull in `httpx` (and provider-specific
deps) just to read a config.
"""
from __future__ import annotations

# Re-export `ConfigError` from the foundation lib so downstream callers
# can `except lib_python_projects.ConfigError` without dragging in the
# `lib_python_config` import. The loader raises and propagates the same
# class — there's no domain-specific subclass.
from lib_python_config import ConfigError

from lib_python_projects.finder import find_projects
from lib_python_projects.loader import load_projects, resolve_token
from lib_python_projects.models import (
    AutoLabels,
    AzureBoardsBinding,
    Board,
    BoardAutoLabels,
    BoardBinding,
    ConfigDocument,
    FindResult,
    GithubProjectsV2Binding,
    InvalidProjectEntry,
    IssuesPermissions,
    Permissions,
    ProjectConfig,
    ProjectMatch,
    ProjectsLoadResult,
    Provider,
    PullsPermissions,
    Source,
)

__version__ = "0.1.0"

__all__ = [
    "ProjectConfig",
    "AutoLabels",
    "Permissions",
    "IssuesPermissions",
    "PullsPermissions",
    "Board",
    "BoardAutoLabels",
    "BoardBinding",
    "GithubProjectsV2Binding",
    "AzureBoardsBinding",
    "ConfigDocument",
    "ProjectsLoadResult",
    "InvalidProjectEntry",
    "Provider",
    "Source",
    "ConfigError",
    "load_projects",
    "resolve_token",
    "find_projects",
    "FindResult",
    "ProjectMatch",
    "__version__",
]
