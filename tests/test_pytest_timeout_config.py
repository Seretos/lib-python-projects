"""Behavioural coverage for the per-test pytest-timeout configuration.

Ticket #199: the suite previously had no per-test timeout, so a single
blocked test (a mock falling through to a real socket, a client built
without a timeout, ...) could hang the entire run indefinitely. These tests
verify that a thread-based timeout is actually wired up and actually kills
blocked tests, using pytest's own `pytester` fixture to run a throwaway
child suite in a subprocess.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(30)


def test_blocking_test_is_killed_by_timeout(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(
        """
        [tool.pytest.ini_options]
        timeout = 1
        timeout_method = "thread"
        """
    )
    pytester.makepyfile(
        """
        import time

        def test_hangs_forever():
            time.sleep(20)
        """
    )

    import time as _time

    start = _time.monotonic()
    result = pytester.runpytest_subprocess(timeout=30)
    elapsed = _time.monotonic() - start

    result.stdout.fnmatch_lines(["*Timeout*"])
    assert result.ret != 0
    assert elapsed < 15, f"expected the timeout to kill the test well under 20s, took {elapsed}s"


def test_blocking_socket_recv_is_killed_by_timeout(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(
        """
        [tool.pytest.ini_options]
        timeout = 1
        timeout_method = "thread"
        """
    )
    pytester.makepyfile(
        """
        import socket

        def test_blocks_on_socket_recv():
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            addr = listener.getsockname()

            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(addr)
            conn, _ = listener.accept()

            # Nobody ever sends anything: this call blocks until killed.
            client.recv(1024)
        """
    )

    result = pytester.runpytest_subprocess(timeout=30)

    result.stdout.fnmatch_lines(["*Timeout*"])
    assert result.ret != 0


def test_pyproject_declares_timeout_settings() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    ini_options = data["tool"]["pytest"]["ini_options"]
    assert ini_options["timeout"] == 60
    assert ini_options["timeout_method"] == "thread"

    test_extra = data["project"]["optional-dependencies"]["test"]
    assert any(dep.startswith("pytest-timeout") for dep in test_extra)

    runtime_deps = data["project"]["dependencies"]
    assert not any(dep.startswith("pytest-timeout") for dep in runtime_deps)


def test_timeout_ini_is_active_in_this_session(pytestconfig: pytest.Config) -> None:
    assert float(pytestconfig.getini("timeout")) == 60.0
    assert pytestconfig.getini("timeout_method") == "thread"


def test_fast_test_is_not_affected_by_timeout(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(
        """
        [tool.pytest.ini_options]
        timeout = 1
        timeout_method = "thread"
        """
    )
    pytester.makepyfile(
        """
        def test_trivially_fast():
            assert 1 + 1 == 2
        """
    )

    result = pytester.runpytest_subprocess(timeout=30)

    assert result.ret == 0
