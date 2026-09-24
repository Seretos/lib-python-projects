# Changelog

All notable changes to `lib-python-projects` are documented here.

The version number itself is pipeline-stamped at release time (see
`release.yml`) — entries land under `## Unreleased` during development and
are never hand-labelled with a version string or tag.

## Unreleased

### Fixed

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
