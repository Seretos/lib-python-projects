"""Driving tests for `wait_for_pipeline` (ticket #275).

The three providers share one blocking implementation that polls
`list_runs_for_commit` to a verdict. All tests drive the real provider
methods through `httpx.MockTransport`; `now`/`sleep` are injected so no
test consumes wall time.
"""
from __future__ import annotations

import json

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as ado_provider
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers import gitlab as gitlab_provider
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider, _basic_auth_header
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider

SHA = "a" * 40


def _resp(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class FakeClock:
    """Injected `now`/`sleep`: sleeping advances the clock, nothing blocks."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class Script:
    """Per-poll scripted responses; the last entry repeats forever."""

    def __init__(self, polls: list) -> None:
        self.polls = polls
        self.count = 0

    def next(self):
        item = self.polls[min(self.count, len(self.polls) - 1)]
        self.count += 1
        return item


# ---------- GitHub -----------------------------------------------------------


def _gh_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="github", path="acme/backend", token_env="GITHUB_TOKEN_ACME"
    )


def _gh_run(run_id: int, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {
        "id": run_id,
        "name": f"wf{run_id}",
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/acme/backend/actions/runs/{run_id}",
        "created_at": "2026-08-21T10:00:00Z",
        "updated_at": "2026-08-21T10:00:00Z",
        "run_attempt": 1,
        "display_title": "CI",
    }


def _install_github(monkeypatch, script: Script, *, workflows: int = 1) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith(f"/commits/{SHA}"):
            return _resp({"sha": SHA})
        if path.endswith("/actions/runs"):
            return _resp({"workflow_runs": script.next()})
        if path.endswith("/actions/workflows"):
            return _resp({"total_count": workflows, "workflows": [{"id": 1}] * workflows})
        raise AssertionError(f"unexpected request: {req.url}")

    transport = httpx.MockTransport(handler)

    def fake_client(token):
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers={"Accept": "application/vnd.github+json"},
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)


def _wait_gh(clock: FakeClock, **kw):
    kw.setdefault("timeout_s", 600.0)
    kw.setdefault("poll_interval_s", 20.0)
    return GitHubProvider().wait_for_pipeline(
        _gh_project(), "t", SHA, now=clock.now, sleep=clock.sleep, **kw
    )


def test_first_poll_green_returns_success_without_sleeping(monkeypatch):
    script = Script([[_gh_run(1)]])
    _install_github(monkeypatch, script)
    clock = FakeClock()
    result = _wait_gh(clock)
    assert result.state == "success"
    assert result.waited_s == 0
    assert clock.sleeps == []
    assert script.count == 1
    assert [r.id for r in result.runs] == ["1"]


def test_queued_then_in_progress_then_success_after_three_polls(monkeypatch):
    script = Script([
        [_gh_run(1, "queued", None)],
        [_gh_run(1, "in_progress", None)],
        [_gh_run(1)],
    ])
    _install_github(monkeypatch, script)
    clock = FakeClock()
    result = _wait_gh(clock, poll_interval_s=20.0)
    assert result.state == "success"
    assert script.count == 3
    assert clock.sleeps == [20.0, 20.0]
    assert result.waited_s == pytest.approx(40.0)


def test_failure_returned_immediately_while_other_run_in_progress(monkeypatch):
    script = Script([[_gh_run(1, "completed", "failure"), _gh_run(2, "in_progress", None)]])
    _install_github(monkeypatch, script)
    clock = FakeClock()
    result = _wait_gh(clock)
    assert result.state == "failure"
    assert clock.sleeps == []
    assert script.count == 1
    assert {r.id for r in result.runs} == {"1", "2"}


def test_timed_out_conclusion_is_failure(monkeypatch):
    _install_github(monkeypatch, Script([[_gh_run(1, "completed", "timed_out")]]))
    assert _wait_gh(FakeClock()).state == "failure"


def test_in_progress_past_timeout_returns_pending_within_one_interval(monkeypatch):
    script = Script([[_gh_run(1, "in_progress", None)]])
    _install_github(monkeypatch, script)
    clock = FakeClock()
    result = _wait_gh(clock, timeout_s=50.0, poll_interval_s=20.0)
    assert result.state == "pending"
    assert result.waited_s <= 50.0
    assert clock.sleeps  # it did wait
    assert sum(clock.sleeps) <= 50.0
    assert all(s <= 20.0 for s in clock.sleeps)


def test_poll_interval_below_floor_is_clamped_to_five_seconds(monkeypatch):
    _install_github(monkeypatch, Script([[_gh_run(1, "in_progress", None)]]))
    clock = FakeClock()
    _wait_gh(clock, timeout_s=30.0, poll_interval_s=1.0)
    assert clock.sleeps
    assert min(clock.sleeps) >= 5.0


def test_interval_larger_than_timeout_still_returns_within_timeout(monkeypatch):
    _install_github(monkeypatch, Script([[_gh_run(1, "in_progress", None)]]))
    clock = FakeClock()
    result = _wait_gh(clock, timeout_s=10.0, poll_interval_s=300.0)
    assert result.state == "pending"
    assert result.waited_s <= 10.0


def test_no_runs_only_after_full_timeout(monkeypatch):
    script = Script([[]])
    _install_github(monkeypatch, script, workflows=1)
    clock = FakeClock()
    result = _wait_gh(clock, timeout_s=60.0, poll_interval_s=20.0)
    assert result.state == "no_runs"
    assert result.runs == []
    assert script.count > 1
    assert result.waited_s >= 60.0 - 1e-9


def test_run_appearing_after_empty_polls_is_picked_up(monkeypatch):
    script = Script([[], [], [_gh_run(1)]])
    _install_github(monkeypatch, script)
    result = _wait_gh(FakeClock())
    assert result.state == "success"
    assert script.count == 3


def test_no_ci_sentinel_returns_no_runs_immediately(monkeypatch):
    script = Script([[]])
    _install_github(monkeypatch, script, workflows=0)
    clock = FakeClock()
    result = _wait_gh(clock)
    assert result.state == "no_runs"
    assert clock.sleeps == []
    assert script.count == 1


# ---------- cancelled / skipped => no_verdict, all providers ------------------


def _gl_project() -> ProjectConfig:
    return ProjectConfig(id="gitlab-tests", provider="gitlab", path="acme/backend")


def _gl_pipeline(pid: int, status: str) -> dict:
    return {
        "id": pid,
        "ref": "main",
        "sha": SHA,
        "source": "push",
        "status": status,
        "web_url": f"https://gitlab.com/acme/backend/-/pipelines/{pid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:05:00Z",
    }


def _install_gitlab(monkeypatch, statuses: list[str]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/repository/commits/" in url:
            return _resp({"id": SHA})
        if "/pipelines" in url:
            return _resp([_gl_pipeline(i + 1, s) for i, s in enumerate(statuses)])
        raise AssertionError(f"unexpected request: {url}")

    transport = httpx.MockTransport(handler)

    def fake_client(project, token):
        return httpx.Client(
            base_url="https://gitlab.com/api/v4",
            headers={"Accept": "application/json"},
            transport=transport,
        )

    monkeypatch.setattr(gitlab_provider, "_client", fake_client)


def _ado_project() -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests", provider="azuredevops",
        path="seredos/azure-tests/azure-tests", token_env="AZURE_TOKEN",
    )


def _ado_build(bid: int, status: str = "completed", result: str | None = "succeeded") -> dict:
    b = {
        "id": bid,
        "definition": {"name": "CI"},
        "sourceBranch": "refs/heads/main",
        "sourceVersion": SHA,
        "status": status,
        "queueTime": "2026-05-18T10:00:00Z",
        "finishTime": "2026-05-18T10:05:00Z",
        "_links": {"web": {"href": f"https://example/builds/{bid}"}},
    }
    if result is not None:
        b["result"] = result
    return b


def _install_ado(monkeypatch, builds: list[dict]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/_apis/build/builds"):
            return _resp({"value": builds})
        raise AssertionError(f"unexpected request: {req.url}")

    transport = httpx.MockTransport(handler)

    def fake_client(project, token):
        return httpx.Client(
            base_url="https://dev.azure.com",
            headers={"Accept": "application/json", "Authorization": _basic_auth_header("t")},
            transport=transport,
        )

    monkeypatch.setattr(ado_provider, "_client", fake_client)


def _run_wait(provider, project, clock: FakeClock):
    return provider.wait_for_pipeline(
        project, "t", SHA, timeout_s=600.0, poll_interval_s=20.0,
        now=clock.now, sleep=clock.sleep,
    )


_GH_CASES = [
    ("cancelled", [_gh_run(1, "completed", "cancelled")], "no_verdict"),
    ("skipped", [_gh_run(1, "completed", "skipped")], "no_verdict"),
    ("green+cancelled", [_gh_run(1), _gh_run(2, "completed", "cancelled")], "no_verdict"),
    (
        "red+cancelled",
        [_gh_run(1, "completed", "failure"), _gh_run(2, "completed", "cancelled")],
        "failure",
    ),
]


@pytest.mark.parametrize(
    "runs,expected", [c[1:] for c in _GH_CASES], ids=[c[0] for c in _GH_CASES]
)
def test_github_cancelled_or_skipped_yields_no_verdict(monkeypatch, runs, expected):
    _install_github(monkeypatch, Script([runs]))
    clock = FakeClock()
    result = _wait_gh(clock)
    assert result.state == expected
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["canceled"], "no_verdict"),
        (["skipped"], "no_verdict"),
        (["success", "canceled"], "no_verdict"),
        (["failed", "canceled"], "failure"),
        (["failed"], "failure"),
    ],
)
def test_gitlab_cancelled_or_skipped_yields_no_verdict(monkeypatch, statuses, expected):
    _install_gitlab(monkeypatch, statuses)
    clock = FakeClock()
    result = _run_wait(GitLabProvider(), _gl_project(), clock)
    assert result.state == expected
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "builds,expected",
    [
        ([_ado_build(1, result="canceled")], "no_verdict"),
        ([_ado_build(1, result=None)], "no_verdict"),
        ([_ado_build(1), _ado_build(2, result="canceled")], "no_verdict"),
        ([_ado_build(1, result="failed"), _ado_build(2, result="canceled")], "failure"),
    ],
    ids=["canceled", "completed-without-result", "green+canceled", "red+canceled"],
)
def test_azuredevops_cancelled_or_skipped_yields_no_verdict(monkeypatch, builds, expected):
    _install_ado(monkeypatch, builds)
    clock = FakeClock()
    result = _run_wait(AzureDevOpsProvider(), _ado_project(), clock)
    assert result.state == expected
    assert clock.sleeps == []
