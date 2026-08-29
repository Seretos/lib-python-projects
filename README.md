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
    # PR diff discovery (ticket #240):
    PRFileDiff, DiffHunkRange, PRDiffProvider, parse_diff_hunk_ranges,
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

`permissions` also carries `verified` and `reason` (ticket #252) — these say
whether the flags above were confirmed by a live capability probe rather
than merely declared here. They are **derived, not YAML-settable at all**:
`verified`/`reason` are computed from private, non-input state that only a
real probe (`Permissions.from_probe`) can write, so nothing in `projects.yml`
— including `permissions.verified: true` — can ever produce `verified=true`;
a config entry always sees `verified: false, reason: "not_probed"`,
regardless of what it writes or what `source` it claims. `verified` only
covers `issues`/`pulls` — the probe never touches `board` or `pipelines`, so
those stay at their `False` defaults regardless of what `verified`/`reason`
report.

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

**`trigger_pipeline`'s `workflow` is stricter than `wait_for_run`'s.**
`trigger_pipeline` must resolve `workflow` to a concrete dispatch/queue
target *before* making any HTTP call, so on GitHub a bare display name
(e.g. `"Release"`) raises `ValueError` up front rather than being
forwarded to a dispatch request that would 404 — only a filename
(`"release.yml"`) or numeric workflow id is accepted. On Azure DevOps a
name or numeric id both work, but an unresolvable one raises
`AzureDevOpsError(404, ...)` before any build is queued. `wait_for_run`,
by contrast, never raises for an unresolvable `workflow` — it accepts any
string and, whenever the value can't be pushed down as a server-side
filter, silently falls back to `apply_run_filters`'s client-side match
against `run.name` (matching zero runs, and eventually timing out, is a
valid outcome there, not an error). Passing the same bare display name to
both calls will make `trigger_pipeline` raise immediately while a
standalone `wait_for_run(..., workflow="Release")` call would happily
poll and match client-side — don't assume symmetry between the two.

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

### Discovering workflows / is CI configured? (ticket #209)

Before calling `trigger_pipeline`, an agent can ask "does this project have
CI at all?" and "which workflows can I trigger?" via the
`CIConfigurationProvider` mixin all three providers implement:

```python
from lib_python_projects.providers.base import Workflow, NO_CI_SENTINEL

workflows = provider.list_workflows(project, token)
# list[Workflow] — [] when the project has no CI configured at all.

configured = provider.is_ci_configured(project, token)
# bool(list_workflows(...)) in spirit — cheaper on some providers.

if workflows:
    run = provider.trigger_pipeline(
        project, token, workflows[0].dispatch_target, ref="main",
    )
```

`Workflow.dispatch_target` is guaranteed to work **verbatim** as the
`workflow` argument to that same provider's `trigger_pipeline` — no
provider-specific munging needed:

| Provider | `dispatch_target` | Notes |
|---|---|---|
| GitHub | workflow filename (e.g. `"ci.yml"`) | falls back to the numeric workflow id (as a string) when no `path` is available |
| GitLab | the CI config path (e.g. `".gitlab-ci.yml"`) | GitLab has no per-workflow concept; `trigger_pipeline` validates it's non-empty but never sends it to the API |
| Azure DevOps | numeric build-definition id (as a string) | unique — avoids the name-resolution 404s a bare display name can hit |

**Error semantics** mirror the original GitHub-only `_has_workflows` probe
(ticket #200), generalized to all three providers: only a definitive
"not configured" signal (404, empty listing, missing/absent CI config
file) folds to `False`/`[]`. Authentication failures (401/403) and server
errors (5xx) propagate as the provider's native error type
(`GitHubError`/`GitLabError`/`AzureDevOpsError`) — a caller must not
conclude "no CI" from a response that actually means "the token can't see
it" or "the server is down". Known, deliberately-unchanged limitation:
GitHub returns 403 (not 404) when Actions is disabled organization-wide,
so that case still raises rather than reporting "no CI".

**The uniform `"no-ci"` sentinel.** All five run-listing methods
(`list_runs_for_branch`/`_commit`/`_tag`/`_ticket`/`_recent`), on all
three providers, append `NO_CI_SENTINEL` (`"no-ci"`) as the **last**
element of `resolved_refs` whenever they are about to return an empty
`runs` list **and** `is_ci_configured(...)` is `False`:

```python
runs, resolved_refs = provider.list_runs_for_branch(project, token, "main")
if not runs and resolved_refs and resolved_refs[-1] == NO_CI_SENTINEL:
    print("this project has no CI configured at all")
```

The sentinel is never appended when `runs` is non-empty (a non-empty
result already proves CI is configured — the extra probe request is
skipped entirely), and it is appended regardless of *why* `runs` came
back empty (ref not found, no linked PR/MR/work item, ref exists but has
no runs, ...) — it answers "is there CI at all," not "why no runs."
`wait_for_run` never triggers this probe on any poll iteration — it polls
through an internal unprobed helper, so a repeated `trigger_pipeline`
wait loop doesn't pay for a CI-configuration check on every empty poll.

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
- Ticket #209: `list_workflows`/`is_ci_configured`
  (`CIConfigurationProvider`) on all three providers, so an agent can
  discover CI workflows and check whether a project has CI configured at
  all before calling `trigger_pipeline`. The `"no-ci"` sentinel
  (`NO_CI_SENTINEL`) is now appended uniformly across all five
  run-listing methods on all three providers, not just GitHub
  branch-mode. See "Discovering workflows / is CI configured?" above.
