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

After every release, `release.yml` calls the central
`seretos-agents/modular-software-factory-dev/.github/actions/notify-consumers@main` action
(deliberately unpinned to `@main` so fixes propagate to every lib) in one
step, fed only facts — `version`, `source_repo`,
the newline-separated `consumers` list (`seretos-agents/agent-project-issues`,
`Seretos/workboard`, `seretos-agents/ecosystem-statistics`), and
`gh_token: ${{ secrets.ECOSYSTEM_TOKEN }}`. The central action owns labels,
changelog embedding and board placement for all consumers; this lib keeps
no per-consumer ticket or board logic of its own. A failure in that step
fails the release run itself (no
`continue-on-error`) — silently missing bump tickets is worse than a red
run.

**Human prerequisite — `ECOSYSTEM_TOKEN`:**
This must be a repository secret (Settings → Secrets → Actions) containing a
**classic PAT** (Settings → Developer settings → Personal access tokens →
**Tokens (classic)**) with the **`repo`** scope (covers Issues: write on
both consumer repos) and the **`project`** scope (board placement).
Fine-grained PATs cannot be used here — they have no "Projects" permission
at all, a hard GitHub platform limitation. `GITHUB_TOKEN` cannot open
cross-repo issues. Creating/rotating this token is a human task that must
be done once before the first release.

**`ci/` package invariants.** `ci/gh.py`, `ci/actions_io.py` and
`ci/prev_tag.py` are plain-source modules at the repo root (not installed as
part of the distribution). This replaced an earlier bash implementation that
shipped six review-round bugs in a row (silent `jq`/`gh api --jq` misuse,
CLI-side filtering swallowing real failures, an undeclared `jq` runtime
dependency, and pagination that silently truncated past the default page).
`ci/` closes that whole bug class *structurally*, not by patching each one
after the fact — a future change here must preserve these invariants
(enforced by `tests/test_ci_gh_discipline.py`, which source-scans every
module under `ci/`):

- every `gh` invocation goes through the single choke point in `ci/gh.py`
  (`run_gh`, `gh_json`, `gh_paginate_rest`) — no other module spawns a
  process directly;
- no CLI-side filtering/paging flags anywhere (`gh`'s own query-filter,
  quiet-JSON, templating, or automatic-pagination flags) — every response is
  parsed as JSON in Python instead, and REST pagination is followed
  explicitly, one page at a time, via `gh_paginate_rest`;
- no shell execution, and no external `jq` (or any other) runtime
  dependency — standard library only, no third-party imports anywhere under
  `ci/`.
