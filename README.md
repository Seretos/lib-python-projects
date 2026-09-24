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
    # Slim `light=True` write results (ticket #265):
    TicketRef, CommentRef, PullRequestRef,
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
    # Issue-template discovery (ticket #259):
    IssueTemplate, TemplateField, TemplateViolation, IssueTemplateProvider,
)

# Provider-free issue-template validation/rendering (ticket #259):
from lib_python_projects.templates import validate_ticket_body, render_skeleton
```

### Opting out of the post-write reload: `light=True` (ticket #265)

Each of the six write methods — `create_ticket`, `update_ticket`,
`add_comment`, `create_pr`, `update_pr`, `merge_pr` — takes an opt-in
keyword-only `light: bool = False`. Passing `light=True` skips every
request whose sole purpose is enriching the return value (a post-write
poll/re-GET, a re-fetch of reviews/approvals/votes, ...) and returns a
slim `TicketRef` / `CommentRef` / `PullRequestRef` built only from the
write call's own response instead of the full `Ticket` / `Comment` /
`PullRequest`. `light=False` (the default) is unchanged, byte-for-byte,
down to the requests it sends on the wire — every existing caller keeps
working exactly as before.

```python
# Full object (default): may cost a post-write reload/reread.
ticket = provider.create_ticket(project, token, title="...", body="...")

# Light: only the requests needed to perform the write.
ref = provider.create_ticket(project, token, title="...", body="...", light=True)
# ref.id, ref.url, ref.status, ref.labels, ref.updated_at,
# ref.custom_fields (only when this call wrote some), ref.idempotent_replay
```

Every ref field comes from the body of the write request this call
actually issued (or, for `merge_pr` only, the single conditional
post-write status read the write budget permits), or is echoed from the
call's own arguments where the write response can't carry it (e.g.
`PullRequestRef.number` on GitHub's `merge_pr`, whose merge response
never repeats the PR number back). A field this call's write genuinely
can't supply stays `None` rather than being backfilled with an extra
request — each provider method's docstring names exactly which fields
that is and why, under a `Light mode (`light=True`)` block.

Idempotency replay (`idempotency_key=`, ticket #150) composes with
`light`: a `light=True` retry of a previously-used key returns the
stored ref with `idempotent_replay=True` and issues no new request; a
`light=False` retry of a key whose original create used `light=True`
reloads once (`get_ticket`/`get_pr`) and returns the full model, also
with `idempotent_replay=True` — the one request cost AC3's promise
("`light=False` always returns a `Ticket`/`PullRequest`") requires on
that specific mixed-`light` replay path.

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

### Label-mode boards: `columns` without a `binding` (ticket #285)

`board.binding` is optional. A project that tracks its logical columns via
issue labels instead of a live provider board — a GitLab project (GitLab
has no board binding concept), or a GitHub project with no Projects v2
board — can configure `columns` (plus optional `label_map` and
`closed_column`) with no `binding` at all:

```yaml
projects:
  - id: acme
    provider: gitlab
    path: acme-group/backend
    board:
      columns: [Todo, Doing, Done]
      label_map:
        Todo: status/todo
        Doing: status/doing
      closed_column: Done
```

`label_map` (logical column -> label name) and `closed_column` (the
logical column that corresponds to the ticket's native closed state,
*not* a label) are validated against `columns` the same way
`binding.map` is — an unmatched key still lands the whole project in
`invalid_projects`, and `closed_column` may not also appear as a
`label_map` key. Both fields are inert data on `Board`: this library
validates and stores them but does not itself resolve a ticket's labels
to a column (that consumer-facing lookup is out of scope here — see
agent-project-issues#365). `Board.resolve(column)` falls back to
identity (the logical column name unchanged) when `binding` is unset, so
label-mode boards behave the same as an unmapped bound column. Every
provider call that requires a live board binding (`list_board_columns`,
`ensure_board_column`, `board_column` filtering) still raises its usual
`ValueError` for a label-mode board — it just names the binding as
missing rather than crashing.

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

### Waiting for a commit's CI to finish (ticket #275)

```python
result = provider.wait_for_pipeline(
    project, token, sha, timeout_s=600.0, poll_interval_s=20.0,
)
result.state      # "success" | "failure" | "pending" | "no_verdict" | "no_runs"
result.runs       # the last polled run rows (same shape as list_runs_for_commit)
result.waited_s   # elapsed seconds
```

One blocking call, shared by GitHub, GitLab and Azure DevOps (the
`PipelineWaitProvider` mixin in `providers.base`), so a caller that may not
run its own `sleep` loop can still gate on CI. It polls
`list_runs_for_commit` until a verdict or `timeout_s`:

- `failure` - any run red (`failure`, `failed`, `timed_out`,
  `startup_failure`); returned at once, without waiting for other runs.
- `success` - at least one run, all `completed` with conclusion `success`.
- `no_verdict` - all runs finished, none red, but at least one was
  cancelled/skipped/otherwise inconclusive. A cancelled run is never
  reported as `failure`.
- `pending` - runs were still in flight when `timeout_s` expired. No
  exception is raised.
- `no_runs` - no run appeared for the whole timeout (a push can precede run
  creation, so an empty listing is retried), or the project has no CI at all.

`poll_interval_s` defaults to 20 s and is clamped to a 5 s floor; the last
sleep is truncated so the call returns within `timeout_s`. `now`/`sleep`
are injectable keyword-only arguments for tests.

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

## Reading issue templates & validating ticket bodies (ticket #259)

`create_ticket` writes straight to the provider API — it never goes through
a project's web-UI issue templates (GitHub issue forms, GitLab issue
templates, Azure DevOps work-item templates), so nothing enforces that a
ticket filed by an agent actually satisfies them. Two pieces close that
gap: the `IssueTemplateProvider` mixin (read the templates) and the
provider-free `lib_python_projects.templates` module (validate/render
against them).

### `IssueTemplateProvider`

All three providers implement `list_issue_templates(project, token) ->
list[IssueTemplate]`:

```python
from lib_python_projects.providers.github import GitHubProvider

provider = GitHubProvider()
templates = provider.list_issue_templates(project, token)
# [] when the project has no templates configured at all.
```

| Provider | Source | `IssueTemplate.kind` |
|---|---|---|
| GitHub | `.github/ISSUE_TEMPLATE/*.yml`/`*.yaml` (issue forms) and `*.md` (legacy templates); `config.yml` is excluded | `"form"` for YAML forms, `"markdown"` for `.md` templates |
| GitLab | `GET /projects/:id/templates/issues` | `"markdown"` always — GitLab issue templates are plain markdown, no field structure |
| Azure DevOps | `GET .../_apis/wit/templates` for the project's default work-item type (resolved via `_default_work_item_type`, never hardcoded) | `"workitem"` always |

Only a definitive "no templates" signal (404, empty listing) folds to
`[]`; authentication failures (401/403) and server errors (5xx) propagate
as the provider's native error type, mirroring `CIConfigurationProvider`'s
contract above.

### The `### <label>` section convention

This is **GitHub's own** issue-form rendering, not a convention this
library invented: when a contributor fills out a GitHub issue form, each
field's answer is rendered into the issue body as a `### <label>` markdown
heading followed by the answer (see GitHub's docs on ["Syntax for issue
forms"](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms)).
`lib_python_projects.templates` reuses that exact convention as the
contract a submitted ticket body is checked against:

```python
from lib_python_projects import templates

violations = templates.validate_ticket_body(ticket.body, template)
# [] means the body satisfies every required field.
for v in violations:
    print(v.field_label, v.reason, "->", v.expected)

skeleton = templates.render_skeleton(template)
# a fillable "### <label>" skeleton — description/placeholder/dropdown
# options rendered as HTML comments, checkboxes as real "- [ ] option"
# lines, ready to hand to a human or an agent to fill in before
# create_ticket.
```

`validate_ticket_body` mirrors GitHub's own form-submission semantics: a
required field with no matching heading is `"missing"`; a heading present
but empty (or left at GitHub's `_No response_` sentinel, after stripping
HTML comments) is `"empty"`; content equal to the field's placeholder text
is `"placeholder"`; an invalid dropdown selection is `"invalid-option"`;
required checkboxes with nothing checked is `"no-option-checked"`.
`type="markdown"` form fields (section headers/instructions, not real
inputs) are never checked.

### Markdown and work-item templates get lighter checks

- **`kind="markdown"`** (GitLab templates, GitHub `.md` templates): there
  are no fields to validate field-by-field, so `validate_ticket_body`
  falls back to **heading-presence checking only** — every `##`/`###`
  heading in the template's own body must appear as a heading line
  (not merely mentioned in prose) in the submitted body, reported as
  `reason="heading-missing"`.
- **`kind="workitem"`** (Azure DevOps): `validate_ticket_body` always
  returns `[]` — Azure work-item templates don't expose per-field
  required-ness the way GitHub forms do, so there's no validation teeth
  here yet.

### `IssueTemplate.required_sections`

A computed, read-only property listing the section labels a template
actually requires, in order — the same labels `validate_ticket_body` would
report as `"missing"`/`"heading-missing"` against an empty/heading-free
body, so discovery code (e.g. a `list_ticket_templates` MCP tool) can show
a template's required sections without re-parsing anything itself:

```python
template.required_sections
# e.g. ["Summary", "Steps to Reproduce", "Expected Behavior"]
```

- **`kind="form"`**: the labels of fields that are `required=True` and not
  `type="markdown"` (markdown-type fields are never checked — see above),
  in field order.
- **`kind="markdown"`**: every `##`/`###` heading in the template's
  `raw_body`, in document order, de-duplicated (a heading repeated in the
  template is listed once).
- **`kind="workitem"`**: always `[]`, matching `validate_ticket_body`'s
  unconditional `[]` for this kind.

### Known limitation: multi-select dropdowns

GitHub issue-form dropdowns support `attributes.multiple: true`, letting a
contributor pick more than one option (GitHub renders the submitted answer
as a comma-separated list in that case). `list_issue_templates`/
`validate_ticket_body` don't parse or validate against `multiple` yet — a
`dropdown` field is always treated as single-select, so a multi-select
dropdown's rendered comma-separated answer will fail the `invalid-option`
membership check even when every selected item is a real option. This is a
deliberate, documented scope cut (not silent breakage): no crash, just a
known gap to close in a follow-up ticket if multi-select dropdowns turn up
in practice.

### Known limitation: Azure DevOps `default_team` fallback

Azure DevOps's `list_issue_templates` resolves the team-scoped URL via
`_team_scope(project)`, which falls back to the project name when
`ProjectConfig.default_team` is unset — this matches Azure DevOps's own
convention and is usually correct. But if a project's default team was
renamed away from the project name, that fallback produces a team-scoped
URL that 404s, and `list_issue_templates`'s existing "404 means no
templates" handling folds that straight into `[]` — indistinguishable from
a project that genuinely has no templates. Set `default_team` explicitly on
the project config whenever the project's default team isn't named after
the project itself, or `list_issue_templates` will silently return `[]`
instead of the real templates.

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

- Ticket #265: opt-in `light: bool = False` on the six write methods
  (`create_ticket`, `update_ticket`, `add_comment`, `create_pr`,
  `update_pr`, `merge_pr`) on all three providers. `light=True` returns a
  slim `TicketRef`/`CommentRef`/`PullRequestRef` sourced only from the
  write's own response, skipping every post-write reload/reread the
  full-object path otherwise performs (up to 4-6 extra HTTP round trips
  and ~0.35s of built-in sleeps on a PR merge or board-column move
  today). `light=False` is unchanged, byte-for-byte. See "Opting out of
  the post-write reload" above.
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
