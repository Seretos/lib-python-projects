# lib-python-projects — agent guide

A pure Python domain library (project-list model, provider abstraction for
GitHub/GitLab/Azure DevOps). This file tells any AI coding agent how to
operate in this repo. Keep it generic — behaviour lives in skills.

## Tool-priority law (read this first)

When you decide how to accomplish a step, always prefer the highest
available tier — this is a strict ordering:

1. **Skills first.** If a skill in `.claude/skills/` covers the task,
   invoke it. Skills encode the intended workflow and supersede ad-hoc
   approaches. Check for a matching skill before doing anything else.
2. **MCP second.** If no skill fits but a Model Context Protocol tool can
   do the job (ticket/PR operations, worktree lifecycle, …), use the MCP
   tool rather than shelling out. MCP calls are structured and
   permission-gated.
3. **Raw CLI / shell last.** Only drop to `git`, `gh`, `curl`, or manual
   shell when neither a skill nor an MCP exposes the capability (running
   tests, editing files, local git operations with no MCP equivalent).

Never reach for a lower tier when a higher tier can do the same thing. If
you find yourself scripting something a skill or MCP already provides,
stop and use the higher tier.

This ordering **explicitly overrides** the generic harness default that
says "prefer the dedicated file/search tools (Glob/Grep/Read)" — when a
skill or MCP covers the task, it wins. Concretely: any *"where is X defined
/ what does the code support / which Y exist / how does X work / find the
callers of X"* question is a code-understanding task → use the matching
skill first (e.g. the `serena-wrapper` symbol-aware tools), never raw
Glob/Grep/Read.

## Working on a ticket

To process a ticket end to end, invoke the **process-ticket** skill with
the ticket number. It orchestrates the full pipeline (context extraction →
planning → implementation → review → draft PR) through subagents. Do not
do those phases by hand on the main thread — let the skill drive them.

## Repo specifics (minimal by design)

- **Language:** Python, src-layout under `src/`, package `lib_python_projects`.
- **Tests:** `python -m pytest`. Install dev deps with
  `pip install -e ".[test]"`. Every test has a 60s timeout
  (`pytest-timeout`, thread-based so it also interrupts blocking socket
  I/O on Windows) — a legitimately slow test should get its timeout raised
  (e.g. `@pytest.mark.timeout(120)`), not have the timeout removed.
- **Branch discipline:** All feature work happens on a feature branch in a
  git worktree, never on `main`. Assume the worktree and branch already
  exist and that you are inside them.
- **AI attribution:** The project-issues MCP automatically prefixes every
  comment and PR body with `#ai-generated`. Never type that prefix yourself.

## Downstream dependency notifications

After every release, `release.yml` automatically opens a
`chore(deps): bump lib-python-projects to vX.Y.Z` issue in both
`Seretos/agent-project-issues` (via `PROJECTS_TICKET_TOKEN`) and
`Seretos/workboard` (via `WORKBOARD_TICKET_TOKEN`). Each consumer has its own
dedicated step; both are `continue-on-error: true` so a missing or invalid
token for one consumer never blocks the release or the other consumer. The
ticket body embeds the release's changelog verbatim under a
`### What changed` heading, positioned before `### Action required` — if the
changelog can't be fetched (or is empty), the step warns and substitutes a
link to the release page under the same heading, then still files the
ticket. Both filing steps and both board steps delegate to two local
composite actions (`.github/actions/file-consumer-ticket` and
`.github/actions/add-to-board`) shared across `release.yml` and
`ticket.yml`, so the enriched body and the board placement below are
defined once.

Immediately after each ticket step, a follow-up step adds that issue to the
`users/Seretos/projects/2` board via `gh project item-add`, reusing that same
consumer's ticket token (`PROJECTS_TICKET_TOKEN` or `WORKBOARD_TICKET_TOKEN`),
and sets its Status to **Backlog** (not Todo) — the project id, the Status
field id, and the Backlog option id are all resolved at runtime via `gh`,
never hardcoded, so bump tickets land ready for triage rather than already
in the active column. No separate board token is needed — each per-consumer
token is a classic PAT that also carries the `project` scope. The board-add
step is skipped cleanly if the ticket step produced no issue URL, and a
board-add failure (e.g. the Status field can't be resolved) only warns —
it never fails the ticket-filing job, since the ticket itself already
exists and is usable without a board placement.

**If the automatic step was skipped or failed**, re-file manually by running
the `open-dep-ticket` workflow (`.github/workflows/ticket.yml`) via
"Run workflow" in GitHub Actions. Supply:

- `version` — the semver string (no leading `v`), e.g. `0.2.0`.

The workflow files to both consumers automatically (no `consumers` input
needed). It is idempotent: it checks for an open issue with the exact same
title before creating one, so running it twice is safe.

**Human prerequisite — `PROJECTS_TICKET_TOKEN`:**
This must be a repository secret (Settings → Secrets → Actions) containing a
**classic PAT** (Settings → Developer settings → Personal access tokens →
**Tokens (classic)**) with the **`repo`** scope (covers Issues: write on
`Seretos/agent-project-issues`) and the **`project`** scope, so the follow-up
board-add step can reuse this same token. Fine-grained PATs cannot be used
here — they have no "Projects" permission at all, a hard GitHub platform
limitation. `GITHUB_TOKEN` cannot open cross-repo issues. Creating/rotating
this token is a human task that must be done once before the first release.

**Human prerequisite — `WORKBOARD_TICKET_TOKEN`:**
This must be a repository secret (Settings → Secrets → Actions) containing a
**classic PAT** with the **`repo`** scope (covers Issues: write on
`Seretos/workboard`) and the **`project`** scope, so the follow-up board-add
step can reuse this same token. Fine-grained PATs cannot be used here — they
have no "Projects" permission at all. `GITHUB_TOKEN` cannot open cross-repo
issues. Creating/rotating this token is a human task that must be done once
before the first release.

Without the `project` scope on these tokens, the board-add step is silently
skipped (`continue-on-error`) and the ticket still opens normally — it just
won't appear on board `2`.

**`ci/` package invariants.** Both composite actions' logic lives in the
plain-source `ci/` package at the repo root (`ci/gh.py`, `ci/actions_io.py`,
`ci/bump_ticket.py`, `ci/board.py` — not installed as part of the
distribution; each action's `run:` line invokes it as `python3 -m
ci.<module>`). This replaced an earlier bash implementation that shipped six
review-round bugs in a row (silent `jq`/`gh api --jq` misuse, CLI-side
filtering swallowing real failures, an undeclared `jq` runtime dependency,
pagination that silently truncated past the default page, and a board write
that could land an item outside Backlog on a partial failure). `ci/` closes
that whole bug class *structurally*, not by patching each one after the
fact — a future change here must preserve these invariants (enforced by
`tests/test_ci_gh_discipline.py`, which source-scans every module under
`ci/`):

- every `gh` invocation goes through the single choke point in `ci/gh.py`
  (`run_gh`, `gh_json`, `gh_paginate_rest`) — no other module spawns a
  process directly;
- no CLI-side filtering/paging flags anywhere (`gh`'s own query-filter,
  quiet-JSON, templating, or automatic-pagination flags) — every response is
  parsed as JSON in Python instead, and REST pagination is followed
  explicitly, one page at a time, via `gh_paginate_rest`;
- no shell execution, and no external `jq` (or any other) runtime
  dependency — standard library only, no third-party imports anywhere under
  `ci/`;
- board writes are resolve-then-mutate: `ci/board.py`'s
  `resolve_backlog_target` fully resolves the project id, the Status field
  id, and the Backlog option id (read-only calls only) *before*
  `place_in_backlog` ever runs the first mutating call — a resolution
  failure means the item is never added to the board at all, never added
  and then stranded outside Backlog.
