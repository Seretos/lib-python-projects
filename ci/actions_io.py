"""GitHub Actions workflow-command helpers: ``::warning::``/``::error::``
annotations, ``$GITHUB_OUTPUT`` writing, and a small ``run_main`` wrapper
that turns a handled :class:`ScriptError` (or any genuinely unexpected
exception) into an ``::error::`` annotation plus exit code 1, for the
``python3 -m ci.<module>`` entry points.
"""

from __future__ import annotations

from typing import Callable


class ScriptError(RuntimeError):
    """A handled, reportable failure raised by a ``ci`` module. ``run_main``
    maps it to an ``::error::`` annotation and exit code 1."""


def warn(msg: str) -> None:
    print(f"::warning::{msg}")


def error(msg: str) -> None:
    print(f"::error::{msg}")


def set_output(name: str, value: str, env: dict[str, str]) -> None:
    """Append ``name=value`` to the file at ``env["GITHUB_OUTPUT"]``, the
    way a GitHub Actions step reports an output. ``value`` must not contain
    a newline -- ``$GITHUB_OUTPUT``'s ``name=value`` line format has no
    escaping for one, so a raw ``\\n``/``\\r`` would corrupt the file (and
    could inject an unrelated extra output)."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"set_output value for {name!r} must not contain newlines: {value!r}")

    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def run_main(fn: Callable[[], int]) -> int:
    """Run ``fn`` (a module's ``run(os.environ)`` call), mapping a handled
    :class:`ScriptError` -- or any other unexpected exception -- to an
    ``::error::`` annotation and exit code 1, instead of an unhandled
    traceback."""
    try:
        return fn()
    except ScriptError as exc:
        error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 -- last-resort safety net for python3 -m entry points
        error(f"unexpected error: {exc}")
        return 1
