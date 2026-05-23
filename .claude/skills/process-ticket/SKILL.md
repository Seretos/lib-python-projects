---
name: process-ticket
description: End-to-end automated ticket processing for lib-python-projects. Invoke with a ticket number (e.g. "process ticket #42", "implement issue 42", "work on ticket 42") when you are already inside a prepared git worktree on a feature branch. Orchestrates four subagents in sequence — context-extractor, planner, developer, reviewer — and ends with a pushed feature branch and an open draft Pull Request, plus traceability comments on the ticket. The skill itself never extracts context, plans, codes, or reviews; it delegates every phase to a subagent and manages the hand-offs (including a planner question-loop routed to the user via AskUserQuestion). Does NOT create the worktree or branch — the user prepares those beforehand.
---

# process-ticket — orchestrator

You orchestrate the processing of one ticket. You receive a **ticket number**
(e.g. `#42`) and drive the whole workflow **exclusively through subagents**
via the `Agent` tool. You do not read the ticket, write the plan, edit code,
or review yourself — each is a subagent's job. Your job is sequencing,
threading context between phases, handling the planner's questions, posting
traceability comments, and the final commit/push/draft-PR.

The project id for every project-issues MCP call in this repo is
**`lib-python-projects`**.

## Preconditions / guards (before anything else)

1. **Confirm the ticket number.** If the user didn't give one, ask.
2. **Branch guard.** Run `git rev-parse --abbrev-ref HEAD`. If it is `main`
   or `master`, STOP and tell the user this skill must run inside a prepared
   feature-branch worktree, which the user owns. Do not create a branch or
   worktree yourself. Proceed only on a non-default branch. Capture the
   branch name and the default branch (for the PR base).

## Phase sequence

Each subagent is a leaf (no further delegation) and cannot refetch context
it wasn't given. Thread each phase's output into the next phase's prompt.

### Phase 1 — context-extractor (read-only)
Spawn `context-extractor`. Pass: project id `lib-python-projects` and the
ticket number. It returns a distilled **context_summary** (problem,
acceptance criteria, constraints from comments, related tickets/PRs,
candidate affected modules). Capture it verbatim — downstream agents never
see the raw ticket.

### Phase 2 — planner (read-only, question-loop)
Spawn the planner **with a name** so you can resume it:
`Agent(name="planner-<ticket>", subagent_type="planner", prompt=…)`. Pass the
`context_summary` and the repo cwd.

The planner ends every reply with a status line as its LAST line:
- `STATUS: PLAN_FINAL` — no open questions.
- `STATUS: NEEDS_INPUT` — reply contains a numbered `## Open Questions`
  section (each question 2-4 options, one marked *(recommended)*).

Loop:
1. Read the status line.
2. `PLAN_FINAL` → capture full plan text as `plan`, exit loop.
3. `NEEDS_INPUT` → present each open question to the user via
   **AskUserQuestion** (options from the planner, recommended flagged).
   Collect answers, then **resume the same agent** with
   `SendMessage(name="planner-<ticket>", …)` carrying the answers keyed to
   question numbers. Back to step 1.

Never re-spawn a fresh planner inside the loop — always `SendMessage` the
named one so its context survives. If `NEEDS_INPUT` recurs more than ~4
times, surface it and ask whether to proceed with the recommended defaults.

**After PLAN_FINAL — post short plan comment.** Condense `plan` to a
short-form summary (goal + approach bullets + affected files; NOT every
detail) and post it to the ticket via
`add_comment(project_id="lib-python-projects", ticket_id=<#>, body=…)`.
Do not type `#ai-generated` — the MCP prepends it.

### Phase 3 — developer (Edit / Write / Bash)
Spawn `developer`. Pass the full `plan` and the `context_summary`. It
implements on the **current branch/worktree**, edits/writes files, and runs
`python -m pytest`. It returns a **change_report** (files touched, summary,
test result PASS/FAIL with failing test names). If it reports unfixable
failing tests, STOP and report to the user — do not push a broken branch.

### Phase 4 — reviewer (read-only)
Spawn `reviewer`. Pass the final `plan` and the developer's `change_report`;
instruct it to review the working-tree diff (`git diff` / `git diff
--staged`). It returns `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`
plus severity-tagged findings (`[blocking]` / `[nit]`).
- `CHANGES_REQUESTED` with blocking findings → re-spawn `developer` once with
  the findings appended to the plan, then re-run `reviewer` once. After one
  fix cycle, proceed and report any remaining non-blocking findings.
- `APPROVE` → proceed to the final step.

## Final step — commit, push, open draft PR, comment (orchestrator does this)

The orchestrator owns this (not the developer): it depends on the whole
pipeline's outcome (final plan + review verdict) and the branch/ticket the
orchestrator holds, and it keeps the developer's tool scope minimal.

1. **Commit** (raw git — no MCP for a local commit):
   `git add -A` then `git commit -m "<concise summary> (#<ticket>)"`.
2. **Push** the feature branch: `git push -u origin <branch>`.
3. **Open the PR as a draft via MCP** (MCP over CLI per the priority law):
   `create_pr(project_id="lib-python-projects", title=<from plan>,
   head=<branch>, base=<default branch>, draft=True,
   body=<summary + "Closes #<ticket>" + plan recap + review verdict>)`.
   Never type `#ai-generated` — the MCP prepends it.
4. **Comment on the ticket** linking the PR:
   `add_comment(project_id="lib-python-projects", ticket_id=<#>,
   body="Draft PR opened: <PR URL>. <one-line status>")`.
5. **Report to the user:** PR URL, branch, review verdict, test result.

## Hard rules
- **Delegate everything.** Never call `get_ticket`, `Edit`/`Write`, or review
  a diff yourself. Your tools: `Agent`/`SendMessage`, `AskUserQuestion`, the
  branch-guard git reads, the final commit/push git calls, and the
  project-issues write calls (`add_comment`, `create_pr`).
- **Subagents can't refetch.** Inline the summary/plan into each prompt.
- **Never run on main.** Enforce the branch guard up front.
- **Never create the worktree/branch.** The user owns that.
- **Push is authorized for this workflow only** (user-confirmed). PR opens as
  a **draft** so the user finalizes it.
