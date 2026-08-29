"""Driving tests for R2 (#251): release.yml's "Create GitHub Release" step
must thread a computed previous-release tag into
``gh release create --notes-start-tag <prev>``, so the auto-generated notes
only cover the delta since the previous release -- and must omit the flag
entirely when there is no previous tag (first release).

Approach: extract that step's literal ``run:`` block via ``ruamel.yaml``
(same technique ``tests/test_bump_workflow_wiring.py`` and
``tests/test_ticket_workflow_failure_reporting.py`` already use), execute it
under a real bash (``tests.bash_probe.BASH_PATH`` / ``requires_bash``) with a
``gh`` shim on ``PATH`` that appends its argv to a log file, then assert on
the recorded argv.

Today the step still interpolates ``${{ inputs.version }}`` /
``${{ github.repository }}`` directly into the shell script rather than
reading them from an ``env:`` block (planned R2 change: make the step
env-driven so ``VERSION``/``REPO``/``PREV_TAG`` are ordinary bash variables,
which is also what removes GitHub Actions expression syntax from the shell
script body entirely). Executing today's raw block through real bash would
hit that unsubstituted ``${{ ... }}`` syntax as a "bad substitution" bash
parse-time error and never even reach the ``gh`` call -- an artifact of the
extraction *technique*, not of production behaviour (GitHub Actions itself
always substitutes ``${{ }}`` before bash ever sees the script). To keep the
RED failure attributable to the real missing behaviour (no
``--notes-start-tag``) rather than to that extraction artifact, this test
substitutes ``${{ inputs.version }}`` -> ``$VERSION`` and
``${{ github.repository }}`` -> ``$REPO`` itself before executing -- exactly
mirroring the substitution ``test_ticket_workflow_failure_reporting.py``
already does for ``${{ steps.*.outcome }}``. No production file is touched
by this test file.

Expected RED reason: with PREV_TAG=v0.3.13, the recorded
``gh release create`` argv contains no ``--notes-start-tag`` -- the current
block never emits it (there is no ``$PREV_TAG`` reference in it at all yet).
Genuine behaviour failure, not a parse/setup error (confirmed manually:
running the substituted script through the fake gh shim today records
``release create v0.3.14 --repo ... --title v0.3.14 --generate-notes`` with
no ``--notes-start-tag`` anywhere in the log).

A second, independent guard test asserts the RAW (un-substituted) run block
in release.yml today contains no lingering GitHub Actions expression syntax
-- this is the R2 approach's own acceptance bar ("removes expression
interpolation into a shell script") and is RED today for a different,
equally genuine reason: the raw block still contains
``${{ inputs.version }}`` (twice) and ``${{ github.repository }}`` (once).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from tests.bash_probe import BASH_PATH, requires_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"

STEP_NAME = "Create GitHub Release"

_VERSION_EXPR = re.compile(r"\$\{\{\s*inputs\.version\s*\}\}")
_REPO_EXPR = re.compile(r"\$\{\{\s*github\.repository\s*\}\}")


def _extract_run_block(step_name: str) -> str:
    yaml = YAML(typ="safe")
    with RELEASE_YML.open("r", encoding="utf-8") as f:
        workflow = yaml.load(f)

    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                run_block = step.get("run")
                assert run_block, f"step {step_name!r} has no run: block"
                return run_block

    raise AssertionError(f"no step named {step_name!r} found in {RELEASE_YML}")


def _test_substituted_script() -> str:
    """The raw run block with this *test's own* GitHub-Actions-expression
    substitution applied (see module docstring) -- not a stand-in for the
    production env-driven rewrite, only for keeping this test's RED
    attributable to the missing --notes-start-tag behaviour."""
    run_block = _extract_run_block(STEP_NAME)
    run_block = _VERSION_EXPR.sub("$VERSION", run_block)
    run_block = _REPO_EXPR.sub("$REPO", run_block)
    return run_block


# Record separator between successive gh invocations in the argv log. Each
# argument is written on its own line (via ``printf '%s\n'``, one line per
# "$@" element) rather than joined with ``echo "$@"``, which collapses
# through shell word-splitting and can never represent a dangling empty
# argument distinctly from "no argument at all" -- see
# test_no_notes_start_tag_flag_when_previous_tag_empty below.
_CALL_SEP = "\x1e"


def _install_gh_shim(bin_dir: Path, log_path: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "gh"
    log = log_path.as_posix()
    script.write_text(
        "#!/bin/sh\n"
        f'for arg in "$@"; do printf \'%s\\n\' "$arg" >> "{log}"; done\n'
        f'printf \'{_CALL_SEP}\\n\' >> "{log}"\n'
        "exit 0\n",
        newline="\n",
    )
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_step(
    tmp_path: Path, env_overrides: dict[str, str]
) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    assert BASH_PATH is not None, "run called without a usable bash"

    bin_dir = tmp_path / "fakebin"
    log_path = tmp_path / "gh_argv.log"
    _install_gh_shim(bin_dir, log_path)

    script_path = tmp_path / "release_step.sh"
    script_path.write_text(_test_substituted_script(), newline="\n")

    env = dict(os.environ)
    env.update(env_overrides)
    env["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])

    result = subprocess.run(
        [BASH_PATH, str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    calls: list[list[str]] = []
    if log_path.exists():
        raw = log_path.read_text(encoding="utf-8")
        for block in raw.split(_CALL_SEP + "\n"):
            if block == "":
                continue
            lines = block.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            calls.append(lines)
    return result, calls


# ---------------------------------------------------------------------
# R2 driving test
# ---------------------------------------------------------------------


@requires_bash
def test_notes_start_tag_flag_added_when_previous_tag_exists(tmp_path):
    result, calls = _run_step(
        tmp_path,
        {"VERSION": "0.3.14", "REPO": "Seretos/lib-python-projects", "PREV_TAG": "v0.3.13"},
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert len(calls) == 1, f"expected exactly one gh invocation; calls={calls!r}"
    argv = calls[0]

    assert "--generate-notes" in argv, f"argv={argv!r}"
    assert "--notes-start-tag" in argv, f"argv={argv!r}"
    idx = argv.index("--notes-start-tag")
    assert argv[idx + 1] == "v0.3.13", f"argv={argv!r}"


# ---------------------------------------------------------------------
# R2 guard test (independent RED reason: raw block still uses ${{ }})
# ---------------------------------------------------------------------


def test_raw_run_block_has_no_unsubstituted_github_actions_expressions():
    """The R2 approach's own acceptance bar: once the step is rewired to be
    env-driven, its run: block text (as authored in the YAML, no test-side
    substitution) must contain zero ``${{ ... }}`` expressions -- everything
    interpolated moves into the step's env: block instead."""
    run_block = _extract_run_block(STEP_NAME)
    assert "${{" not in run_block, (
        f"step {STEP_NAME!r}'s run: block still contains unsubstituted GitHub Actions "
        f"expression syntax; expected it to be fully env-driven:\n{run_block}"
    )


# ---------------------------------------------------------------------
# Additional edge-case coverage
# ---------------------------------------------------------------------


@requires_bash
def test_no_notes_start_tag_flag_when_previous_tag_empty(tmp_path):
    """First release: no previous tag. The flag must not appear at all --
    not even as a dangling empty-string argument."""
    result, calls = _run_step(
        tmp_path,
        {"VERSION": "0.1.0", "REPO": "Seretos/lib-python-projects", "PREV_TAG": ""},
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert len(calls) == 1, f"expected exactly one gh invocation; calls={calls!r}"
    argv = calls[0]

    assert "--generate-notes" in argv, f"argv={argv!r}"
    assert "--notes-start-tag" not in argv, f"argv={argv!r}"
    # argv is now built from one printf'd line per "$@" element (see
    # _install_gh_shim), so a dangling empty-string argument (e.g. a bare
    # ``--notes-start-tag ""`` the shell script forgot to omit) would show
    # up as a genuine empty element here -- unlike the old ``echo "$@"`` +
    # ``.split()`` approach, which could never distinguish that from "no
    # argument at all" regardless of what the script actually passed.
    assert "" not in argv, f"a dangling empty-string argument leaked into argv={argv!r}"


@requires_bash
def test_prerelease_flag_still_present_for_prerelease_version(tmp_path):
    """Existing behaviour (unrelated to this ticket) must survive: a
    prerelease VERSION still gets --prerelease, and --notes-start-tag is
    still threaded through correctly alongside it."""
    result, calls = _run_step(
        tmp_path,
        {"VERSION": "0.4.0-rc.1", "REPO": "Seretos/lib-python-projects", "PREV_TAG": "v0.3.14"},
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert len(calls) == 1, f"expected exactly one gh invocation; calls={calls!r}"
    argv = calls[0]

    assert "--prerelease" in argv, f"argv={argv!r}"
    assert "--notes-start-tag" in argv, f"argv={argv!r}"
    idx = argv.index("--notes-start-tag")
    assert argv[idx + 1] == "v0.3.14", f"argv={argv!r}"


@requires_bash
def test_prerelease_flag_absent_for_stable_version(tmp_path):
    """Mirror of test_prerelease_flag_still_present_for_prerelease_version:
    a stable VERSION must NOT get --prerelease. The sibling test only ever
    checked presence for a prerelease version; without this case, an
    implementation that always passed --prerelease unconditionally would
    also pass."""
    result, calls = _run_step(
        tmp_path,
        {"VERSION": "0.3.14", "REPO": "Seretos/lib-python-projects", "PREV_TAG": "v0.3.13"},
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert len(calls) == 1, f"expected exactly one gh invocation; calls={calls!r}"
    argv = calls[0]

    assert "--prerelease" not in argv, f"argv={argv!r}"
