"""Driving test for R5 (#243): both workflows must call the two new local
composite actions instead of inlining `gh issue create` / `gh project
item-add`, and `ticket.yml` must checkout the repo before any local
`uses:` step (a local composite action reference needs the repo checked
out to resolve `./.github/actions/...`).

Pure YAML-parsing test -- no bash execution needed. Uses
`ruamel.yaml.YAML(typ="safe")`, already a runtime dependency of this
package (see pyproject.toml's `ruamel.yaml>=0.18`).

RED today: `release.yml` and `ticket.yml` still inline every `gh` call
directly in `run:` blocks and reference neither composite action, so the
step-count assertions fail (0 found, not 2) for a genuine "the wiring is
missing" reason -- not a parsing error.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
TICKET_YML = REPO_ROOT / ".github" / "workflows" / "ticket.yml"
FILE_TICKET_ACTION_YML = REPO_ROOT / ".github" / "actions" / "file-consumer-ticket" / "action.yml"
ADD_TO_BOARD_ACTION_YML = REPO_ROOT / ".github" / "actions" / "add-to-board" / "action.yml"

FILE_TICKET_USES = "./.github/actions/file-consumer-ticket"
ADD_TO_BOARD_USES = "./.github/actions/add-to-board"


def _load(path: Path) -> dict:
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as f:
        return yaml.load(f)


def _all_steps(workflow: dict) -> list[dict]:
    steps: list[dict] = []
    # PyYAML/ruamel parse the `on:` key as boolean True under typ="safe"
    # in some configs, but jobs stays a plain string key regardless.
    for job in workflow.get("jobs", {}).values():
        steps.extend(job.get("steps", []))
    return steps


def _count_uses(steps: list[dict], uses_value: str) -> int:
    return sum(1 for s in steps if s.get("uses") == uses_value)


def _steps_with_inline_gh_calls(steps: list[dict]) -> list[str]:
    offenders = []
    for step in steps:
        run_block = step.get("run")
        if not run_block:
            continue
        if "gh issue create" in run_block or "gh project item-add" in run_block:
            offenders.append(step.get("name", "<unnamed step>"))
    return offenders


def _declared_inputs(action_yml: Path) -> set[str]:
    yaml = YAML(typ="safe")
    with action_yml.open("r", encoding="utf-8") as f:
        action = yaml.load(f)
    return set((action.get("inputs") or {}).keys())


def test_all_four_call_sites_use_the_composite_actions():
    release_wf = _load(RELEASE_YML)
    ticket_wf = _load(TICKET_YML)

    file_ticket_inputs = _declared_inputs(FILE_TICKET_ACTION_YML)
    add_to_board_inputs = _declared_inputs(ADD_TO_BOARD_ACTION_YML)
    assert file_ticket_inputs, f"no inputs declared in {FILE_TICKET_ACTION_YML}"
    assert add_to_board_inputs, f"no inputs declared in {ADD_TO_BOARD_ACTION_YML}"

    for label, workflow in (("release.yml", release_wf), ("ticket.yml", ticket_wf)):
        steps = _all_steps(workflow)

        file_ticket_steps = [s for s in steps if s.get("uses") == FILE_TICKET_USES]
        assert len(file_ticket_steps) == 2, (
            f"{label}: expected exactly 2 steps using '{FILE_TICKET_USES}', "
            f"found {len(file_ticket_steps)}"
        )

        add_to_board_steps = [s for s in steps if s.get("uses") == ADD_TO_BOARD_USES]
        assert len(add_to_board_steps) == 2, (
            f"{label}: expected exactly 2 steps using '{ADD_TO_BOARD_USES}', "
            f"found {len(add_to_board_steps)}"
        )

        # R5 hardening: each call site's `with:` block must actually supply
        # every input the composite action declares as required -- parsing
        # `with:` keys against the action.yml `inputs:` contract, not just
        # trusting the `uses:` reference is enough. A mismatched
        # action.yml/script contract (e.g. renamed input the caller never
        # updated) would otherwise pass silently.
        for step in file_ticket_steps:
            with_keys = set((step.get("with") or {}).keys())
            missing = file_ticket_inputs - with_keys
            assert not missing, (
                f"{label}: step {step.get('name')!r} using '{FILE_TICKET_USES}' is "
                f"missing with: keys for declared inputs {missing}"
            )
        for step in add_to_board_steps:
            with_keys = set((step.get("with") or {}).keys())
            missing = add_to_board_inputs - with_keys
            assert not missing, (
                f"{label}: step {step.get('name')!r} using '{ADD_TO_BOARD_USES}' is "
                f"missing with: keys for declared inputs {missing}"
            )

        offenders = _steps_with_inline_gh_calls(steps)
        assert offenders == [], (
            f"{label}: these steps still inline 'gh issue create' or "
            f"'gh project item-add' in a run: block instead of delegating "
            f"to the composite action: {offenders}"
        )


def _step_env_keys(action_yml: Path) -> set[str]:
    yaml = YAML(typ="safe")
    with action_yml.open("r", encoding="utf-8") as f:
        action = yaml.load(f)
    steps = (action.get("runs") or {}).get("steps") or []
    assert steps, f"no steps found in {action_yml}"
    return set((steps[0].get("env") or {}).keys())


def test_action_env_block_matches_module_required_env():
    """#243 generation 2, R5: the composite action's own ``env:`` block is
    the contract the ``ci.*`` module's ``run(env)`` actually reads from --
    keep them in lockstep so a renamed/added/removed input on one side
    can't silently drift from the other. ``$GITHUB_OUTPUT`` is deliberately
    excluded from ``REQUIRED_ENV``: it's provided ambiently by the GitHub
    Actions runner to every step, not declared in a composite action's own
    ``env:`` block."""
    import ci.board
    import ci.bump_ticket

    file_ticket_env = _step_env_keys(FILE_TICKET_ACTION_YML)
    add_to_board_env = _step_env_keys(ADD_TO_BOARD_ACTION_YML)

    assert file_ticket_env == set(ci.bump_ticket.REQUIRED_ENV), (
        f"file-consumer-ticket/action.yml env: keys {file_ticket_env} must match "
        f"ci.bump_ticket.REQUIRED_ENV {set(ci.bump_ticket.REQUIRED_ENV)}"
    )
    assert add_to_board_env == set(ci.board.REQUIRED_ENV), (
        f"add-to-board/action.yml env: keys {add_to_board_env} must match "
        f"ci.board.REQUIRED_ENV {set(ci.board.REQUIRED_ENV)}"
    )


def test_ticket_workflow_checks_out_repo_before_any_local_composite_action():
    ticket_wf = _load(TICKET_YML)
    steps = _all_steps(ticket_wf)

    checkout_index = None
    first_local_uses_index = None
    for i, step in enumerate(steps):
        uses = step.get("uses")
        if uses is None:
            continue
        if uses.startswith("actions/checkout@") and checkout_index is None:
            checkout_index = i
        if uses.startswith("./.github/actions/") and first_local_uses_index is None:
            first_local_uses_index = i

    assert checkout_index is not None, (
        "ticket.yml must have an actions/checkout@v4 step -- local composite "
        "actions under ./.github/actions/... cannot resolve without the repo "
        "checked out first"
    )
    if first_local_uses_index is not None:
        assert checkout_index < first_local_uses_index, (
            "actions/checkout must run before the first local composite-action step"
        )
