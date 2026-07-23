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
  `pip install -e ".[test]"`.
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
token for one consumer never blocks the release or the other consumer.
Immediately after each ticket step, a follow-up step adds that issue to the
`users/Seretos/projects/2` board via `gh project item-add`, reusing that same
consumer's ticket token (`PROJECTS_TICKET_TOKEN` or `WORKBOARD_TICKET_TOKEN`),
so bump tickets show up on the board without manual triage. No separate board
token is needed — each per-consumer token is a classic PAT that also carries
the `project` scope. The board-add step is skipped cleanly if the ticket step
produced no issue URL.

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
