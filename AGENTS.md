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
