"""Places an issue into the Backlog column of a user-owned Projects v2
board. The project id, the Status field id, and the Backlog option id are
all resolved at runtime via read-only ``gh`` calls -- never hardcoded (see
``tests/test_board_backlog.py``'s static guard) -- and resolution completes
*fully* before the first mutating call (``gh project item-add``), so a
resolution failure never leaves an item added to the board and stranded
outside Backlog (#243 round 5).

Entry point: :func:`run` (``ci.board.run(env) -> int``), reading
``ISSUE_URL``, ``BOARD_OWNER``, ``BOARD_NUMBER``, ``GH_TOKEN`` from the
passed-in mapping -- see ``REQUIRED_ENV``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import ci.actions_io as actions_io
import ci.gh as gh

REQUIRED_ENV = ("ISSUE_URL", "BOARD_OWNER", "BOARD_NUMBER", "GH_TOKEN")

_STATUS_FIELD_NAME = "Status"
_BACKLOG_OPTION_NAME = "Backlog"

# `gh project field-list`'s own documented default page size when --limit
# isn't passed. Used only to recognise a *suspected* truncation when the
# response doesn't even report totalCount (see _fetch_all_fields).
_DEFAULT_FIELD_LIST_LIMIT = 30


class BoardResolutionError(actions_io.ScriptError):
    """Raised when the project id, the Status field, or its Backlog option
    cannot be resolved. Each failure mode below carries a distinct message
    (see the driving tests: a Status-field-missing failure must never be
    confused with, or masked by, a Backlog-option-missing one)."""


@dataclass(frozen=True)
class BacklogTarget:
    """Everything :func:`place_in_backlog` needs, fully resolved. Requiring
    this as an argument (rather than owner/number plus loose ids) makes it
    structurally impossible to call ``place_in_backlog`` before resolution
    has actually succeeded."""

    owner: str
    number: str
    project_id: str
    status_field_id: str
    backlog_option_id: str


def _fetch_project_id(owner: str, number: str) -> str:
    payload = gh.gh_json(["project", "view", number, "--owner", owner, "--format", "json"])
    project_id = payload.get("id") if isinstance(payload, dict) else None
    if not project_id:
        raise BoardResolutionError(
            f"Could not resolve the project id for project {number} (owner {owner})."
        )
    return project_id


def _fetch_all_fields(owner: str, number: str) -> list[dict]:
    """Fetch every field on the board, re-requesting with a bigger
    ``--limit`` if the first response's own ``totalCount`` says the default
    page was truncated. If ``totalCount`` isn't reported at all *and* the
    response looks like it hit the default page size, truncation can't be
    ruled out -- raise rather than silently resolving against a possibly
    incomplete field list (#243 round 4, generalised)."""
    payload = gh.gh_json(["project", "field-list", number, "--owner", owner, "--format", "json"])
    fields = payload.get("fields") or [] if isinstance(payload, dict) else []
    total_count = payload.get("totalCount") if isinstance(payload, dict) else None

    if total_count is None:
        if len(fields) >= _DEFAULT_FIELD_LIST_LIMIT:
            raise BoardResolutionError(
                "field-list result may be truncated and totalCount is unavailable -- cannot "
                f"safely resolve fields for project {number} (owner {owner})."
            )
        return fields

    if len(fields) < total_count:
        payload = gh.gh_json(
            ["project", "field-list", number, "--owner", owner, "--limit", str(total_count), "--format", "json"]
        )
        fields = payload.get("fields") or [] if isinstance(payload, dict) else []

    return fields


def resolve_backlog_target(owner: str, number: str) -> BacklogTarget:
    """Read-only resolution of everything a subsequent
    :func:`place_in_backlog` call needs. Never mutates the board."""
    project_id = _fetch_project_id(owner, number)
    fields = _fetch_all_fields(owner, number)

    status_field = next((f for f in fields if f.get("name") == _STATUS_FIELD_NAME), None)
    if status_field is None:
        raise BoardResolutionError(
            f"Could not resolve the Status field on project {number} (owner {owner})."
        )

    backlog_option = next(
        (o for o in (status_field.get("options") or []) if o.get("name") == _BACKLOG_OPTION_NAME),
        None,
    )
    if backlog_option is None:
        raise BoardResolutionError(
            f"Could not resolve the Backlog option on project {number} (owner {owner})."
        )

    return BacklogTarget(
        owner=owner,
        number=number,
        project_id=project_id,
        status_field_id=status_field["id"],
        backlog_option_id=backlog_option["id"],
    )


def place_in_backlog(target: BacklogTarget, issue_url: str) -> None:
    """Add ``issue_url`` to the board and set its Status to Backlog, using
    only ids carried by the already-resolved ``target`` -- the field-id /
    option-id pair is wired together from the same resolution pass, so a
    mismatched pairing (wrong field, wrong option) structurally cannot
    happen here.

    These are still two separate mutating ``gh`` calls, so a failure of the
    second one (transient API hiccup, or the Status field/Backlog option
    getting renamed/deleted in the moment between resolution and this call)
    is a real, distinct outcome from either call never happening: the item
    genuinely IS on the board at that point, just outside Backlog. That is
    caught below and re-raised with a message that says so explicitly, so an
    operator reading the error knows to go check the board rather than
    assuming nothing happened (#243 round 2 blocking finding 1+2).

    No compensating rollback (e.g. deleting the stray item via `gh project
    item-delete-item`) is attempted here, by design: a delete call can itself
    fail for the same transient reasons, which would only trade one
    ambiguous state for another (did the delete work?) while adding a third
    `gh` call's worth of failure modes to reason about. The distinguishing
    error message is what actually matters for an operator to act on; a
    blind retry-without-rollback would still need this same message on
    eventual failure, so it is simpler to stop here and let a human (or a
    future workflow re-run, which is safe -- item-add against an
    already-present item is not expected to duplicate it) resolve it."""
    payload = gh.gh_json(
        ["project", "item-add", target.number, "--owner", target.owner, "--url", issue_url, "--format", "json"]
    )
    item_id = payload.get("id") if isinstance(payload, dict) else None
    if not item_id:
        raise BoardResolutionError(
            f"Failed to add {issue_url} to project {target.number} (owner {target.owner})."
        )

    try:
        gh.run_gh(
            [
                "project",
                "item-edit",
                "--id",
                item_id,
                "--project-id",
                target.project_id,
                "--field-id",
                target.status_field_id,
                "--single-select-option-id",
                target.backlog_option_id,
            ]
        )
    except gh.GhError as exc:
        raise BoardResolutionError(
            f"Added {issue_url} to project {target.number} (owner {target.owner}) as item "
            f"{item_id}, but failed to set its Status to Backlog: {exc}. The item IS now on "
            "the board, outside Backlog -- check it manually."
        ) from exc


def run(env: dict[str, str]) -> int:
    issue_url = env["ISSUE_URL"]
    owner = env["BOARD_OWNER"]
    number = env["BOARD_NUMBER"]

    try:
        target = resolve_backlog_target(owner, number)
        place_in_backlog(target, issue_url)
    except (gh.GhError, actions_io.ScriptError) as exc:
        actions_io.error(str(exc))
        return 1

    return 0


def main() -> int:
    return actions_io.run_main(lambda: run(dict(os.environ)))


if __name__ == "__main__":
    sys.exit(main())
