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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    computed_field,
    model_validator,
)

from lib_python_config import LoadResult
from lib_python_projects.markers import AI_GENERATED_LABEL, AI_MODIFIED_LABEL

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


class BoardPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manage: bool = False


class PipelinesPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: bool = False


class Permissions(BaseModel):
    """Nested permissions namespace.

    The legacy flat form (`{create, modify, pr_create, pr_modify}`)
    was removed in v1 of the YAML schema — see plugin ticket #8.
    """

    model_config = ConfigDict(extra="forbid")
    issues: IssuesPermissions = Field(default_factory=IssuesPermissions)
    pulls: PullsPermissions = Field(default_factory=PullsPermissions)
    board: BoardPermissions = Field(default_factory=BoardPermissions)
    pipelines: PipelinesPermissions = Field(default_factory=PipelinesPermissions)
    # Ticket #252 gen 2: `verified`/`reason` are *derived*, non-input state
    # — private attributes exposed as read-only `@computed_field`
    # properties, not settable fields. They are unreachable from
    # `__init__`/`model_validate`/YAML (the `mode="before"` validator below
    # strips any `verified`/`reason` keys out of mapping input before
    # field validation ever sees them) — only `Permissions.from_probe`, the
    # sole writer, can produce `verified=True` through this library's public
    # construction/validation surface (`__init__` with a mapping,
    # `model_validate`, YAML loading); it does not and cannot prevent code
    # that already holds a `Permissions` instance from directly mutating
    # `_verified`/`_reason`, a general Python-language limitation shared by
    # every "private attribute" pattern, not a gap specific to this design.
    # This closes the gap the
    # earlier `source`-gated approach left open: a `ProjectConfig` built
    # directly (e.g. `model_validate`, bypassing the loader boundary that
    # forces `source="config"`) with a forged `source="token-discovery"`
    # *and* a hand-forged `permissions.verified=True` no longer sails
    # through — `verified`/`reason` are simply not settable input at all,
    # regardless of what `source` claims.
    #
    # Full contract, one paragraph: `verified=True` means a capability
    # probe ran *and* returned a usable signal — check `reason`. When
    # `reason is None`, the probe was fully clean and every issues/pulls
    # flag reflects an attempted independent confirmation (subject to each
    # provider's own sub-flag inference caveats — e.g. Azure DevOps's
    # `pulls_*` flags are inferred from a successful `connectionData` call,
    # not a real pull-request write check; see
    # `AzureDevOpsProvider.probe_token_capabilities`'s docstring). When
    # `reason` is a *partial* code (`"work_items_unavailable"`,
    # `"insufficient_scope"`), at least one flag is genuinely confirmed but
    # not every flag is. `verified=False` means the probe either never ran
    # (`reason="not_probed"`, the default for any `Permissions` that was
    # never routed through `from_probe`) or ran but returned no usable
    # signal at all (`"bad_credentials"`, `"network_error"`,
    # `"repo_invisible_to_token"`, `"permissions_field_missing"`, GitHub's
    # `"http_<code>"`) — in that case every issues/pulls flag is just its
    # unconfirmed `False` default, not a confirmed denial. `board`/
    # `pipelines` are never probed by `TokenCapabilities` at all and stay
    # at their all-False defaults *regardless* of `verified` — always read
    # them as unprobed placeholders, never as confirmed negatives. See
    # `TokenCapabilities.confirmed` in `providers/base.py`, which is the
    # single source of truth `loader._capabilities_to_permissions` uses to
    # compute this flag via `from_probe`.
    #
    # Round-trip note: `Permissions.model_dump()` followed by
    # `Permissions(**dump)` (the shape `ProjectConfig.model_dump()` +
    # reload also goes through) does not raise, and preserves values for
    # any *unprobed* instance (both sides `verified=False`,
    # `reason="not_probed"`) — but a *probed* instance downgrades to
    # unprobed on reload, since the dump is shape-identical to a forgery
    # and re-validation has no way to tell them apart. This is accepted,
    # not fixed: nothing in this library dumps a probed `Permissions` and
    # then re-validates it (the loader only validates fresh YAML; the
    # auto-discovery/token-discovery paths pass a `Permissions` *instance*
    # through untouched, and pydantic never revalidates instances by
    # default). A consumer that serializes a probed project across a
    # process boundary and revalidates it must treat the result as
    # unprobed.
    _verified: bool = PrivateAttr(default=False)
    _reason: str | None = PrivateAttr(default="not_probed")

    @model_validator(mode="before")
    @classmethod
    def _strip_forged_verification(cls, data: Any) -> Any:
        """Drop any `verified`/`reason` keys from mapping input.

        `verified`/`reason` are computed, not settable — this keeps
        `extra="forbid"` from rejecting them when they show up in
        `model_dump()` output (both the two-key case dumped by this model
        itself, and the case where a caller hand-writes them into YAML),
        while making sure they can never actually set the underlying
        private attributes. Non-mapping input (e.g. another `Permissions`
        instance, whose fields pydantic reads directly) passes through
        unchanged.
        """
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in ("verified", "reason")}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verified(self) -> bool:
        """True only when a live capability probe returned a usable
        signal — see the class docstring above for the full contract.
        Read-only: derived from a private attribute only
        `Permissions.from_probe` can set."""
        return self._verified

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reason(self) -> str | None:
        """Stable identifier for why the flags are unverified, or why a
        probe didn't come back clean — see the class docstring above.
        Read-only: derived from a private attribute only
        `Permissions.from_probe` can set."""
        return self._reason

    @classmethod
    def from_probe(
        cls,
        *,
        issues: IssuesPermissions | None = None,
        pulls: PullsPermissions | None = None,
        board: BoardPermissions | None = None,
        pipelines: PipelinesPermissions | None = None,
        confirmed: bool,
        reason: str | None,
    ) -> "Permissions":
        """Sole writer of `verified`/`reason` — the only way to produce a
        `Permissions` with `verified=True` through this library's public
        construction/validation surface (`__init__` with a mapping,
        `model_validate`, YAML loading). This does not and cannot prevent
        direct mutation of the private attributes by code that already
        holds a `Permissions` instance and chooses to bypass the public
        API — a general Python-language limitation shared by every
        "private attribute" pattern, not a gap specific to this design.
        Takes plain `bool`/`str | None`
        for `confirmed`/`reason` (not a provider-specific type) so this
        module keeps zero provider imports; callers (e.g.
        `loader._capabilities_to_permissions`) translate their own
        provider-facing result into these plain values first.

        This is an internal API for translating an already-completed,
        real capability probe (`TokenCapabilities.confirmed`/`.reason`)
        into stored `Permissions` state — not a public convenience
        constructor. It intentionally trusts its caller: nothing stops a
        caller from passing `confirmed=True` with a fabricated `reason`,
        but the one production call site (`loader._capabilities_to_permissions`)
        only ever forwards values read off a real `TokenCapabilities`
        instance returned by a provider's `probe_token_capabilities`, never
        arbitrary or user/YAML-controlled input.
        """
        perms = cls(
            issues=issues if issues is not None else IssuesPermissions(),
            pulls=pulls if pulls is not None else PullsPermissions(),
            board=board if board is not None else BoardPermissions(),
            pipelines=pipelines if pipelines is not None else PipelinesPermissions(),
        )
        perms._verified = confirmed
        perms._reason = reason
        return perms


class AutoLabels(BaseModel):
    """Per-project AI-attribution names for the `#153` auto-labels.

    Drives *both* the provider labels applied to tickets/PRs and the
    `#…` body/comment marker lines (see `markers.py`'s `MarkerSet`).
    Defaults to the module-level `AI_GENERATED_LABEL`/`AI_MODIFIED_LABEL`
    constants so a project with no `auto_labels:` block behaves exactly
    as before.

    `AI_NOT_PLANNED_LABEL` (GitLab's `state_reason` stand-in) is
    deliberately not configurable here — out of scope for #153.
    """

    model_config = ConfigDict(extra="forbid")
    ai_generated: str = Field(default=AI_GENERATED_LABEL, min_length=1)
    ai_modified: str = Field(default=AI_MODIFIED_LABEL, min_length=1)


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
    # Names the Projects-v2 *iteration* field backing the normalized
    # `Ticket.milestone` projection (ticket #151) — GitHub has no native
    # issue-milestone equivalent in this surface, so milestone read/write
    # is modeled via the board's iteration field instead. `None` (default)
    # means: on read, auto-detect the first iteration-typed field on the
    # item (deterministic by field order); on write, `milestone=` raises
    # `ValueError` since the write path can't auto-detect by type.
    iteration_field: str | None = None


class AzureBoardsBinding(BoardBinding):
    """Azure Boards board binding.

    An Azure Boards board is bound to a **team + backlog level**, not the
    project alone — `team` and `board` locate it (e.g. team name and one of
    `"Stories"`, `"Epics"`, `"Features"`, or a custom board name). Both are
    optional here so the schema stays valid without them; the Azure DevOps
    provider raises `ValueError` at call time if either is missing when it
    actually needs to resolve the live board.

    The Doing/Done split-column marker (Azure Boards' `System.BoardColumnDone`
    boolean) has no dedicated field: set `provider_extras["split_done_column"]`
    to the logical column name (from `Board.columns`) that represents the
    "done" half of a split column. Its sibling "doing" half is inferred as
    the other logical column that resolves to the same native column name.
    """

    kind: Literal["azure-boards"]
    team: str | None = None
    board: str | None = None


class BoardAutoLabels(BaseModel):
    """Board-column-dependent auto-labels (ticket #154).

    Distinct and independent from the top-level `ProjectConfig.auto_labels`
    (`AutoLabels`): that block names the AI-attribution
    (`ai-generated`/`ai-modified`) labels/markers applied regardless of
    board state. This block instead lets operators declare "when this
    happens, apply these labels" rules scoped to the board's lifecycle:

    - `on_create`: labels applied whenever a ticket is created.
    - `on_update`: labels applied whenever a ticket is updated.
    - `on_move_to`: labels applied when a ticket moves into a specific
      logical board column, keyed by the column name (must match an
      entry in `Board.columns`).

    `on_create`/`on_update` are honored on all three providers (GitHub,
    GitLab, Azure DevOps), folded additively into each provider's
    existing label set. `on_move_to` is currently honored **only on
    GitHub Projects v2** (fires from `update_ticket` when `custom_fields`
    carries a new value for the board's `status_field`). Azure Boards and
    GitLab accept and validate `on_move_to` — it does not raise — but do
    not fire it yet; it is a documented no-op on those two providers.
    """

    model_config = ConfigDict(extra="forbid")
    on_create: list[str] = Field(default_factory=list)
    on_update: list[str] = Field(default_factory=list)
    on_move_to: dict[str, list[str]] = Field(default_factory=dict)


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
    # Board-column-dependent auto-labels (ticket #154). Defaults to empty
    # lists/dict, so a board with no `auto_labels:` block behaves exactly
    # as before.
    auto_labels: BoardAutoLabels = Field(default_factory=BoardAutoLabels)

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

    @model_validator(mode="after")
    def _check_auto_labels_move_keys(self) -> "Board":
        if self.auto_labels.on_move_to:
            columns_lower = {col.lower() for col in self.columns}
            for key in self.auto_labels.on_move_to:
                if key.lower() not in columns_lower:
                    raise ValueError(
                        f"board auto_labels 'on_move_to' key {key!r} does not "
                        f"match any entry in 'columns' {self.columns!r}"
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

    def auto_label_names_on_create(self) -> list[str]:
        """Order-preserving dedup of `auto_labels.on_create`."""
        return list(dict.fromkeys(self.auto_labels.on_create))

    def auto_label_names_on_update(self) -> list[str]:
        """Order-preserving dedup of `auto_labels.on_update`."""
        return list(dict.fromkeys(self.auto_labels.on_update))

    def auto_label_names_for_move(self, value: str) -> list[str]:
        """Labels to apply when a ticket moves into the column `value`.

        For each configured `on_move_to` column key, matches `value`
        case-insensitively against either the logical column name (the
        key itself) or its resolved provider-native value
        (`self.resolve(key)`). Returns the order-preserving dedup of
        labels across every matching key; `[]` when nothing matches.
        """
        names: list[str] = []
        value_lower = value.lower()
        for key, key_labels in self.auto_labels.on_move_to.items():
            if key.lower() == value_lower or self.resolve(key).lower() == value_lower:
                names.extend(key_labels)
        return list(dict.fromkeys(names))


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
        """Whether `token_env` is set on this config AND that environment
        variable is currently non-empty in the process environment.

        This is **not evidence the token authenticates or is authorized**
        against the live provider — it only reflects local environment
        state, not a round-trip to GitHub/GitLab/Azure DevOps. A token can
        be present and still be expired, revoked, or scoped to the wrong
        surface (e.g. missing "Work Items" access on Azure DevOps). For an
        actual read on what the token can do, see
        `TokenCapabilityProvider.probe_token_capabilities`.
        """
        return bool(self.token_env and os.environ.get(self.token_env))

    permissions: Permissions = Field(default_factory=Permissions)
    source: Source = "config"
    # Azure DevOps only. When unset, the provider discovers a sensible
    # default once per project (Issue → Bug → User Story → Product
    # Backlog Item → Requirement). Ignored by github/gitlab.
    default_work_item_type: str | None = None
    # Azure DevOps only (ticket #172). Scopes work-item read/write paths
    # (list_tickets, list_labels, create_ticket) to a `System.AreaPath`
    # sub-tree, so two wrapper `ProjectConfig`s that share the same
    # `organization/ado_project` (differing only in the unused
    # `repository` path segment) can be genuinely scope-isolated rather
    # than both resolving to the same team project. Unset (the default)
    # preserves today's unscoped behaviour exactly. An explicit
    # `TicketFilters.area_path` on a `list_tickets` call still overrides
    # this config-level default entirely. Ignored by github/gitlab.
    area_path: str | None = None
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
    # Projects v2 support landed in #118, Azure Boards support in #119
    # (both via `<Provider>.list_board_columns` / `TicketFilters.board_column`).
    board: Board | None = None
    # Per-project AI-attribution names (ticket #153). Defaults to the
    # module-level ai-generated/ai-modified names when unset, so existing
    # `projects.yml` entries behave unchanged.
    auto_labels: AutoLabels = Field(default_factory=AutoLabels)

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

    # `source` is provenance labelling only (ticket #252 gen 2): it no
    # longer gates anything. `Permissions.verified`/`.reason` are derived,
    # non-input fields (see `Permissions` above) that only
    # `Permissions.from_probe` can set to a probed state — so an entry's
    # claimed `source` (including a forged `source="token-discovery"`)
    # cannot, by itself, produce `verified=True`.

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


class InvalidProjectEntry(BaseModel):
    """A single project entry that failed `ProjectConfig` schema validation.

    Recorded (rather than raised) so one schema-invalid entry — e.g. a
    malformed `board:` block — doesn't collapse the whole registry
    (ticket #132). `index` and `id` let the calling agent locate the
    offending entry in the source YAML without reading server stderr;
    `error` is the raw Pydantic `ValidationError` message.

    Structural per-entry failures (non-mapping item, reserved `_auto`
    id, duplicate id) are never recorded here — those stay fatal and
    still raise `ConfigError`, collapsing the whole registry as before.
    """

    model_config = ConfigDict(extra="forbid")
    index: int
    id: str | None = None
    error: str


class ProjectsLoadResult(LoadResult):
    """`LoadResult` extended with the resolved project list.

    Subclasses the generic `LoadResult` from `lib_python_config` so the
    diagnostic fields (`state`, `config_file`, `git_config`, `search_root`,
    `error`, `searched_paths`) are inherited unchanged. Adds the
    domain-specific `projects:` list.
    """

    projects: list[ProjectConfig] = Field(default_factory=list)
    discovery_truncated: bool = False
    # Schema-invalid entries skipped during load (ticket #132). Empty
    # unless at least one project entry failed `ProjectConfig` validation.
    invalid_projects: list[InvalidProjectEntry] = Field(default_factory=list)


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
