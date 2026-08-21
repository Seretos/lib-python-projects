# lib-python-projects

Domain library for the Seretos agent-plugin ecosystem. Bundles the
project-list model (whitelist of repos the agent can act on), the provider
abstraction (GitHub / GitLab / Azure DevOps wire layer), git-remote
auto-discovery, and a `load_projects` loader that builds on top of the
generic `lib-python-config` machinery.

Extracted from `agent-project-issues` so other plugins (e.g. release
automation, repo dashboards) can reuse the same project model without
pulling in the MCP server itself.

## Install

`lib-python-projects` depends on the sibling source library
`lib-python-config`. Install it as an editable dep first:

```bash
pip install -e ../lib-python-config
pip install -e .
```

## Public API

```python
from lib_python_projects import (
    ProjectConfig,
    Permissions,
    IssuesPermissions,
    PullsPermissions,
    BoardPermissions,
    PipelinesPermissions,
    Board,
    BoardBinding,
    GithubProjectsV2Binding,
    AzureBoardsBinding,
    ConfigDocument,
    ProjectsLoadResult,
    Provider,
    Source,
    load_projects,
    resolve_token,
)

# Providers are exposed through their sub-package:
from lib_python_projects.providers.github import GitHubProvider, GitHubError
from lib_python_projects.providers.gitlab import GitLabProvider, GitLabError
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers.base import (
    Ticket, Comment, PullRequest, ReviewComment, Review,
    TicketFilters, PRFilters, RelationKind, Relation,
    StatusSpec, BoardColumnSpec, PipelineRun, FailingJob, PipelineFailure,
    TokenCapabilities, TokenCapabilityProvider,
    RelationKindUnsupported, RelationNotFound, RelationAlreadyExists,
    Label, LabelOperationUnsupported,
    normalize_timestamp,
    WRITABLE_RELATION_KINDS, READ_ONLY_RELATION_KINDS,
    # Pipeline trigger / run filtering / refs / releases (ticket #200):
    Ref, Release, EVENT_ALIASES, resolve_event_alias, apply_run_filters,
    now_utc,
)
```

## Board support

`ProjectConfig.board` is an optional, provider-agnostic board configuration:
an ordered list of logical `columns` plus a provider-specific `binding`.
`Board.resolve(column)` turns a logical column name into its provider-native
value — an explicit `binding.map` entry wins, otherwise it falls back to the
column name itself (case-insensitive identity).

GitHub Projects v2 support (ticket #118) is implemented on `GitHubProvider`:

```python
from lib_python_projects.providers.base import TicketFilters

provider = GitHubProvider()

# Discover the live board's columns (logical name, resolved native option
# name, and that option's provider-native id):
columns = provider.list_board_columns(project, token)

# List only the issues currently sitting in one logical column. The column
# is resolved against `project.board` the same way `Board.resolve()` does;
# `labels`/`not_labels`/`assignee`/`states`/`status` still apply.
tickets, has_more = provider.list_tickets(
    project, token, TicketFilters(board_column="Review"),
)
```

`board_column` requires `project.board.binding` to be a `GithubProjectsV2Binding`
with `owner` and `project_number` set (the org/user login and project number
GitHub Projects v2 are scoped under — auto-detected as org vs user at call
time, not configured). It raises `ValueError` when combined with `search` or
`area_path`, and on GitLab (no equivalent concept).

Azure Boards support (ticket #119) is implemented on `AzureDevOpsProvider`.
An Azure Boards board is bound to a **team + backlog level** (not the
project alone), so the binding needs `team` and `board`:

```yaml
projects:
  - id: acme
    provider: azuredevops
    path: acme-org/acme-project/acme-repo
    board:
      columns: [Todo, Doing, Done]
      binding:
        kind: azure-boards
        team: "Acme Team"
        board: Stories
        # Doing/Done split columns (System.BoardColumnDone) have no
        # dedicated field — mark the "done" half via provider_extras:
        provider_extras:
          split_done_column: Done
```

```python
provider = AzureDevOpsProvider()

# Discover the live board's columns (logical name, resolved native column
# name, that column's id, its System.State stateMappings, and whether it's
# a Doing/Done split column):
columns = provider.list_board_columns(project, token)

# List only the work items currently sitting in one logical column. The
# column is resolved against `project.board` via `Board.resolve()` and
# filtered on `System.BoardColumn`; when the column is the "done" half of
# a split column, `System.BoardColumnDone` is constrained too.
tickets, has_more = provider.list_tickets(
    project, token, TicketFilters(board_column="Done"),
)
```

`board_column` raises `ValueError` when `project.board` is unset, the
binding isn't `kind="azure-boards"`, the binding is missing `team`/`board`,
or the column isn't one of `board.columns` — never silently ignored or
falling back to an unfiltered result. When board context isn't configured,
use `status` / `states` (matching `System.State` directly) as a manual
fallback filter instead.

## Pipeline triggering, run filtering, refs & releases (ticket #200)

All three providers expose a matching, provider-agnostic surface for
triggering a pipeline, disambiguating the run it created, filtering run
listings, and resolving refs/releases. This is provider-layer only — an
orchestrator (e.g. `agent-project-issues`) is responsible for any
`projects.yml` gate that decides *whether* a caller may trigger.

### Opting in: the `pipelines.trigger` permission

```yaml
projects:
  - id: acme
    provider: github
    path: acme/backend
    permissions:
      pipelines:
        trigger: true   # defaults to false; omitting the block is unchanged
```

`PipelinesPermissions` follows the same `extra="forbid"` nested-namespace
shape as `board`/`issues`/`pulls` — unknown keys raise, and a `projects.yml`
that omits `pipelines` entirely still loads unchanged (the field defaults
via `Field(default_factory=PipelinesPermissions)`).

### Triggering a run and resolving it

```python
provider = GitHubProvider()

run = provider.trigger_pipeline(
    project, token, "release.yml", ref="main", inputs={"version": "1.2.3"},
)
# run is None only when wait=False (GitHub) or on a genuine timeout.

# Resolve a trigger you already know the dispatch time for, standalone:
run = provider.wait_for_run(
    project, token, since=t0, workflow="release.yml", ref="main", timeout=60.0,
)
```

`workflow` accepts a filename or numeric id on GitHub/Azure DevOps (a bare
display name still works — it just isn't pushed down as a server-side
path/`definitions=` filter, only matched client-side against `run.name`);
GitLab has no per-workflow concept and ignores it beyond signature parity.
`since` is a **required** keyword-only argument on `wait_for_run` — an
unbounded wait would happily return a pre-existing run instead of the one
just triggered. Both `trigger_pipeline` and `wait_for_run` return the
**oldest** matching run at/after `since` and `None` on timeout, never
raising for "not found yet". A non-2xx dispatch/queue response still
raises the provider's error type.

### Filtering run listings

All five listing methods (`list_runs_for_branch`, `list_runs_for_commit`,
`list_runs_for_tag`, `list_runs_for_ticket`, `list_runs_recent`) accept
three additional keyword-only filters, on all three providers:

```python
runs, refs = provider.list_runs_recent(
    project, token, workflow="release", event="manual",
    since="2026-08-21T09:00:00Z", limit=5,
)
```

Each provider pushes `workflow`/`event`/`since` down as server-side query
params where the API supports it, then **always** re-applies
`apply_run_filters` (from `providers.base`) client-side as the
authoritative final pass — so a provider-native string the server ignored
or rejected still produces a correct result, and `limit` is applied
*after* filtering (`limit=1` means "one matching run", not "one of the
recent runs, maybe filtered away"). This is what lets a concurrent-run-
heavy `main` branch be disambiguated reliably.

`event` is resolved through a canonical vocabulary shared across
providers — pass the canonical name or any provider-native string
verbatim:

| Canonical | GitHub (`event`) | GitLab (`source`) | Azure DevOps (`reason`) |
|---|---|---|---|
| `manual` / `workflow_dispatch` | `workflow_dispatch` | `web` | `manual` |
| `push` | `push` | `push` | `individualCI` |
| `schedule` | `schedule` | `schedule` | `schedule` |
| `pull_request` | `pull_request` | `merge_request_event` | `pullRequest` |
| `api` | `repository_dispatch` | `trigger` | `userCreated` |

Any string outside this table (e.g. `event="individualCI"` on Azure
DevOps) passes through unchanged on every provider.

### Resolving refs and listing releases

```python
ref = provider.get_ref(project, token, "v1.2.3")
# Ref(name="v1.2.3", kind="tag", sha="<peeled commit sha>", url=...)
# Resolution order is branch -> tag -> commit; a branch and a tag sharing
# a name resolve as the branch. `sha` is always the peeled *commit* sha
# — GitHub's annotated tags are dereferenced via a second hop, GitLab's
# and Azure DevOps's tag payloads already carry it directly.

releases = provider.list_releases(project, token, limit=20)
# list[Release] — most recent first.
```

Azure DevOps has no native "release" concept distinct from an annotated
Git tag: `list_releases` maps annotated tags into the shared `Release`
shape, and **`draft`/`prerelease` are always `False` there — not
representable on Azure DevOps.** GitLab has no prerelease flag either, so
`prerelease` is always `False` on GitLab too.

## Usage

```python
from pathlib import Path
from lib_python_projects import load_projects

result = load_projects(Path.cwd())

if result.state == "ok":
    for p in result.projects:
        print(p.id, p.provider, p.path, p.local_path)
elif result.state == "config_error":
    print("config broken:", result.error)
```

`load_projects` defaults match the `agent-project-issues` plugin (config
dir `.seretos/`, filename `project-issues.yml`, env vars
`PROJECT_ISSUES_CONFIG` / `PROJECT_ISSUES_PLUGIN_ROOT` /
`PROJECT_ISSUES_PLUGIN_CWD`). Other consumers pass their own values:

```python
result = load_projects(
    Path.cwd(),
    config_filename="release.yml",
    override_env="RELEASE_PLUGIN_CONFIG",
    plugin_root_env="RELEASE_PLUGIN_ROOT",
    search_env_vars=("RELEASE_PLUGIN_CWD", "CLAUDE_PROJECT_DIR"),
)
```

## What's new in 0.1.0

- `ProjectConfig.local_path: str | None = None` — the local checkout path
  for the project, when known. Auto-populated for `source="git-remote"`
  projects from the discovered git-repo root; readable from YAML for
  `source="config"` projects.
- Ticket #200: `trigger_pipeline`/`wait_for_run` (trigger a
  `workflow_dispatch`-style pipeline and reliably resolve the run it
  created), `workflow`/`event`/`since` filters on all five run-listing
  methods, `get_ref`/`list_releases` read APIs, and the
  `permissions.pipelines.trigger` opt-in — on all three providers. See
  "Pipeline triggering, run filtering, refs & releases" above.
