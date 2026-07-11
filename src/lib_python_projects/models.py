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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from lib_python_config import LoadResult

Provider = Literal["github", "gitlab", "azuredevops"]
Source = Literal["config", "git-remote", "token-discovery"]


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


class BoardBinding(BaseModel):
    """Shared shape for provider-specific board bindings.

    `map` translates logical column names (see `Board.columns`) to the
    provider-native primitive (e.g. a GitHub Projects v2 status option, or
    an Azure Boards column/state). `provider_extras` is a generic escape
    hatch for provider-specific settings that don't warrant a dedicated
    field yet; it is not validated beyond being a dict.
    """

    model_config = ConfigDict(extra="forbid")
    map: dict[str, str] | None = None
    provider_extras: dict[str, Any] = Field(default_factory=dict)


class GithubProjectsV2Binding(BoardBinding):
    """GitHub Projects v2 board binding.

    `owner` and `project_number` locate the project: GitHub Projects v2
    are org- or user-scoped (not repo-bound), addressed via
    `organization(login:).projectV2(number:)` or
    `user(login:).projectV2(number:)`. Both are optional here so the
    schema stays valid without them (e.g. a `board:` block declared
    before the owning project/number is known); the GitHub provider
    raises `ValueError` at call time if either is missing when it
    actually needs to resolve the live board. Which of org/user `owner`
    resolves to is auto-detected at runtime by the provider — there is
    deliberately no `owner_type` field to configure.

    `status_field` names the single-select field whose options are the
    board's columns — conventionally `"Status"`, hence the default.
    """

    kind: Literal["github-projects-v2"]
    owner: str | None = None
    project_number: int | None = None
    status_field: str = "Status"


class AzureBoardsBinding(BoardBinding):
    kind: Literal["azure-boards"]


class Board(BaseModel):
    """Optional board configuration for a project.

    `columns` is the ordered list of logical column names the agent
    reasons about; `binding` maps those logical names onto a specific
    provider's native board primitives.
    """

    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    binding: Annotated[
        GithubProjectsV2Binding | AzureBoardsBinding, Field(discriminator="kind")
    ]

    @model_validator(mode="after")
    def _check_columns(self) -> "Board":
        if not self.columns:
            raise ValueError("board 'columns' must not be empty")
        seen: dict[str, str] = {}
        for col in self.columns:
            key = col.lower()
            if key in seen:
                raise ValueError(
                    f"board 'columns' has a duplicate entry (case-insensitive): "
                    f"{col!r}"
                )
            seen[key] = col
        return self

    @model_validator(mode="after")
    def _check_map_keys(self) -> "Board":
        if self.binding.map:
            columns_lower = {col.lower() for col in self.columns}
            for key in self.binding.map:
                if key.lower() not in columns_lower:
                    raise ValueError(
                        f"board binding 'map' key {key!r} does not match any "
                        f"entry in 'columns' {self.columns!r}"
                    )
        return self

    def resolve(self, column: str) -> str:
        """Resolve a logical column name to its provider-native value.

        Looks up `column` in `binding.map` case-insensitively; falls back
        to the column name itself (identity) when unmapped.
        """
        if self.binding.map:
            for key, value in self.binding.map.items():
                if key.lower() == column.lower():
                    return value
        return column


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
    # Default branch for this project. Consumers use this as the base
    # branch for PRs and comparisons. Defaults to "main" for backward
    # compatibility.
    default_branch: str = "main"
    # On-disk checkout path, when known. Optional for both config- and
    # git-remote-sourced projects; populated by the loader for the
    # auto-discovered CWD repo.
    local_path: str | None = None
    # Optional board configuration: an ordered list of logical columns plus
    # a provider-specific binding. Resolution logic (turning this into
    # actual provider board calls) is implemented per-provider: GitHub
    # Projects v2 support landed in #118 (`GitHubProvider.list_board_columns`
    # / `TicketFilters.board_column`); Azure Boards support is #119.
    board: Board | None = None

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
    discovery_truncated: bool = False


class ProjectMatch(BaseModel):
    """A single project paired with its relevance score.

    `score` is in [0.0, 1.0].  1.0 means an exact token match against one
    of the scored fields; 0.0 means no similarity at all.
    """

    model_config = ConfigDict(extra="forbid")

    project: ProjectConfig
    score: float


class FindResult(BaseModel):
    """Result returned by `find_projects`.

    `matches` is sorted descending by score and contains only projects whose
    best token score is at or above the relevance floor.

    `hint` is set to `"no matches above relevance floor"` when at least one
    project was scored but every score fell below the floor.  It is `None`
    when the project list was empty to begin with, or when `matches` is
    non-empty (good results were found).
    """

    model_config = ConfigDict(extra="forbid")

    matches: list[ProjectMatch] = Field(default_factory=list)
    hint: str | None = None
