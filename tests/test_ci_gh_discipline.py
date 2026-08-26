"""R6 -- substrate invariants for #243 generation 2 (see
``.adev/243-1/plan.md``): the whole point of moving off bash is that these
rules become *structurally* enforced, not individually patched review
findings. This file has two kinds of test:

1. Tests that exercise ``tests/fake_gh.FakeGitHub`` itself -- these do NOT
   import anything from the not-yet-existing ``ci`` package, so they are
   expected to PASS now (they prove the simulator's own discipline
   enforcement works, independent of production code).
2. Tests that target ``ci.gh`` / ``ci.actions_io`` / the ``ci/`` package as
   a whole -- these are expected to RED now, because ``ci/`` does not exist
   yet in this dispatch. Each RED reason is either a genuine
   ``ModuleNotFoundError`` (imported lazily inside the test body, so only
   that test fails, not the whole file at collection) or an explicit,
   clearly-worded assertion failure (never a vacuous pass from an empty
   glob) -- see each test's docstring for which.

Do not implement ``ci/`` in this dispatch; that is a separate
(``phase=implement``) dispatch.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

import pytest

from tests.fake_gh import FakeGitHub, GhCallError

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_DIR = REPO_ROOT / "ci"

_FORBIDDEN_SUBSTRINGS = ["--jq", "-q ", "--template", "--paginate", "shell=True", "os.system"]
_BARE_EXCEPT_PASS = re.compile(r"except\s*:\s*\n\s*pass")


# ---------------------------------------------------------------------
# 1. FakeGitHub's own discipline enforcement -- no `ci` import needed,
#    expected to PASS now.
# ---------------------------------------------------------------------


def test_unknown_flag_is_rejected_by_the_simulator():
    """Meta-test proving the simulator really rejects a malformed argv the
    way real `gh` would -- the exact class of bug (round 3: `--jq --arg`)
    that generation 1's blanket-responding fake could never have caught."""
    fake = FakeGitHub()
    fake.add_release("Seretos/lib-python-projects", "v1.0.0", "some notes")

    with pytest.raises(GhCallError):
        fake.run_gh(
            [
                "release",
                "view",
                "v1.0.0",
                "--repo",
                "Seretos/lib-python-projects",
                "--json",
                "body",
                "--bogus-flag",
                "surprise-value",
            ]
        )


@pytest.mark.parametrize("forbidden_flag", ["--jq", "-q", "--paginate", "--template"])
def test_forbidden_cli_side_filtering_flags_are_rejected_at_runtime(forbidden_flag):
    """Dynamic counterpart to the static source-scan below: even if a
    forbidden flag somehow made it into a real call, the simulator itself
    refuses to answer it -- and it refuses unconditionally (even with
    check=False), because this is a discipline violation, not a tolerable
    `gh` failure."""
    fake = FakeGitHub()

    with pytest.raises(GhCallError, match=re.escape(forbidden_flag)):
        fake.run_gh(["api", "repos/Seretos/thing/issues", forbidden_flag, "whatever"], check=False)


def test_simulator_rejects_missing_required_flag():
    fake = FakeGitHub()
    with pytest.raises(GhCallError):
        fake.run_gh(["release", "view", "v1.0.0", "--json", "body"])  # missing --repo


# ---------------------------------------------------------------------
# 2. `ci/` package invariants -- expected RED now.
# ---------------------------------------------------------------------


def test_no_cli_side_filtering_or_shell_execution():
    """Source-scans every module under ci/ for the forbidden substrings and
    for `subprocess` usage outside ci/gh.py.

    Expected RED reason now: ci/ does not exist yet. This must fail with a
    clear, explicit assertion naming what's missing -- never a silent
    `glob("ci/**/*.py")` returning `[]` that would make the substring loop
    below vacuously pass.
    """
    assert CI_DIR.is_dir(), (
        f"ci/ package not found at {CI_DIR} -- expected modules gh.py, "
        "actions_io.py, bump_ticket.py, board.py (generation 2, #243). This "
        "test scans them for forbidden substrings once they exist; it is "
        "not yet possible to run that scan."
    )

    modules = sorted(CI_DIR.glob("**/*.py"))
    assert modules, f"expected python modules under {CI_DIR}, found none"

    for path in modules:
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_SUBSTRINGS:
            assert token not in text, f"{path}: forbidden substring {token!r} found"
        assert not _BARE_EXCEPT_PASS.search(text), f"{path}: bare 'except:' immediately followed by 'pass' found"
        if path.name != "gh.py":
            assert "subprocess" not in text, (
                f"{path}: 'subprocess' referenced outside ci/gh.py -- every gh "
                "invocation must go through the one choke point"
            )


def test_ci_package_uses_only_the_standard_library():
    """No third-party import anywhere under ci/ -- the undeclared `jq`
    runtime dependency generation 1 shipped (round 6, never fixed) must not
    recur in any form.

    Expected RED reason now: ci/ does not exist yet -- explicit assertion,
    not a vacuous pass.
    """
    assert CI_DIR.is_dir(), f"ci/ package not found at {CI_DIR} -- cannot verify stdlib-only imports yet"

    stdlib_prefixes = set(sys.stdlib_module_names) | {"ci"}
    import_re = re.compile(r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_.]*)", re.MULTILINE)

    modules = sorted(CI_DIR.glob("**/*.py"))
    assert modules, f"expected python modules under {CI_DIR}, found none"

    offenders = []
    for path in modules:
        text = path.read_text(encoding="utf-8")
        for match in import_re.finditer(text):
            top_level = match.group(1).split(".")[0]
            if top_level not in stdlib_prefixes and top_level != "__future__":
                offenders.append(f"{path}: {match.group(0).strip()}")

    assert offenders == [], f"non-stdlib import(s) found under ci/: {offenders}"


def test_set_output_rejects_multiline_values(tmp_path):
    """Targets the not-yet-existing ci.actions_io.set_output. RED now via
    ModuleNotFoundError -- the import is inside the test body (not at
    module level) so only this test fails, not the whole file.

    #243 test-critic finding 5: assert the specific exception type the
    implementation actually raises (not a bare ``Exception``, which would
    also match e.g. a ``KeyError`` from a missing ``GITHUB_OUTPUT`` -- a
    completely different, non-validation failure), and add a companion
    assertion that a valid single-line value actually succeeds and lands
    correctly, so this test can't be satisfied by an implementation that
    simply always raises."""
    import ci.actions_io  # noqa: PLC0415 -- deliberately lazy, see docstring

    github_output = tmp_path / "github_output.txt"
    github_output.write_text("", encoding="utf-8")
    env = {"GITHUB_OUTPUT": str(github_output)}

    with pytest.raises(ValueError):
        ci.actions_io.set_output("issue_url", "line one\nline two", env)

    ci.actions_io.set_output("issue_url", "https://example.com/issues/1", env)
    assert github_output.read_text(encoding="utf-8") == "issue_url=https://example.com/issues/1\n"


def _write_fake_gh_executable(bin_dir: Path, *, exit_code: int, stderr: str = "boom") -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        script = bin_dir / "gh.cmd"
        script.write_text(f"@echo off\r\n>&2 echo {stderr}\r\nexit /b {exit_code}\r\n")
    else:
        script = bin_dir / "gh"
        script.write_text(f"#!/bin/sh\necho '{stderr}' 1>&2\nexit {exit_code}\n")
        mode = script.stat().st_mode
        script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_run_gh_raises_on_nonzero_exit_by_default(tmp_path, monkeypatch):
    """Targets the not-yet-existing ci.gh.run_gh. RED now via
    ModuleNotFoundError. Once ci.gh exists, this proves class B (silent
    error swallowing) is closed: check=True (the default) must raise on a
    non-zero exit, never `2>/dev/null || true` its way past it."""
    import ci.gh  # noqa: PLC0415

    fake_bin = tmp_path / "fakebin"
    _write_fake_gh_executable(fake_bin, exit_code=1)
    monkeypatch.setenv("PATH", str(fake_bin))

    with pytest.raises(ci.gh.GhError):
        ci.gh.run_gh(["issue", "list"])


def test_gh_paginate_rest_raises_distinct_exception_on_page_exhaustion(monkeypatch):
    """Targets the not-yet-existing ci.gh.gh_paginate_rest / gh.GhPaginationExhausted.
    RED now via ModuleNotFoundError.

    #243 round 2 blocking finding 3: exhausting max_pages without ever
    seeing a short "last page" chunk must raise a distinct exception type
    (a GhError subclass), not the same plain GhError a single failed
    request raises -- so a caller like ci.bump_ticket's idempotency probe
    can tell "the listing is known-incomplete" apart from "one gh call
    failed" and refuse to silently risk a duplicate."""
    import ci.gh  # noqa: PLC0415

    def always_full_page(args, *, check=True):
        return json.dumps([{"i": i} for i in range(100)])

    monkeypatch.setattr(ci.gh, "run_gh", always_full_page)

    with pytest.raises(ci.gh.GhPaginationExhausted) as excinfo:
        ci.gh.gh_paginate_rest("repos/Seretos/thing/issues", max_pages=2)

    # Must genuinely be a GhError subclass too, so existing `except GhError`
    # call sites that don't yet distinguish it keep working.
    assert isinstance(excinfo.value, ci.gh.GhError)


def test_run_gh_raises_when_gh_is_absent_from_path(tmp_path, monkeypatch):
    """Targets the not-yet-existing ci.gh.run_gh. RED now via
    ModuleNotFoundError. Once ci.gh exists: resolving `gh` via
    shutil.which and raising a named error when it's absent, rather than
    letting subprocess.run raise a raw, less actionable FileNotFoundError."""
    import ci.gh  # noqa: PLC0415

    empty_dir = tmp_path / "empty_path"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))

    with pytest.raises(ci.gh.GhError):
        ci.gh.run_gh(["issue", "list"])
