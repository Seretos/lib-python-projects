---
name: orchestrate-tickets
description: Fleet orchestrator for lib-python-projects — turns open tickets into parallel, conflict-free background work. Invoke to dispatch ticket work (e.g. "orchestrate tickets", "orchestrate the open tickets", "work all open tickets in parallel", "orchestrate ticket 7"). With ONE ticket given it goes straight to creating a worktree and launching its instance. With none (all open) or several, it spawns the conflict-analyst subagent to find the maximal set of tickets that can run in parallel without their PRs conflicting, then creates one worktree per selected ticket and starts a background + remote-control Claude instance per worktree — idle, with no boot prompt. Plugin MCPs don't auto-load in a fresh worktree session (anthropics/claude-code#61866), so the user runs `/reload-plugins` in each session and then drives `process-ticket #<n>` there themselves. The skill creates the worktrees and starts the sessions; it does NOT prompt them, implement, push, or merge.
---

# orchestrate-tickets — fleet orchestrator

You dispatch ticket work across parallel, isolated worktrees. You decide *what*
runs (via the `conflict-analyst` subagent), create the worktrees, and start one
background + remote-control Claude instance per ticket — **idle, with no boot
prompt**. You implement nothing. The user drives each session: a fresh worktree
session does not have the plugin MCPs loaded yet (anthropics/claude-code#61866),
so they run `/reload-plugins` first, then `process ticket #<n>` (the
`process-ticket` skill: context → plan → code → review → draft-PR).

The project id for project-issues calls is **`lib-python-projects`**.

## Inputs

- An optional ticket number, or several, or nothing.
  - **exactly one** ticket → SINGLE mode (skip analysis).
  - **none** → MULTI mode over **all open** tickets.
  - **several** → MULTI mode over **that subset**.

## Preconditions

1. **Run only from the main checkout — never inside a worktree.** This skill is
   the mirror of `process-ticket` (which runs only *inside* a worktree on a
   feature branch). Guard before doing anything else:
   - `git rev-parse --abbrev-ref HEAD` must be the default branch (`main`).
   - `git rev-parse --git-dir` must EQUAL `git rev-parse --git-common-dir`
     (they differ when you are inside a linked worktree).
   If either check fails, **STOP** and tell the user to run orchestrate-tickets
   from the main `lib-python-projects` checkout — otherwise the worktrees it
   spawns and the orchestrator's own branch/state collide with the workers.
2. **Capture repo + base branch.** `git rev-parse --show-toplevel` → `repo_root`.
   Determine the default branch (`main` here) → `base`. All worktrees branch off
   `base`.
3. **Worktree mechanism.** Worktree creation uses the **agent-worktree MCP**
   (`worktree_create`). If that MCP is not available in this session, fall back to
   raw git: `git worktree add -b <branch> <path> <base>` with `<path>` under a
   sibling worktree-store dir. Confirm which one is available before Phase B.

## Phase A — decide the target tickets

**SINGLE mode** (one ticket `#n`): do **not** spawn the analyst. Fetch only the
title for the branch slug — `get_ticket(project_id, n, include_comments=False,
include_relations=False)` — and form `branch = fix/<n>-<slug>` (title
lower-cased, non-alphanumerics → hyphens, ~4 words). The target set is just
`[{ticket: n, branch}]`.

**MULTI mode** (none, or several): spawn the analyst —
`Agent(subagent_type="conflict-analyst", prompt=…)` — passing `project_id` and
either "all open" or the explicit subset. It returns a readable summary and a
trailing fenced ```json block with `parallel` and `deferred` arrays. Parse the
**json block only**; the target set is `parallel`.

## Phase B — confirm, then create worktrees

1. **Confirm before launching.** Present the planned fleet to the user via
   **AskUserQuestion**: the tickets that will run in parallel (with branch +
   footprint), and the deferred ones with their collision reason. Launching N
   background `--dangerously-skip-permissions` sessions is heavy and
   hard-to-undo, so get a go-ahead (or let the user drop/keep tickets) first.
   For SINGLE mode keep it light, but still confirm the one launch.
2. **Create one worktree per selected ticket, SEQUENTIALLY.** Never in parallel —
   concurrent `git worktree` ops on one repo race on the index lock.
   `worktree_create(repo_root, branch=<branch>, base=<base>)`. Capture the
   returned `path` for each.

## Phase C — launch one instance per worktree

For each created worktree, **sequentially**, start a background + remote-control
Claude instance whose working directory is the worktree (so the user's later
`process-ticket` run passes its branch guard — it must run on a feature branch,
never on `main`). Start it **idle — no boot prompt**: a freshly-spawned worktree
session does not have the plugin MCPs loaded, so a boot prompt would fail before
the user can `/reload-plugins` (see anthropics/claude-code#61866). On
Windows/PowerShell:

```powershell
Push-Location '<worktree-path>'
claude --allow-dangerously-skip-permissions --verbose --rc "<branch>" --bg
Pop-Location
```

- `--rc "<branch>"` names the remote-control session after the branch; `--bg`
  detaches it under the daemon. **No trailing prompt** — the session waits idle.
- **Capture the `backgrounded · <job-id>` line** the launch prints — that
  `<job-id>` is the handle for `claude attach <job-id>` / `claude logs <job-id>`
  / `claude stop <job-id>`.
- Do **not** open a terminal and do **not** `claude attach` — the user attaches
  when ready.

## Phase D — report

Print one table: `ticket · branch · worktree path · bg job-id`. Then spell out
the **per-session next steps the user must do** (the skill can't — see Phase C):
for each session, `claude attach <job-id>`, run **`/reload-plugins`** (so the
worktree's plugin MCPs load), then `process ticket #<n>`. Also list the deferred
tickets (what still needs a later, sequential pass) and the stop hint
(`claude stop <job-id>`). Then stop — you've started the fleet; the user drives
it from here.

## Hard rules

- **Delegate the analysis.** In MULTI mode the `conflict-analyst` decides the
  set; you never compute footprints yourself. In SINGLE mode you only read the
  title for a slug — no footprint analysis.
- **Conflict-free = disjoint file footprints** (the analyst's contract). Tickets
  that share a source file are never launched together; they go to `deferred`
  for a later run.
- **Sequential, not parallel**, for both worktree creation and instance launch
  (git index lock + bg-session registration races).
- **Each instance runs in its own worktree on its feature branch.** Never launch
  an instance with cwd on `main`.
- **You start sessions idle; you don't prompt, implement, push, or merge.** The
  user runs `/reload-plugins` then `process-ticket` in each session — that owns
  implementation and its own draft PR. You stop after launching + reporting.
- **`/reload-plugins` is mandatory in each spawned session before `process-ticket`** —
  fresh worktree sessions don't auto-load the plugin MCPs
  (anthropics/claude-code#61866). That's why sessions start idle, not with a
  `process-ticket` boot prompt.
- **Stopping an instance is `claude stop <job-id>`** — never force-kill the
  process (the daemon respawns `--bg` sessions from their job record; the job-id
  is the original short id, not the rotating sessionId from `claude agents`).
- **Lane separation (load-bearing).** orchestrate-tickets runs **only** from the
  main checkout; `process-ticket` runs **only** inside a worktree on a feature
  branch. Never invoke orchestrate-tickets from a worktree, and never run
  `process-ticket` on `main`. Each guards its own lane (see Preconditions here,
  and process-ticket's branch+worktree guard) so the orchestrator and the
  workers can't step on each other.
