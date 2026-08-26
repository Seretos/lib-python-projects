"""``ci`` -- plain-source CI helper package for #243 generation 2.

Not installed as part of the ``lib-python-projects`` distribution: it lives
at the repo root, is imported by the two local composite actions
(``.github/actions/file-consumer-ticket``, ``.github/actions/add-to-board``)
via ``python3 -m ci.<module>``, and is exercised directly by the test suite
(``pyproject.toml`` adds the repo root to ``pythonpath`` for that reason).

Invariants enforced structurally across every module in this package (see
``tests/test_ci_gh_discipline.py``):

- every ``gh`` invocation goes through the single choke point in
  :mod:`ci.gh` -- no other module spawns a child process directly;
- no CLI-side filtering/paging flags anywhere -- JSON is always parsed in
  Python, never extracted by gh's own query-string or templating flags;
- no shell execution;
- standard library only, no third-party imports.
"""

from __future__ import annotations
