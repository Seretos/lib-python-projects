"""In-process `gh` CLI simulator used by the ``ci/`` test suite (#243,
generation 2).

See ``tests/fake_gh/model.py`` for the ``FakeGitHub`` class itself and the
design rationale in ``.adev/243-1/plan.md``: strict per-subcommand argv
parsing (so an invalid flag combination fails the same way real ``gh``
would), real REST pagination, and mutable state that production code's
*outcomes* are asserted against -- never argv-shape assertions.

This package re-exports the small public surface tests import.
"""

from tests.fake_gh.model import FakeGitHub, GhCallError

__all__ = ["FakeGitHub", "GhCallError"]
