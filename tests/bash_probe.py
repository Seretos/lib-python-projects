"""Locates a real POSIX bash for the one test file that still needs to
execute a bash ``run:`` block directly:
``tests/test_ticket_workflow_failure_reporting.py``'s "Report consumer
failures" step in ``ticket.yml``, which is not part of the ``ci/`` rewrite
(#243 generation 2) -- see that module's own docstring for why.

Extracted from the now-deleted ``tests/bash_script_harness.py``, which used
to additionally carry a fake-``gh``-executable harness for exercising the
old ``.sh`` scripts (``file-ticket.sh`` / ``add-to-board.sh``) directly.
Those scripts are gone -- replaced by ``ci/bump_ticket.py`` / ``ci/board.py``
-- so only the bash-discovery piece is still needed, and lives here.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest


def _find_bash() -> str | None:
    """Locate a real POSIX bash, honouring $BASH_FOR_TESTS, preferring Git
    Bash on Windows, and explicitly rejecting the WSL System32 stub (which
    fails immediately outside a WSL distro context)."""
    override = os.environ.get("BASH_FOR_TESTS")
    if override and Path(override).is_file():
        return override

    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).is_file():
                return candidate

    found = shutil.which("bash")
    if found and "System32" in found:
        # The WSL launcher stub -- not a usable POSIX bash without a
        # configured WSL distro. Reject it explicitly so we skip cleanly
        # instead of failing every test with a confusing WSL error.
        return None
    return found


BASH_PATH = _find_bash()

requires_bash = pytest.mark.skipif(BASH_PATH is None, reason="bash unavailable")
