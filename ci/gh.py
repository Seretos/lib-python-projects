"""The one choke point through which every ``gh`` CLI invocation in this
package must pass (see ``ci/__init__.py``'s discipline note and
``tests/test_ci_gh_discipline.py``).

``run_gh`` resolves ``gh`` via ``shutil.which`` (never lets a raw
``FileNotFoundError`` leak out when it's missing from ``PATH``) and never
invokes a shell to run it (no shell-invoking subprocess flag, no
os-module system-execution call). JSON responses are always parsed in
Python: no gh-side query-filter, quiet-JSON, templating, or automatic-
pagination flag is ever passed to ``gh`` -- ``gh_paginate_rest`` implements
REST pagination itself, one explicit page-numbered request at a time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class GhError(RuntimeError):
    """Raised when the ``gh`` CLI cannot be found on ``PATH``, or a `gh`
    invocation exits non-zero while ``check=True`` (the default) -- never a
    bare ``FileNotFoundError`` or a silently-swallowed non-zero exit."""


class GhPaginationExhausted(GhError):
    """Raised by :func:`gh_paginate_rest` specifically when ``max_pages`` is
    exhausted without ever seeing a short "last page" response. This is
    distinct from the plain :class:`GhError` a single failed request raises.

    The distinction matters to callers whose correctness depends on having
    seen *every* page (e.g. ``ci.bump_ticket``'s open-issue duplicate probe):
    a single transient ``gh api`` failure is safe to treat as "couldn't
    check, fall through to creation", but exhausting the page cap means the
    result set is known-incomplete -- silently falling through there risks
    filing a duplicate (#243 round 2 blocking finding 3). Being a subclass
    of ``GhError``, a caller that only wants the old, coarser behaviour can
    still catch ``except GhError`` and get it."""


def run_gh(args: list[str], *, check: bool = True) -> str:
    """Run ``gh <args>`` and return its stdout.

    ``check=True`` (the default): a non-zero exit raises :class:`GhError`
    carrying stderr. ``check=False``: a non-zero exit returns ``""`` instead
    of raising -- callers that legitimately tolerate a `gh` failure (a
    release lookup, the labelled ``issue create`` attempt) are expected to
    treat an empty return as "didn't work" themselves.
    """
    gh_path = shutil.which("gh")
    if gh_path is None:
        raise GhError("gh CLI not found on PATH")

    result = subprocess.run(
        [gh_path, *args],
        shell=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        if check:
            raise GhError(
                f"gh {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return ""

    return result.stdout


def gh_json(args: list[str], *, check: bool = True) -> Any:
    """``run_gh`` followed by ``json.loads`` -- for the ``gh`` subcommands
    that support ``--format json`` (``gh project view``/``field-list``).
    Callers that need to tolerate an empty/failed response (e.g. a
    ``check=False`` release lookup) parse the raw string themselves instead,
    since an empty string is not valid JSON."""
    return json.loads(run_gh(args, check=check))


def gh_paginate_rest(path: str, *, per_page: int = 100, max_pages: int = 200) -> list:
    """Follow a REST list endpoint's pagination explicitly, one
    ``&page=N&per_page=<per_page>`` request at a time, extending a single
    list and stopping the moment a page comes back shorter than
    ``per_page`` (the standard "last page" signal). Raises
    :class:`GhPaginationExhausted` if ``max_pages`` is exhausted without
    ever seeing a short page -- better a loud, distinct failure than a
    silent, incomplete result.

    ``max_pages=200`` (at ``per_page=100``, 20,000 items) rather than the
    original 50 (5,000 items): this function's one known caller today
    (``ci.bump_ticket``'s open-issue duplicate probe) only ever targets
    ``Seretos/agent-project-issues`` and ``Seretos/workboard``, two repos
    extremely unlikely to ever carry 5,000+ open issues, let alone 20,000 --
    but since exhaustion here has a real cost (the caller's duplicate-avoidance
    check becomes unreliable, see :class:`GhPaginationExhausted`), a cheap 4x
    headroom bump is worth doing on top of that caller also handling the
    exhausted-cap case distinctly rather than treating it as an ordinary
    probe failure (#243 round 2 blocking finding 3)."""
    separator = "&" if "?" in path else "?"
    items: list = []

    for page in range(1, max_pages + 1):
        full_path = f"{path}{separator}page={page}&per_page={per_page}"
        chunk = json.loads(run_gh(["api", full_path]))
        if not isinstance(chunk, list):
            raise GhError(f"gh api {full_path} did not return a JSON array")

        items.extend(chunk)
        if len(chunk) < per_page:
            return items

    raise GhPaginationExhausted(
        f"gh api pagination for {path!r} did not terminate within {max_pages} pages"
    )
