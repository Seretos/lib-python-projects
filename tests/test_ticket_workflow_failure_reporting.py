"""Driving tests for ticket.yml's "Report consumer failures" step (review
fix-up for #243): the step distinguishes filing failures (``::error::`` +
non-zero exit, reddening the job) from board-only failures (``::warning::``
only, job stays green) based on ``steps.*.outcome``. This branching already
exists in ``ticket.yml`` (added by the #243 implementation) but had zero
test coverage before this file: a regression that made a board hiccup also
set ``failed=1`` would currently pass the whole suite undetected.

These tests are RETROSPECTIVE regression tests, disclosed honestly as such:
``ticket.yml``'s step already implements the described behaviour correctly,
so running them against the current step is a GREEN run from the start,
not a RED->GREEN transition -- there is no "unfixed" production code left
to demonstrate failing against. Their protective value is forward-looking:
they pin the filing-vs-board distinction so a future edit that reintroduces
the "board failure reddens the job" regression the review flagged is
caught immediately. (Verified manually during development, not committed:
temporarily editing the extracted run: block to also set ``failed=1`` on a
board failure makes ``test_board_only_failure_warns_without_reddening_job``
fail for the expected reason -- exit code becomes 1 instead of 0 -- which
confirms the test is not vacuous.)

Approach: extract the step's literal ``run:`` block text via ruamel.yaml
(same technique as ``test_bump_workflow_wiring.py``), replace each
``${{ steps.<id>.outcome }}`` GitHub Actions expression with a bash env-var
reference (``$<ID>_OUTCOME``) the way GitHub Actions substitutes those
expressions before bash ever sees the script, then execute the resulting
script under a real bash with the corresponding env vars set. No ``gh``
call exists in this step, so ``bash_script_harness``'s fake-argv-log
machinery isn't needed -- only its bash discovery.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from tests.bash_probe import BASH_PATH, requires_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKET_YML = REPO_ROOT / ".github" / "workflows" / "ticket.yml"

_EXPR_PATTERN = re.compile(r"\$\{\{\s*steps\.(\w+)\.outcome\s*\}\}")


def _extract_run_block(step_name: str) -> str:
    yaml = YAML(typ="safe")
    with TICKET_YML.open("r", encoding="utf-8") as f:
        workflow = yaml.load(f)

    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                run_block = step.get("run")
                assert run_block, f"step {step_name!r} has no run: block"
                return run_block

    raise AssertionError(f"no step named {step_name!r} found in {TICKET_YML}")


def _templated_script() -> str:
    run_block = _extract_run_block("Report consumer failures")
    return _EXPR_PATTERN.sub(lambda m: "$" + m.group(1).upper() + "_OUTCOME", run_block)


def _run(tmp_path: Path, outcomes: dict[str, str]) -> subprocess.CompletedProcess:
    assert BASH_PATH is not None, "run called without a usable bash"
    script_path = tmp_path / "report.sh"
    script_path.write_text(_templated_script(), newline="\n")

    env = dict(os.environ)
    env.update(outcomes)

    return subprocess.run(
        [BASH_PATH, str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=30,
    )


def _default_outcomes() -> dict[str, str]:
    return {
        "FILE_PROJECTS_OUTCOME": "success",
        "FILE_WORKBOARD_OUTCOME": "success",
        "BOARD_PROJECTS_OUTCOME": "success",
        "BOARD_WORKBOARD_OUTCOME": "success",
    }


def test_extraction_finds_all_four_outcome_expressions():
    """Sanity guard on the extraction itself: if a future edit renames a
    step id, the substitution would silently stop matching and every test
    below would run against unsubstituted `${{ }}` syntax (which bash
    treats as a literal string, not a variable) -- catch that loudly here
    instead of via confusing failures below."""
    run_block = _extract_run_block("Report consumer failures")
    found_ids = set(_EXPR_PATTERN.findall(run_block))
    assert found_ids == {
        "file_projects",
        "file_workboard",
        "board_projects",
        "board_workboard",
    }, f"expected outcome expressions for all four steps; found={found_ids}"


@requires_bash
def test_all_success_exits_zero_with_no_annotations(tmp_path):
    result = _run(tmp_path, _default_outcomes())
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "::error::" not in result.stdout
    assert "::warning::" not in result.stdout


@requires_bash
def test_filing_failure_reddens_job_with_error_annotation(tmp_path):
    """A filing failure (the ticket was never created) must fail the job."""
    outcomes = _default_outcomes()
    outcomes["FILE_PROJECTS_OUTCOME"] = "failure"

    result = _run(tmp_path, outcomes)

    assert result.returncode != 0, (
        f"a filing failure must redden the job; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "::error::" in result.stdout, f"expected ::error::; stdout={result.stdout!r}"


@requires_bash
def test_board_only_failure_warns_without_reddening_job(tmp_path):
    """Review fix-up for #243: a board-add failure, with filing having
    succeeded, must only warn -- the job must stay green since the ticket
    itself is fully usable without a board placement."""
    outcomes = _default_outcomes()
    outcomes["BOARD_PROJECTS_OUTCOME"] = "failure"

    result = _run(tmp_path, outcomes)

    assert result.returncode == 0, (
        f"a board-only failure must not redden the job; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "::warning::" in result.stdout, f"expected ::warning::; stdout={result.stdout!r}"
    assert "::error::" not in result.stdout, (
        f"board failure must never emit ::error::; stdout={result.stdout!r}"
    )


@requires_bash
def test_both_board_failures_warn_independently_without_reddening_job(tmp_path):
    """Edge-case coverage: both board steps failing (filing succeeded for
    both) still stays green and emits both warnings."""
    outcomes = _default_outcomes()
    outcomes["BOARD_PROJECTS_OUTCOME"] = "failure"
    outcomes["BOARD_WORKBOARD_OUTCOME"] = "failure"

    result = _run(tmp_path, outcomes)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout.count("::warning::") == 2, f"stdout={result.stdout!r}"
    assert "::error::" not in result.stdout
