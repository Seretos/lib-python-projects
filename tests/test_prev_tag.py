"""Driving tests for R1 (#251): the not-yet-existing ``ci.prev_tag`` module,
which computes the previous released ``v*`` tag so ``release.yml`` can pass
it to ``gh release create --notes-start-tag``, scoping the auto-generated
notes to only the delta since the last release.

Entry-point API decision (mirroring ``ci.bump_ticket`` / ``ci.board``'s
established shape): ``ci.prev_tag.run(env: dict[str, str]) -> int``. It reads
``VERSION``, ``SOURCE_REPO``, ``GH_TOKEN`` from the ``env`` mapping passed in
(see ``REQUIRED_ENV``), lists *published GitHub Releases* (not raw Git tags)
via ``ci.gh.gh_paginate_rest`` (the single choke point) against
``repos/{repo}/releases``, and writes ``previous_tag`` to ``$GITHUB_OUTPUT``
via ``ci.actions_io.set_output`` -- empty string when there is no lower
published-release tag or when the release listing fails. A notes-range
lookup must never fail a release: a ``ci.gh.GhError`` (which
``ci.gh.GhPaginationExhausted`` also is, being a subclass) is caught, warned
about via ``ci.actions_io.warn``, and still exits 0 with an empty
``previous_tag``.

Releases vs. tags (round-3 review finding, #251): ``repos/{repo}/tags``
lists every Git tag regardless of whether a Release backs it. This repo's
own release workflow pushes the ``v*`` tag and creates the GitHub Release in
two separate, non-atomic steps -- a runner failure or transient ``gh``
outage between them can leave a real, pushed tag with no Release ever
published for it. Sourcing candidates from ``releases`` instead closes that
gap structurally: an orphaned tag with no Release entry simply never appears
in the listing. Draft releases are filtered out (not yet "published");
prereleases are kept (this repo's own ``-rc.N`` versions are published,
non-draft Releases).

Tag matching mirrors release.yml's own version-validation regex exactly
(``^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?$``), prefixed with the ``v``
every tag in this repo carries. A tag that doesn't match is ignored, not
treated as an error. Ordering: parsed (major, minor, patch) compares
first; a prerelease suffix sorts *below* the same (major, minor, patch)
with no suffix (release), and prerelease suffixes compare against each
other lexicographically by dot-separated identifier (mirroring common
semver precedence closely enough for this repo's own prerelease tags,
which are simple ``-rc.N`` style).

RED reason for every test below that imports ``ci.prev_tag``:
``ModuleNotFoundError: No module named 'ci.prev_tag'`` -- the module does
not exist yet in this dispatch. Imports are lazy (inside each test, not at
module level) so this file collects cleanly and each test fails
individually for the same, correctly-attributed reason -- matching this
repo's established RED style (see e.g. ``tests/test_bump_ticket.py``).

Do not implement ``ci/prev_tag.py`` in this dispatch; that is a separate
(``phase=implement``) dispatch.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_gh import FakeGitHub

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_GH_EXECUTABLE = REPO_ROOT / "tests" / "fake_gh" / "executable.py"

SOURCE_REPO = "Seretos/lib-python-projects"


@pytest.fixture
def fake(monkeypatch):
    """A fresh FakeGitHub wired in as ci.gh.run_gh. Does NOT itself raise
    ModuleNotFoundError -- ci.gh already exists (#243) -- each test's own
    ``import ci.prev_tag`` is what RREDs."""
    import ci.gh  # noqa: PLC0415 -- deliberately lazy, see module docstring

    sim = FakeGitHub()
    monkeypatch.setattr(ci.gh, "run_gh", sim.run_gh)
    return sim


def _env(tmp_path: Path, *, version: str, source_repo: str = SOURCE_REPO) -> dict[str, str]:
    github_output = tmp_path / "github_output.txt"
    github_output.write_text("", encoding="utf-8")
    return {
        "VERSION": version,
        "SOURCE_REPO": source_repo,
        "GH_TOKEN": "fake-token",
        "GITHUB_OUTPUT": str(github_output),
    }


def _read_outputs(github_output_path: str) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for line in Path(github_output_path).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            outputs[k] = v
    return outputs


def _run(env: dict[str, str]) -> int:
    import ci.prev_tag  # noqa: PLC0415 -- deliberately lazy, see module docstring

    return ci.prev_tag.run(env)


# ---------------------------------------------------------------------
# R1 driving test
# ---------------------------------------------------------------------


def test_greatest_lower_tag_is_selected(tmp_path, fake):
    """The driving test: given a scattered set of released tags, the
    greatest one strictly below the version being released is picked --
    not the most-recently-added one, not the lexicographically-greatest
    string (``v0.3.9`` < ``v0.3.13`` numerically but not as a string)."""
    env = _env(tmp_path, version="0.3.14")
    fake.add_tag(SOURCE_REPO, "v0.1.0")
    fake.add_tag(SOURCE_REPO, "v0.3.12")
    fake.add_tag(SOURCE_REPO, "v0.3.13")

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == "v0.3.13"


# ---------------------------------------------------------------------
# R3 driving test (round-3 review finding, #251)
# ---------------------------------------------------------------------


def test_unreleased_tag_is_excluded_even_though_it_is_semver_shaped(tmp_path, fake):
    """A ``v*``-shaped Git tag that exists but has no backing published
    GitHub Release (e.g. a run that pushed the tag but died before
    "Create GitHub Release" ran) must never be selected as
    ``--notes-start-tag``, even though it is the greatest semver-shaped tag
    strictly below VERSION -- ``ci.prev_tag`` must source candidates from
    ``repos/{repo}/releases``, not ``repos/{repo}/tags``. The true previous
    *released* tag (``v0.3.12``) must win instead."""
    env = _env(tmp_path, version="0.3.14")
    fake.add_tag(SOURCE_REPO, "v0.3.12")
    fake.add_unreleased_tag(SOURCE_REPO, "v0.3.13")  # orphaned: tag exists, no Release

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == "v0.3.12"


def test_draft_release_tag_is_excluded(tmp_path, fake):
    """A draft Release's tag is present in the releases listing but is not
    yet "published" -- it must not be selected as ``--notes-start-tag``
    either."""
    env = _env(tmp_path, version="0.3.14")
    fake.add_tag(SOURCE_REPO, "v0.3.12")
    fake.add_draft_release_tag(SOURCE_REPO, "v0.3.13")

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == "v0.3.12"


# ---------------------------------------------------------------------
# Additional edge-case coverage
# ---------------------------------------------------------------------


def test_self_and_higher_tags_are_excluded(tmp_path, fake):
    """The just-pushed tag for the version being released (already in the
    listing by the time this step runs, since the previous workflow step
    pushed it) and any tag above it must both be excluded -- selection is
    strictly-below, not less-than-or-equal."""
    env = _env(tmp_path, version="0.3.14")
    fake.add_tag(SOURCE_REPO, "v0.3.12")
    fake.add_tag(SOURCE_REPO, "v0.3.13")
    fake.add_tag(SOURCE_REPO, "v0.3.14")  # self -- must be excluded
    fake.add_tag(SOURCE_REPO, "v0.4.0")  # higher -- must be excluded

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == "v0.3.13"


def test_prerelease_orders_below_its_release_and_above_lower_versions(tmp_path, fake):
    """A prerelease tag (``v0.4.0-rc.1``) sorts below the release it
    precedes (``v0.4.0``, the self tag, excluded) but above an unrelated
    lower released version (``v0.3.14``)."""
    env = _env(tmp_path, version="0.4.0")
    fake.add_tag(SOURCE_REPO, "v0.3.14")
    fake.add_tag(SOURCE_REPO, "v0.4.0-rc.1")
    fake.add_tag(SOURCE_REPO, "v0.4.0")  # self -- must be excluded

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == "v0.4.0-rc.1"


def test_non_semver_tags_are_ignored(tmp_path, fake):
    """Tags that don't match ``vMAJOR.MINOR.PATCH[-PRERELEASE]`` are
    silently ignored, not treated as an error and not allowed to crash the
    numeric comparison."""
    env = _env(tmp_path, version="0.3.14")
    fake.add_tag(SOURCE_REPO, "v0.3.13")
    fake.add_tag(SOURCE_REPO, "latest")
    fake.add_tag(SOURCE_REPO, "release/0.x")
    fake.add_tag(SOURCE_REPO, "0.3.13")  # missing the leading v

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == "v0.3.13"


def test_non_semver_tags_are_dropped_not_ranked(tmp_path, fake):
    """Distinguishes "ignored" from "ranked lowest": when the ONLY tags
    below VERSION are non-semver, they must be dropped entirely rather than
    coerced into some low rank that would still let one of them win --
    previous_tag must come back empty. (test_non_semver_tags_are_ignored
    above cannot tell these two behaviours apart, since v0.3.13 -- a valid
    lower semver tag -- is present there and wins either way.)"""
    env = _env(tmp_path, version="0.3.14")
    fake.add_tag(SOURCE_REPO, "latest")
    fake.add_tag(SOURCE_REPO, "release/0.x")
    fake.add_tag(SOURCE_REPO, "0.3.13")  # missing the leading v

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == ""


def test_no_lower_tag_writes_empty_output(tmp_path, fake):
    """First release of a repo (or of a major line): no tag below VERSION
    exists. The step must not fail -- empty previous_tag, exit 0."""
    env = _env(tmp_path, version="0.1.0")
    fake.add_tag(SOURCE_REPO, "v0.1.0")  # self only

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == ""


def test_gh_failure_warns_and_writes_empty_output_without_failing(tmp_path, monkeypatch, capsys):
    """A notes-range lookup must never fail a release: a gh.GhError while
    listing tags degrades to today's behaviour (no --notes-start-tag) plus
    a visible ::warning::, never a non-zero exit."""
    import ci.gh  # noqa: PLC0415

    def _boom(path, **kwargs):
        raise ci.gh.GhError("simulated gh api outage")

    monkeypatch.setattr(ci.gh, "gh_paginate_rest", _boom)

    env = _env(tmp_path, version="0.3.14")

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == ""
    captured = capsys.readouterr()
    assert "::warning::" in captured.out, f"expected ::warning:: on stdout; stdout={captured.out!r}"


def test_gh_malformed_json_warns_and_writes_empty_output_without_failing(tmp_path, monkeypatch, capsys):
    """A notes-range lookup must never fail a release even when the ``gh``
    CLI exits 0 but returns a non-JSON body (e.g. a transient HTML error
    page from an outage): ``ci.gh.gh_paginate_rest`` raises a bare
    ``json.JSONDecodeError`` (a ``ValueError`` subclass, NOT a
    ``ci.gh.GhError``) from its internal ``json.loads`` call in that case --
    round-1 review finding 1. This must degrade exactly like a ``GhError``
    does: empty previous_tag, a visible ::warning::, exit 0."""
    import ci.gh  # noqa: PLC0415

    def _not_json(args, *, check=True):
        return "not valid json"

    monkeypatch.setattr(ci.gh, "run_gh", _not_json)

    env = _env(tmp_path, version="0.3.14")

    exit_code = _run(env)

    assert exit_code == 0
    assert _read_outputs(env["GITHUB_OUTPUT"]).get("previous_tag") == ""
    captured = capsys.readouterr()
    assert "::warning::" in captured.out, f"expected ::warning:: on stdout; stdout={captured.out!r}"


def _install_fake_gh(bin_dir: Path) -> None:
    """Mirrors ``tests/test_ci_entrypoints_subprocess.py``'s shim
    installer: a ``gh``/``gh.cmd`` on PATH that just execs
    ``tests/fake_gh/executable.py`` under the current interpreter.

    Windows-only wrinkle this test is the first to actually exercise:
    ``ci.gh.gh_paginate_rest`` (the only caller of ``run_gh`` this test's
    happy path depends on succeeding) always builds its request path as
    ``...?page=<N>&per_page=100`` -- an unquoted ``&`` with no surrounding
    whitespace. ``subprocess.list2cmdline`` (which ``subprocess.run`` uses
    internally on Windows) only quotes an argument that contains a space,
    tab, or double-quote -- never for shell metacharacters like ``&`` --
    and Windows' own loader transparently reroutes a direct
    ``CreateProcess`` call targeting a ``.bat``/``.cmd`` file through
    ``cmd.exe /c <unquoted command line>``, which *does* treat a bare
    ``&`` as a command separator. The net effect: invoking this ``gh.cmd``
    shim with that path splits the line into two commands -- the real one
    (which runs fine and prints the right stdout) and a phantom
    ``per_page=100`` "command" that ``cmd.exe`` can't find, so the overall
    process exit code comes back non-zero even though the real command
    already succeeded. A companion no-op ``per_page.cmd`` (cmd.exe's own
    command-name lookup stops at the ``=``, so the phantom command
    resolves to ``per_page`` on PATH regardless of the trailing digits)
    absorbs that phantom command harmlessly, so the exit code again
    reflects only the real ``gh`` call -- exactly what happens against the
    real, single-executable ``gh.exe`` in production, which this reroute
    quirk never affects in the first place (it is purely an artifact of
    this test's own ``.cmd``-file simulator)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        script = bin_dir / "gh.cmd"
        script.write_text(f'@echo off\r\n"{sys.executable}" "{FAKE_GH_EXECUTABLE}" %*\r\n')
        noop = bin_dir / "per_page.cmd"
        noop.write_text("@echo off\r\nexit /b 0\r\n")
    else:
        script = bin_dir / "gh"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH_EXECUTABLE}" "$@"\n', newline="\n")
        mode = script.stat().st_mode
        script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_entrypoint_runs_end_to_end_through_a_real_subprocess(tmp_path):
    """Layer 2, mirroring ``tests/test_ci_entrypoints_subprocess.py``: one
    happy path proving ``python -m ci.prev_tag`` really reads real
    ``os.environ``, writes a real ``$GITHUB_OUTPUT`` file, and propagates a
    real exit code through a genuine subprocess boundary."""
    bin_dir = tmp_path / "fakebin"
    _install_fake_gh(bin_dir)

    github_output = tmp_path / "github_output.txt"
    github_output.write_text("", encoding="utf-8")

    child_env = dict(os.environ)
    child_env.update(
        {
            "VERSION": "0.3.14",
            "SOURCE_REPO": SOURCE_REPO,
            "GH_TOKEN": "fake-token",
            "GITHUB_OUTPUT": str(github_output),
            "FAKE_GH_RELEASES_PAGE_JSON": (
                '[{"tag_name": "v0.3.13", "draft": false}, '
                '{"tag_name": "v0.3.14", "draft": false}]'
            ),
        }
    )
    child_env["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])

    result = subprocess.run(
        [sys.executable, "-m", "ci.prev_tag"],
        capture_output=True,
        text=True,
        env=child_env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert _read_outputs(str(github_output)).get("previous_tag") == "v0.3.13"
