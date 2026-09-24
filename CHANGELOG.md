# Changelog

All notable changes to `lib-python-projects` are documented here.

The version number itself is pipeline-stamped at release time (see
`release.yml`) — entries land under `## Unreleased` during development and
are never hand-labelled with a version string or tag.

## Unreleased

### Fixed

- Ticket #287: `GitLabProvider.list_comments(since=...)` now matches a
  note's last-update time (`updated_at`, falling back to `created_at`),
  the same semantics as GitHub's `?since=`, instead of only its creation
  time — an edited comment is no longer silently missed. The unreliable
  `created_after` server hint is no longer sent at all; filtering is
  entirely client-side.
- Ticket #287: `GitLabProvider.list_pr_files` now derives real
  `additions`/`deletions` by counting `+`/`-` lines in each file's diff
  hunks, instead of always returning `null`/`null`. They stay `None`
  only when GitLab sends no diff text at all (e.g. an oversized or
  binary file).
- Ticket #287: a rejected GitLab `parent`/`child` relation add (a
  work-item type pair GitLab's hierarchy rules don't allow) now raises
  `GitLabError(422, ...)` naming both items' work-item types and the
  type-pair precondition, keeping GitLab's original error text, instead
  of a bare, cryptic 422 message.
- Ticket #272: the ETag cache no longer replays stale pagination headers
  (`Link`, `X-Total-Pages`, `X-Next-Page`) on a 304. GETs carrying `page` or
  `per_page` now bypass the conditional cache entirely, so
  `list_comments(order="desc")` and `has_more` reflect the current server
  state. GitLab's descending `has_more` now also accounts for older notes
  trimmed by the `limit` slice, and the conditional rebuild preserves
  `request.extensions` (so cached GETs keep the client's timeout).

### Changed

- Ticket #283: bumped the `lib-python-config` pin to the exact tag
  `v0.1.3` (upstream change is CI/tooling-only,
  Seretos/lib-python-config#17; no consumer-side API change).
- Ticket #271: `GitHubProvider.merge_pr`'s 405 error no longer tells the
  caller to "rebase or resolve conflicts" unconditionally. A draft PR now
  gets a message naming the draft and suggesting `update_pr(draft=false)`;
  only `mergeable_state='dirty'` keeps the conflict advice; any other
  state points at `mergeable_state` as the blocking condition.
- Tickets #266/#268: pinned `lib-python-config` in `pyproject.toml` to the
  exact tag `v0.1.2`, replacing the floating `release/0.x` branch pin.
  `lib-python-config`'s own release workflow force-pushes `release/0.x` on
  every release, so a clean install could previously resolve to a
  different, never-reviewed config commit with no diff/PR in this repo. A
  config release now reaches this lib only via an explicit, reviewed pin
  bump.

### Added

- Ticket #285: `Board.binding` is now optional (`None` by default), so a
  `projects.yml` entry can configure `board.columns` — plus optional
  `label_map` (logical column -> label name) and `closed_column` (the
  logical column mapping to the ticket's native closed state) — without
  a live provider board binding. This "label mode" lets a label-only
  consumer (GitLab, or GitHub without a Projects v2 board) configure a
  board at all, instead of the whole project entry being dropped into
  `invalid_projects`. `label_map`/`closed_column` are validated against
  `columns` the same way `binding.map` is; both are inert data stored on
  `Board`, not resolved by this library (agent-project-issues#365).
  Every provider path that requires a live board binding still raises
  its existing `ValueError` for a label-mode project instead of an
  `AttributeError`; a project with an existing `binding` is unaffected.

- Ticket #275: `wait_for_pipeline(project, token, sha, *, timeout_s,
  poll_interval_s)` on the GitHub, GitLab and Azure DevOps providers (shared
  `PipelineWaitProvider` mixin). Blocks until the commit's CI reaches a
  verdict and returns `PipelineWaitResult(state, runs, waited_s)` with state
  `success` / `failure` / `pending` / `no_verdict` (cancelled or skipped, never
  `failure`) / `no_runs`.

- Ticket #265: an opt-in `light: bool = False` keyword-only parameter on
  the six provider write methods — `create_ticket`, `update_ticket`,
  `add_comment`, `create_pr`, `update_pr`, `merge_pr` — across all three
  providers (GitHub, GitLab, Azure DevOps). `light=True` skips every
  request whose sole purpose is enriching the return value (a post-write
  poll/re-GET, a reviews/approvals/votes re-fetch, GitHub's
  `update_ticket` board read-back, ...) and returns a slim
  `TicketRef` / `CommentRef` / `PullRequestRef` (new dataclasses in
  `providers/base.py`) built only from the write call's own response —
  cutting the 4-6 extra HTTP round trips and ~0.35s of built-in sleeps a
  PR merge or board-column move previously cost after the write had
  already landed. `light=False` (the default) is unchanged, byte-for-byte,
  down to the requests it sends on the wire — every existing caller keeps
  working exactly as before. Idempotency replay (`idempotency_key=`)
  composes with `light` via the new `resolve_replay` helper: a
  `light=True` retry of a previously-used key replays the stored ref with
  no new request; a `light=False` retry of a key whose original create
  used `light=True` reloads once and returns the full model. See the
  README's "Opting out of the post-write reload" section for the full
  contract, and each write method's docstring for its own `Light mode
  (`light=True`)` block naming exactly which fields it populates and
  which it leaves `None` (and why).
