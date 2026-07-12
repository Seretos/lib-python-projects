"""In-memory idempotency-key store for `create_ticket` / `create_pr` (ticket #150).

When an agent retries `create_ticket` / `create_pr` after a timeout,
network error, or ambiguous response, there's normally no way to detect
the repeat — a naive retry creates a duplicate ticket/PR. Passing the
same `idempotency_key` on the retry short-circuits to the object created
by the first (successful) call instead.

Mirrors the `_http_cache.py` pattern: a module-level dict guarded by a
`threading.Lock`, plus a `clear_idempotency_cache()` test hook. No
persistence, no external deps — this is purely a same-process safety net.

Concurrency note: this only protects *sequential* retries (the common
timeout/network-error/ambiguous-response case). `lookup()` releases the
lock before returning on a fresh (never-before-seen) key, so two
literally-simultaneous calls with the same new key can both pass the
lookup, both hit the provider API, and both call `record()` (last write
wins) — two real objects get created, not deduplicated. Callers that need
true concurrent-duplicate-call safety must add their own locking/coalescing
on top of this module.
"""
from __future__ import annotations

import dataclasses
import os
import threading
import time
from typing import Any

from lib_python_projects.providers.base import IdempotencyConflict

# Default TTL: 24 hours. An entry older than this is treated as absent —
# a retry past the window creates a fresh object rather than replaying a
# (likely stale / no-longer-relevant) cached result.
_DEFAULT_TTL_SECONDS = 86400

_ENV_VAR = "PROJECT_ISSUES_IDEMPOTENCY_TTL_SECONDS"


@dataclasses.dataclass
class _IdemEntry:
    """One cached create_ticket/create_pr result."""

    result: Any
    core_args: dict[str, str]
    created_monotonic: float


# ---------- module-level thread-safe store -----------------------------------

_store: dict[tuple[str, str, str], _IdemEntry] = {}
_lock = threading.Lock()


def clear_idempotency_cache() -> None:
    """Test hook: drain the in-memory idempotency store."""
    with _lock:
        _store.clear()


def _ttl_seconds() -> int:
    """Read the env-configurable TTL (seconds).

    `PROJECT_ISSUES_IDEMPOTENCY_TTL_SECONDS` semantics:
      - unset / empty          -> 86400 (24h) default
      - non-integer            -> 86400 default
      - integer <= 0           -> 86400 default (an unbounded/zero TTL has
        no sane meaning here, so it's treated as invalid input rather than
        honored — a deliberate choice for this module, not the same rule
        `_mentions_scan_depth` applies to its own out-of-range values)
      - positive integer N     -> N
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None or raw == "":
        return _DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS
    if value <= 0:
        return _DEFAULT_TTL_SECONDS
    return value


def record(
    namespace: tuple[str, str],
    idempotency_key: str,
    core_args: dict[str, str],
    result: Any,
) -> None:
    """Cache a successfully-created `result` under `(namespace, idempotency_key)`.

    `namespace` is `(project.provider, project.id)` — callers pass it
    pre-built so this module stays provider-agnostic. Only call this
    after the create fully succeeds (including any follow-up steps) so
    the cached `result` is the exact object the non-replay path returns.
    """
    key = (namespace[0], namespace[1], idempotency_key)
    entry = _IdemEntry(
        result=result, core_args=dict(core_args), created_monotonic=time.monotonic(),
    )
    with _lock:
        _store[key] = entry


def lookup(
    namespace: tuple[str, str],
    idempotency_key: str,
    core_args: dict[str, str],
) -> Any | None:
    """Return the cached result for a replayed call, or None on a fresh key.

    Raises `IdempotencyConflict` when the key was already used with
    different `core_args`. Lazily evicts an expired entry (per the TTL)
    and treats it as absent (returns None), so a retry past the window
    creates a fresh object.
    """
    key = (namespace[0], namespace[1], idempotency_key)
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.created_monotonic > _ttl_seconds():
            del _store[key]
            return None
    if entry.core_args != core_args:
        raise IdempotencyConflict(idempotency_key, entry.core_args, core_args)
    return dataclasses.replace(entry.result, idempotent_replay=True)
