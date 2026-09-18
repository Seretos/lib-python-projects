"""Guard against floating-ref git dependencies in pyproject.toml.

Ticket #269: `lib-python-config` was pinned to the floating `release/0.x`
branch. That branch's release workflow force-pushes on every release, so a
clean install of this package could silently resolve to a different, never
reviewed commit at any time. These tests parse `pyproject.toml` (same
`tomllib` idiom as `tests/test_pytest_timeout_config.py`) and assert every
`git+` runtime dependency is pinned to an exact `vX.Y.Z` tag.

The specific fact that `lib-python-config` is pinned to exactly `v0.1.2` is
not asserted here as a standalone literal-string check -- a bare equality
against the current pyproject.toml value has no discriminating power beyond
"the config file currently says v0.1.2" and cannot prove the tag actually
exists/resolves upstream. That value is instead verified by the PR
reviewer reading the pyproject.toml diff, and by a real clean-venv install
(declared `ci-evidence` in the plan).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(30)

_EXACT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Matches "<name> @ git+<url>@<ref>" PEP 508 direct-reference syntax.
_GIT_DEP_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)\s*@\s*(?P<url>git\+\S+?)@(?P<ref>[^@\s]+)$")


def _runtime_dependencies() -> list[str]:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return data["project"]["dependencies"]


def _git_dependency_refs_from(deps: list[str]) -> dict[str, str]:
    """Map dependency name -> pinned ref, for every `git+` entry in `deps`.

    Pure function of its argument -- takes an explicit dependency-string
    list rather than reading `pyproject.toml` itself, so it can be exercised
    directly against a fabricated list in tests, independent of whatever the
    real file currently contains.
    """
    refs: dict[str, str] = {}
    for dep in deps:
        if "git+" not in dep:
            continue
        match = _GIT_DEP_RE.match(dep)
        assert match is not None, f"could not parse git dependency spec: {dep!r}"
        refs[match.group("name")] = match.group("ref")
    return refs


def _git_dependency_refs() -> dict[str, str]:
    """Map dependency name -> pinned ref, for every `git+` dependency in the
    real `pyproject.toml`."""
    return _git_dependency_refs_from(_runtime_dependencies())


def test_git_dependencies_pin_exact_tags() -> None:
    refs = _git_dependency_refs()

    assert refs, "expected at least one git+ dependency to check (lib-python-config)"

    for name, ref in refs.items():
        assert _EXACT_TAG_RE.match(ref), (
            f"git dependency {name!r} is pinned to {ref!r}, which is not an exact "
            f"'vX.Y.Z' tag; a branch/mutable ref can silently resolve to a "
            f"different, never-reviewed commit"
        )


def test_non_git_dependency_is_skipped_by_shape_check() -> None:
    """Exercises `_git_dependency_refs_from` itself against a synthetic,
    hand-constructed dependency list -- not real content read from
    `pyproject.toml` -- so this test discriminates against a broken
    skip-implementation (e.g. one that wrongly matches non-`git+` entries)
    independent of whatever the real file currently contains."""
    deps = [
        "pydantic>=2.0",
        "requests==2.31.0",
        "lib-python-config @ git+https://github.com/Seretos/lib-python-config@v1.0.0",
    ]

    refs = _git_dependency_refs_from(deps)

    assert refs == {"lib-python-config": "v1.0.0"}
