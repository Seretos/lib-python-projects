"""Driving tests for R3 (#243 generation 2, bundling #228's board-Backlog
requirement) -- the not-yet-existing ``ci.board`` module, replacing
``.github/actions/add-to-board/add-to-board.sh``.

Entry-point API decision (mirrors ``ci.bump_ticket``, see that module's
test file for the fuller rationale): ``ci.board.run(env: dict[str, str]) ->
int``, reading ``ISSUE_URL``, ``BOARD_OWNER``, ``BOARD_NUMBER``,
``GH_TOKEN`` from the passed-in mapping. Internally it is expected to
implement the plan's ``resolve_backlog_target`` / ``place_in_backlog``
split (read-only resolution fully before the first mutating `gh` call) --
tests assert on that *outcome* (state after the run), not on call order,
per the plan's design: "a wrong flag->value pairing simply does not
produce Backlog".

RED reason for every test that calls ``ci.board.run``: ``ci.board`` (and,
transitively, ``ci.gh``) do not exist yet in this dispatch --
``ModuleNotFoundError`` at fixture setup, imported lazily so only the
tests that need it fail, not the whole file.

Do not implement ``ci/`` in this dispatch; that is a separate
(``phase=implement``) dispatch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.fake_gh import FakeGitHub

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_MODULE = REPO_ROOT / "ci" / "board.py"
ADD_TO_BOARD_ACTION_YML = REPO_ROOT / ".github" / "actions" / "add-to-board" / "action.yml"
FILE_TICKET_ACTION_YML = REPO_ROOT / ".github" / "actions" / "file-consumer-ticket" / "action.yml"

ISSUE_URL = "https://github.com/Seretos/agent-project-issues/issues/42"
BOARD_OWNER = "Seretos"
BOARD_NUMBER = "2"

_HARDCODED_ID_PATTERN = re.compile(r"PVT[A-Za-z]*_[A-Za-z0-9]+")


@pytest.fixture
def fake(monkeypatch):
    """A fresh FakeGitHub wired in as ci.gh.run_gh. RED via
    ModuleNotFoundError: No module named 'ci' until ci.gh exists."""
    import ci.gh  # noqa: PLC0415 -- deliberately lazy, see module docstring

    sim = FakeGitHub()
    monkeypatch.setattr(ci.gh, "run_gh", sim.run_gh)
    return sim


def _base_env() -> dict[str, str]:
    return {
        "ISSUE_URL": ISSUE_URL,
        "BOARD_OWNER": BOARD_OWNER,
        "BOARD_NUMBER": BOARD_NUMBER,
        "GH_TOKEN": "fake-token",
    }


def _run(env: dict[str, str]) -> int:
    import ci.board  # noqa: PLC0415

    return ci.board.run(env)


def _default_fields(*, include_backlog: bool = True) -> list[dict]:
    options = [{"id": "todo_opt", "name": "Todo"}]
    if include_backlog:
        options.append({"id": "backlog_opt", "name": "Backlog"})
    options.append({"id": "inprogress_opt", "name": "In progress"})
    return [{"id": "PVTSSF_status", "name": "Status", "options": options}]


# ---------------------------------------------------------------------
# R3 driving test
# ---------------------------------------------------------------------


def test_issue_ends_up_in_backlog_on_the_board(fake):
    fake.set_fields(_default_fields())
    env = _base_env()

    exit_code = _run(env)

    assert exit_code == 0
    assert fake.board_item_status(ISSUE_URL) == "Backlog"


# ---------------------------------------------------------------------
# Round-5 regression, as an outcome: resolution failure must leave the
# item never added at all, not added-then-stranded.
# ---------------------------------------------------------------------


def test_backlog_option_missing_item_never_added(fake, capsys):
    fake.set_fields(_default_fields(include_backlog=False))
    env = _base_env()

    exit_code = _run(env)

    assert exit_code != 0
    assert fake.board_items(ISSUE_URL) == [], (
        "the item must never be added to the board at all when Backlog "
        "can't be resolved -- not added and then left stranded outside "
        "Backlog (#243 round 5)"
    )
    captured = capsys.readouterr()
    assert "::error::" in captured.out


def test_no_status_field_errors_without_ever_adding(fake, capsys):
    """Distinct failure mode #1: no field named "Status" is present at
    all."""
    fake.set_fields([{"id": "PVTF_decoy", "name": "Priority", "options": []}])
    env = _base_env()

    exit_code = _run(env)

    assert exit_code != 0
    combined = "".join(capsys.readouterr())
    assert "::error::" in combined
    assert "Status field" in combined, (
        f"expected the Status-field-missing error message; output={combined!r}"
    )
    assert "Backlog option" not in combined, (
        "must not report the Backlog-specific error when Status itself "
        f"could not be resolved; output={combined!r}"
    )
    assert fake.board_items(ISSUE_URL) == []


def test_status_present_without_backlog_option_errors_with_a_distinct_message(fake, capsys):
    """Distinct failure mode #2: Status resolves fine, but its `options`
    has no entry named "Backlog" -- must not be masked by (or reuse) the
    "Status field missing" message from the sibling test above."""
    fake.set_fields(_default_fields(include_backlog=False))
    env = _base_env()

    exit_code = _run(env)

    assert exit_code != 0
    combined = "".join(capsys.readouterr())
    assert "::error::" in combined
    assert "Backlog option" in combined, (
        f"expected the Backlog-option-missing error message; output={combined!r}"
    )
    assert "Status field" not in combined, (
        "must not report the Status-field-missing error when Status was "
        f"actually resolved; output={combined!r}"
    )
    assert fake.board_items(ISSUE_URL) == []


def test_item_add_failure_errors_without_editing(fake, capsys):
    fake.set_fields(_default_fields())
    fake.fail_item_add()
    env = _base_env()

    exit_code = _run(env)

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "::error::" in captured.out
    assert fake.board_item_status(ISSUE_URL) is None
    # #243 test-critic finding 6: board_item_status(...) is None both when
    # the item was never added AND when it was added but never edited --
    # assert directly that item-edit was never even attempted, so a
    # wrongly-continued edit after a failed add can't hide behind that
    # ambiguity.
    item_edit_calls = [c for c in fake.calls if c[:2] == ["project", "item-edit"]]
    assert item_edit_calls == [], f"item-edit must never be attempted after a failed item-add; calls={item_edit_calls!r}"


# ---------------------------------------------------------------------
# Round-2 blocking finding 1+2: item-add succeeds but the SUBSEQUENT
# item-edit fails -- the item is genuinely stranded on the board outside
# Backlog. This must be distinguishable from test_item_add_failure_errors_
# without_editing above (item never added at all): the error message must
# say the item WAS added, and board_items(ISSUE_URL) must be non-empty.
# ---------------------------------------------------------------------


def test_item_edit_failure_after_successful_add_reports_item_was_added_but_status_unset(fake, capsys):
    fake.set_fields(_default_fields())
    fake.fail_item_edit()
    env = _base_env()

    exit_code = _run(env)

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "::error::" in captured.out

    # Distinct from the item-add-failure scenario: the item IS on the board.
    assert fake.board_items(ISSUE_URL) != [], (
        "item-add succeeded in this scenario -- the item must actually be "
        "on the board, unlike the item-add-failure case above"
    )
    # ... but its status was never set to Backlog (the edit failed).
    assert fake.board_item_status(ISSUE_URL) is None

    # The error message must say the item WAS added, so an operator reading
    # it knows to go check the board manually rather than assume nothing
    # happened (this is the whole point of the round-2 finding).
    assert "added" in captured.out.lower(), (
        f"error message must acknowledge the item was already added to the board; output={captured.out!r}"
    )
    assert ISSUE_URL in captured.out
    # Must not be confusable with the "Failed to add" wording used when
    # item-add itself fails.
    assert "failed to add" not in captured.out.lower(), (
        f"must not reuse the item-add-failure wording for a distinct item-edit failure; output={captured.out!r}"
    )


# ---------------------------------------------------------------------
# Round-4 regression, generalised: field-list pagination completeness.
# ---------------------------------------------------------------------


def test_field_list_pagination_completeness_resolves_status_beyond_first_page(fake):
    decoys = [{"id": f"PVTF_decoy{i}", "name": f"Decoy {i}", "options": []} for i in range(32)]
    status_field = _default_fields()[0]
    fields = decoys + [status_field]  # Status sits at index 32, past a naive --limit 30
    fake.set_fields(fields, total_count=len(fields))
    env = _base_env()

    exit_code = _run(env)

    assert exit_code == 0, (
        "Status must still resolve correctly even though it sits past a "
        "naive --limit 30 response -- the code must notice truncation "
        "against totalCount and re-request with a bigger limit (round 4, "
        "generalised)"
    )
    assert fake.board_item_status(ISSUE_URL) == "Backlog"


def test_decoy_field_first_and_reversed_json_key_order_still_resolves(fake):
    fields = [
        {"name": "Priority", "id": "PVTF_decoy_priority", "options": []},
        {
            "options": [
                {"name": "Todo", "id": "todo_opt_040"},
                {"name": "Backlog", "id": "backlog_opt_050"},
            ],
            "name": "Status",
            "id": "PVTSSF_synthetic_status_030",
        },
    ]
    fake.set_fields(fields)
    env = _base_env()

    exit_code = _run(env)

    assert exit_code == 0
    assert fake.board_item_status(ISSUE_URL) == "Backlog"


def test_field_list_truncation_undetectable_without_total_count_raises(fake, capsys):
    """#243 test-critic finding 7: proves the "truncation undetectable ->
    raise a named error" branch is real, not just theoretical -- exactly
    ``_DEFAULT_FIELD_LIST_LIMIT`` (30) fields come back, ``totalCount`` is
    omitted entirely (``omit_total_count=True``), so the response's own
    shape gives no way to tell whether Status (absent from these 30 decoys)
    is genuinely missing or just sitting past a truncated page. The item
    must never be added to the board in this ambiguous case."""
    decoys = [{"id": f"PVTF_decoy{i}", "name": f"Decoy {i}", "options": []} for i in range(30)]
    fake.set_fields(decoys, omit_total_count=True)
    env = _base_env()

    exit_code = _run(env)

    assert exit_code != 0
    assert fake.board_items(ISSUE_URL) == []
    captured = capsys.readouterr()
    assert "::error::" in captured.out


# ---------------------------------------------------------------------
# Static guard -- no hardcoded Projects v2 node id literal in ci/board.py.
# ci/board.py does not exist yet in this dispatch; per the plan, that is
# itself the useful signal here, so this test is skipped (not failed) with
# an explicit reason until the module exists -- it becomes a real
# regression guard the moment ci/board.py is written in the implement
# dispatch.
# ---------------------------------------------------------------------


def test_no_hardcoded_project_field_or_option_id_literal_in_board_module():
    if not BOARD_MODULE.is_file():
        pytest.skip(f"{BOARD_MODULE} does not exist yet (generation 2 implementation dispatch)")

    text = BOARD_MODULE.read_text(encoding="utf-8")
    hits = _HARDCODED_ID_PATTERN.findall(text)
    assert hits == [], f"hardcoded Projects v2 node id(s) found in ci/board.py: {hits}"

    # #243 test-critic finding 8: scan BOTH composite actions' action.yml,
    # not just add-to-board's -- a hardcoded id could just as easily leak
    # into file-consumer-ticket's (e.g. a copy-pasted example).
    for action_yml in (ADD_TO_BOARD_ACTION_YML, FILE_TICKET_ACTION_YML):
        if action_yml.is_file():
            action_text = action_yml.read_text(encoding="utf-8")
            action_hits = _HARDCODED_ID_PATTERN.findall(action_text)
            assert action_hits == [], f"hardcoded Projects v2 node id(s) found in {action_yml}: {action_hits}"
