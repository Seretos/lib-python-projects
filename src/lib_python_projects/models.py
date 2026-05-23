"""Domain models for the project list.

Extracted from `agent-project-issues/src/project_issues_plugin/config.py`.
The shapes here are the agent-facing surface: a `ProjectConfig` is what
every provider call routes through, what the MCP `list_projects` tool
returns, and what permissions checks consult.

Schema policy (carried over from the plugin):
- `extra="forbid"` on every model: typos in YAML become loud errors.
- The legacy split into `owner` / `repo` / `project_path` is gone from the
  YAML schema; provider code keeps reading those names through the
  backwards-compat `@property` accessors below.
- New in lib v0.1.0: `local_path` records the on-disk checkout, when known.
"""
from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from lib_python_config import LoadResult

Provider = Literal["github", "gitlab", "azuredevops"]
Source = Literal["config", "git-remote"]


class IssuesPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    create: bool = False
    modify: bool = False


class PullsPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    create: bool = False
    modify: bool = False
    merge: bool = False


class Permissions(BaseModel):
    """Nested permissions namespace.

    The legacy flat form (`{create, modify, pr_create, pr_modify}`)
    was removed in v1 of the YAML schema — see plugin ticket #8.
    """

    model_config = ConfigDict(extra="forbid")
    issues: IssuesPermissions = Field(default_factory=IssuesPermissions)
    pulls: PullsPermissions = Field(default_factory=PullsPermissions)


class ProjectConfig(BaseModel):
    """A single project entry.

    `path` is the provider-native repo identifier:
      - GitHub: `"owner/repo"` (e.g. `"Seretos/agent-project-issues"`)
      - GitLab: full namespace path (e.g. `"group/sub/project"`)
      - Azure DevOps: `"organization/project/repository"` — work items
        scope to `organization/project`, PRs to the full three-part path.

    The legacy split into `owner`/`repo`/`project_path` is gone from
    the YAML schema; for backward compatibility the internal code
    keeps accessing `project.owner` / `project.repo` / `project.project_path`
    via derived properties so the GitHub provider doesn't need a
    rewrite.

    `local_path` (new in lib v0.1.0) records the local filesystem checkout
    of this project, when known. It is auto-populated by the loader for
    `source="git-remote"` projects (from the discovered git-repo root) and
    readable from YAML for `source="config"` projects.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    provider: Provider
    path: str | None = None
    base_url: str | None = None
    token_env: str | None = Field(default=None, exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_available(self) -> bool:
        return bool(self.token_env and os.environ.get(self.token_env))

    permissions: Permissions = Field(default_factory=Permissions)
    source: Source = "config"
    # Azure DevOps only. When unset, the provider discovers a sensible
    # default once per project (Issue → Bug → User Story → Product
    # Backlog Item → Requirement). Ignored by github/gitlab.
    default_work_item_type: str | None = None
    # On-disk checkout path, when known. Optional for both config- and
    # git-remote-sourced projects; populated by the loader for the
    # auto-discovered CWD repo.
    local_path: str | None = None

    @model_validator(mode="after")
    def _check_provider_fields(self) -> "ProjectConfig":
        if not self.path:
            raise ValueError(
                f"project '{self.id}' is missing required field 'path' "
                f"(provider-native repo path, e.g. 'owner/repo' for github)"
            )
        if self.provider == "github":
            if "/" not in self.path or self.path.count("/") < 1:
                raise ValueError(
                    f"project '{self.id}': github 'path' must be "
                    f"'owner/repo', got {self.path!r}"
                )
        if self.provider == "azuredevops":
            if self.path.count("/") != 2:
                raise ValueError(
                    f"project '{self.id}': azuredevops 'path' must be "
                    f"'organization/project/repository', got {self.path!r}"
                )
            if any(not seg.strip() for seg in self.path.split("/")):
                raise ValueError(
                    f"project '{self.id}': azuredevops 'path' has an "
                    f"empty segment in {self.path!r}"
                )
        return self

    # --- Backward-compat derived properties ----------------------------------

    @property
    def owner(self) -> str | None:
        """GitHub owner derived from `path` (`"owner/repo"`)."""
        if self.provider != "github" or not self.path or "/" not in self.path:
            return None
        return self.path.split("/", 1)[0]

    @property
    def repo(self) -> str | None:
        """GitHub repo derived from `path`."""
        if self.provider != "github" or not self.path or "/" not in self.path:
            return None
        return self.path.split("/", 1)[1]

    @property
    def project_path(self) -> str | None:
        """GitLab project path — same as `path` for the gitlab provider."""
        return self.path if self.provider == "gitlab" else None

    # --- Azure DevOps derived properties -------------------------------------

    @property
    def organization(self) -> str | None:
        """Azure DevOps organization (first segment of `path`)."""
        if self.provider != "azuredevops" or not self.path:
            return None
        parts = self.path.split("/")
        return parts[0] if len(parts) == 3 else None

    @property
    def ado_project(self) -> str | None:
        """Azure DevOps project name (middle segment of `path`)."""
        if self.provider != "azuredevops" or not self.path:
            return None
        parts = self.path.split("/")
        return parts[1] if len(parts) == 3 else None

    @property
    def repository(self) -> str | None:
        """Azure DevOps repository name (last segment of `path`)."""
        if self.provider != "azuredevops" or not self.path:
            return None
        parts = self.path.split("/")
        return parts[2] if len(parts) == 3 else None

    @property
    def display_path(self) -> str:
        return self.path or ""

    @property
    def web_url(self) -> str | None:
        if self.provider == "github":
            return f"https://github.com/{self.path}"
        if self.provider == "gitlab":
            base = (self.base_url or "https://gitlab.com").rstrip("/")
            return f"{base}/{self.path}"
        if self.provider == "azuredevops":
            org, proj, repo = self.organization, self.ado_project, self.repository
            if org and proj and repo:
                base = (self.base_url or "https://dev.azure.com").rstrip("/")
                return f"{base}/{org}/{proj}/_git/{repo}"
        return None


class ConfigDocument(BaseModel):
    """Top-level YAML document shape.

    `version` defaults to 1 when omitted — this preserves the simplest
    happy-path for tiny configs while still letting a future v2 break
    cleanly. Strict on unknown top-level keys (`extra="forbid"`).
    """

    model_config = ConfigDict(extra="forbid")
    version: int = 1
    env_file: str | None = None
    projects: list[dict[str, Any]] = Field(default_factory=list)


class ProjectsLoadResult(LoadResult):
    """`LoadResult` extended with the resolved project list.

    Subclasses the generic `LoadResult` from `lib_python_config` so the
    diagnostic fields (`state`, `config_file`, `git_config`, `search_root`,
    `error`, `searched_paths`) are inherited unchanged. Adds the
    domain-specific `projects:` list.
    """

    projects: list[ProjectConfig] = Field(default_factory=list)
