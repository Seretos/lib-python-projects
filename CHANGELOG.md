# Changelog

All notable changes to `lib-python-projects` are documented here.

The version number itself is pipeline-stamped at release time (see
`release.yml`) — entries land under `## Unreleased` during development and
are never hand-labelled with a version string or tag.

## Unreleased

### Added

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
