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
