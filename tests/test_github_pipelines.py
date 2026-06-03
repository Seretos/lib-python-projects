"""Tests for `list_runs_for_ticket` on the GitHub provider, specifically the
early-bail path where `ticket_id` refers to a PR rather than a plain issue.

We use `httpx.MockTransport` to intercept HTTP calls and return canned
responses; the provider is monkey-patched so `_client(token)` returns a
client backed by our mock transport.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import GitHubProvider


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Replace `github._client` so calls go through MockTransport.

    Returns a list that will be populated with every intercepted request,
    for assertion convenience.
    """
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _pr_issue_payload(number: int) -> dict:
    """An `/issues/{number}` payload that represents a PR (has `pull_request` key)."""
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "PR body",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "pull_request": {
            "url": f"https://api.github.com/repos/acme/backend/pulls/{number}",
            "html_url": f"https://github.com/acme/backend/pull/{number}",
            "merged_at": None,
        },
    }


def _pr_payload(number: int, head_sha: str) -> dict:
    """A `/pulls/{number}` payload with the given head sha."""
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "head": {
            "sha": head_sha,
            "ref": "feature-branch",
            "label": f"acme:feature-branch",
        },
        "base": {
            "sha": "base000",
            "ref": "main",
        },
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }


def _run_payload(run_id: int, head_sha: str) -> dict:
    """A minimal workflow_run payload."""
    return {
        "id": run_id,
        "name": "CI",
        "head_sha": head_sha,
        "head_branch": "feature-branch",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "html_url": f"https://github.com/acme/backend/actions/runs/{run_id}",
        "created_at": "2024-01-02T00:00:00Z",
        "updated_at": "2024-01-02T01:00:00Z",
        "run_attempt": 1,
        "display_title": "CI run",
    }


# ---------- tests ------------------------------------------------------------


def test_ticket_is_pr_returns_head_sha_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ticket_id is a PR number, resolved_refs is [head_sha] and runs are returned."""
    head_sha = "abc123def456"
    run_id = 999

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_pr_issue_payload(42))
        if path == "/repos/acme/backend/pulls/42":
            return _json(_pr_payload(42, head_sha))
        if path == "/repos/acme/backend/actions/runs":
            # Must be queried by head_sha
            assert req.url.params.get("head_sha") == head_sha
            return _json({"workflow_runs": [_run_payload(run_id, head_sha)]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    runs, resolved_refs = provider.list_runs_for_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert resolved_refs == [head_sha]
    assert len(runs) == 1
    assert runs[0].head_sha == head_sha
    assert runs[0].id == str(run_id)


def test_ticket_is_pr_with_no_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ticket_id is a PR but has no runs, resolved_refs is still non-empty."""
    head_sha = "abc123def456"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_pr_issue_payload(42))
        if path == "/repos/acme/backend/pulls/42":
            return _json(_pr_payload(42, head_sha))
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    runs, resolved_refs = provider.list_runs_for_ticket(
        _project(), token="t", ticket_id="42"
    )
    # resolved_refs must be non-empty even when there are no runs,
    # so the caller can distinguish "PR exists but no runs" from "no linked PR".
    assert resolved_refs == [head_sha]
    assert runs == []


def test_issue_ticket_skips_pr_early_bail(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ticket_id is a plain issue (no pull_request key), the PR early-bail
    is not triggered and /pulls/{id} is never requested."""
    requested_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        requested_paths.append(path)
        if path == "/repos/acme/backend/issues/42":
            # Plain issue — no `pull_request` key.
            return _json({
                "number": 42,
                "title": "Plain issue",
                "body": "no branch reference",
                "state": "open",
                "user": {"login": "alice"},
                "assignees": [],
                "labels": [],
                "html_url": "https://github.com/acme/backend/issues/42",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
            })
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if path == "/search/issues":
            return _json({"items": [], "total_count": 0})
        # The PR early-bail path must NOT be triggered for plain issues.
        if path == "/repos/acme/backend/pulls/42":
            raise AssertionError(
                "/pulls/42 was requested for a plain issue — the PR guard fired incorrectly"
            )
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    runs, resolved_refs = provider.list_runs_for_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert resolved_refs == []
    assert runs == []
    # Confirm /pulls/42 was never in any of the requests.
    assert "/repos/acme/backend/pulls/42" not in requested_paths


# ---------- Issue #17: get_run 404 naming ------------------------------------


def test_get_run_404_names_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_run that receives a 404 must re-raise naming the project and run_id."""
    from lib_python_projects.providers.github import GitHubError

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().get_run(_project(), token="t", run_id="99999")
    assert exc.value.status == 404
    assert "pipeline 'acme#99999' not found" in exc.value.message


def test_get_run_non_numeric_404_naming(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_run with a non-numeric run_id (e.g. 'main') raises 404 proactively
    without making any HTTP call."""
    from lib_python_projects.providers.github import GitHubError

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for non-numeric id")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().get_run(_project(), token="t", run_id="main")
    assert exc.value.status == 404
    assert "main" in exc.value.message


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_runs_for_branch_nonpositive_limit_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError without any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        GitHubProvider().list_runs_for_branch(
            _project(), token="t", branch="main", limit=bad_limit,
        )


# ---------- list_runs_for_branch / list_runs_for_commit tuple shape ----------


def test_list_runs_for_branch_branch_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch probe 404 → ([], [])."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "/branches/nonexistent" in req.url.path:
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="nonexistent",
    )
    assert runs == []
    assert resolved_refs == []


def test_list_runs_for_branch_no_runs_no_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch resolves, no runs, no workflows → ([], [sha, 'no-ci'])."""
    sha = "aabbccdd1234"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/branches/feat" in path:
            return _json({"commit": {"sha": sha}})
        if path.endswith("/actions/runs"):
            return _json({"workflow_runs": []})
        if path.endswith("/actions/workflows"):
            return _json({"total_count": 0, "workflows": []})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="feat",
    )
    assert runs == []
    assert resolved_refs == [sha, "no-ci"]


def test_list_runs_for_branch_no_runs_ci_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch resolves, no runs, but workflows exist → ([], [sha])."""
    sha = "aabbccdd1234"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/branches/feat" in path:
            return _json({"commit": {"sha": sha}})
        if path.endswith("/actions/runs"):
            return _json({"workflow_runs": []})
        if path.endswith("/actions/workflows"):
            return _json({"total_count": 2, "workflows": [{}, {}]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="feat",
    )
    assert runs == []
    assert resolved_refs == [sha]


def test_list_runs_for_branch_with_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch resolves and runs exist → (runs, [sha])."""
    sha = "aabbccdd1234"
    run_id = 7777

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/branches/main" in path:
            return _json({"commit": {"sha": sha}})
        if path.endswith("/actions/runs"):
            return _json({"workflow_runs": [_run_payload(run_id, sha)]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="main",
    )
    assert len(runs) == 1
    assert runs[0].id == str(run_id)
    assert resolved_refs == [sha]


def test_list_runs_for_commit_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit probe 404 → ([], [])."""
    sha = "deadbeef"

    def handler(req: httpx.Request) -> httpx.Response:
        if f"/commits/{sha}" in req.url.path:
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_commit(
        _project(), token="t", sha=sha,
    )
    assert runs == []
    assert resolved_refs == []


def test_list_runs_for_commit_found_with_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit found, one run → ([run], [sha])."""
    sha = "cafebabe1234"
    run_id = 8888

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if f"/commits/{sha}" in path:
            return _json({"sha": sha})
        if path.endswith("/actions/runs"):
            return _json({"workflow_runs": [_run_payload(run_id, sha)]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_commit(
        _project(), token="t", sha=sha,
    )
    assert len(runs) == 1
    assert runs[0].head_sha == sha
    assert resolved_refs == [sha]


def test_list_runs_for_commit_found_no_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit found, no runs → ([], [sha])."""
    sha = "cafebabe1234"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if f"/commits/{sha}" in path:
            return _json({"sha": sha})
        if path.endswith("/actions/runs"):
            return _json({"workflow_runs": []})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_commit(
        _project(), token="t", sha=sha,
    )
    assert runs == []
    assert resolved_refs == [sha]


# ---------- Ticket #57: PL5 — list_runs_for_branch 301 returns empty ----------


def test_list_runs_for_branch_301_returns_empty_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the actions/runs endpoint returns 301 (redirect), _list_runs_for_branch
    must return [] instead of raising GitHubError, so list_runs_for_branch returns
    the correct ([], [sha]) sentinel rather than leaking '301 Moved Permanently'."""
    from lib_python_projects.providers.github import GitHubError

    sha = "deadbeef1234"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Branch resolves successfully.
        if "/branches/master" in path:
            return _json({"commit": {"sha": sha}})
        # Actions/runs endpoint returns a redirect.
        if path.endswith("/actions/runs"):
            return httpx.Response(
                status_code=301,
                headers={"Location": "https://api.github.com/other"},
                content=b"",
            )
        # workflows check — called when runs list is empty.
        if path.endswith("/actions/workflows"):
            return _json({"total_count": 1, "workflows": [{}]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    # Must not raise GitHubError.
    runs, resolved_refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="master",
    )
    assert runs == []
    # branch was found, so resolved_refs must be non-empty.
    assert sha in resolved_refs


# ---------- list_runs_recent -------------------------------------------------


def test_list_runs_recent_sends_no_branch_or_head_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unfiltered call sends neither `branch` nor `head_sha`, but does set `per_page`."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["params"] = dict(req.url.params)
        return _json({"workflow_runs": []})

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_recent(_project(), token="t")
    assert "branch" not in captured["params"]
    assert "head_sha" not in captured["params"]
    assert "per_page" in captured["params"]
    assert resolved_refs == []
    assert runs == []


def test_list_runs_recent_status_all_omits_status_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status='all'` must not send a `status` query param."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["params"] = dict(req.url.params)
        return _json({"workflow_runs": []})

    _install_mock(monkeypatch, handler)
    GitHubProvider().list_runs_recent(_project(), token="t", status="all")
    assert "status" not in captured["params"]


def test_list_runs_recent_status_completed_sends_status_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status='completed'` must send `status=completed` in the query."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["params"] = dict(req.url.params)
        return _json({"workflow_runs": []})

    _install_mock(monkeypatch, handler)
    GitHubProvider().list_runs_recent(_project(), token="t", status="completed")
    assert captured["params"].get("status") == "completed"


def test_list_runs_recent_returns_mapped_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returned runs are mapped PipelineRun objects; resolved_refs is []."""
    run_id = 42
    sha = "cafe1234"

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"workflow_runs": [_run_payload(run_id, sha)]})

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_recent(_project(), token="t")
    assert resolved_refs == []
    assert len(runs) == 1
    assert runs[0].id == str(run_id)


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_runs_recent_nonpositive_limit_raises_before_http(
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """limit <= 0 must raise ValueError without any HTTP call."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for limit={bad_limit}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="positive integer"):
        GitHubProvider().list_runs_recent(
            _project(), token="t", limit=bad_limit,
        )


# ---------- _extract_log_excerpt pure-function tests (ticket #76) ------------


def _make_log(*sections: str) -> str:
    """Join log sections with newlines."""
    return "\n".join(sections)


def test_extract_log_excerpt_step_named_run_prefix_matches() -> None:
    """Regression test for ticket #76.

    The GitHub Jobs API returns step names like ``"Run python -m pytest tests -v"``
    while the log group header is ``"##[group]Run python -m pytest tests -v"``.
    Previously the target was compared as-is, producing
    ``"run python -m pytest tests -v"`` vs the captured group name
    ``"python -m pytest tests -v"`` — a mismatch that caused the function to
    fall through to the head-of-log fallback.

    After the fix, the leading ``"Run "`` prefix is stripped from the
    casefolded target before comparison, so the group is found correctly
    and the excerpt contains the error line.
    """
    from lib_python_projects.providers.github import _extract_log_excerpt

    log = _make_log(
        "2024-01-01T00:00:00.000Z ##[group]Set up job",
        "2024-01-01T00:00:01.000Z Setting up runner",
        "2024-01-01T00:00:02.000Z ##[endgroup]",
        "2024-01-01T00:00:03.000Z ##[group]Run python -m pytest tests -v",
        "2024-01-01T00:00:04.000Z /usr/bin/python: No module named pytest",
        "2024-01-01T00:00:05.000Z ##[endgroup]",
        "2024-01-01T00:00:06.000Z Post step cleanup",
    )
    result = _extract_log_excerpt(log, failed_step="Run python -m pytest tests -v")
    assert "No module named pytest" in result


def test_extract_log_excerpt_step_name_without_run_prefix() -> None:
    """A step name without the ``"Run "`` prefix also finds the group."""
    from lib_python_projects.providers.github import _extract_log_excerpt

    log = _make_log(
        "2024-01-01T00:00:03.000Z ##[group]Run python -m pytest tests -v",
        "2024-01-01T00:00:04.000Z /usr/bin/python: No module named pytest",
        "2024-01-01T00:00:05.000Z ##[endgroup]",
    )
    result = _extract_log_excerpt(log, failed_step="python -m pytest tests -v")
    assert "No module named pytest" in result


def test_extract_log_excerpt_step_casefold() -> None:
    """Matching is case-insensitive for both the step name and the group name."""
    from lib_python_projects.providers.github import _extract_log_excerpt

    log = _make_log(
        "##[group]Run Python -m Pytest Tests -v",
        "Error: something went wrong",
        "##[endgroup]",
    )
    result = _extract_log_excerpt(log, failed_step="RUN PYTHON -M PYTEST TESTS -V")
    assert "Error: something went wrong" in result


def test_extract_log_excerpt_error_marker_preferred_over_generic() -> None:
    """The two-pass scan must prefer ``##[error]`` over a generic ``error`` match.

    A generic ``error`` keyword appears in a setup section (after the first
    group opens) and a ``##[error]`` line appears later.  The excerpt must
    be anchored at the ``##[error]`` line, not the earlier generic match.
    """
    from lib_python_projects.providers.github import _extract_log_excerpt

    log = _make_log(
        "##[group]Set up job",
        "echo error suppressed",           # generic 'error' in setup — should be skipped
        "##[endgroup]",
        "Running tests",
        "##[error]Process completed with exit code 1",   # specific marker — should win
        "Post step",
    )
    # No failed_step so we fall through to the substring scan.
    result = _extract_log_excerpt(log)
    assert "##[error]Process completed with exit code 1" in result
    # The generic 'echo error suppressed' line must NOT be the anchor.
    # If it were, the excerpt would start at or before line 2 and the
    # ##[error] line would also happen to be included only by coincidence;
    # we verify the marker line IS present (two-pass chose it as anchor).
    assert "##[error]" in result
    # Explicitly assert the generic line is NOT what drove the excerpt anchor:
    # the excerpt must not start at (or before) the generic-match line.
    # We check this by confirming "echo error suppressed" is absent from the
    # result — the two-pass logic skips it in favour of ##[error].
    assert "echo error suppressed" not in result


def test_extract_log_excerpt_tail_fallback_returns_tail_not_head() -> None:
    """When no groups and no error keywords exist, the fallback must return
    the TAIL of the log, not the head."""
    from lib_python_projects.providers.github import _extract_log_excerpt

    # 40 lines, none containing 'error'/'failed'/groups.
    lines = [f"line-{i:02d}" for i in range(40)]
    log = "\n".join(lines)
    result = _extract_log_excerpt(log, max_lines=10)
    result_lines = result.splitlines()
    # First returned line must NOT be the very first log line.
    assert result_lines[0] != "line-00"
    # The last log line must be present (tail).
    assert "line-39" in result


def test_extract_log_excerpt_empty_log_returns_empty_string() -> None:
    """Empty string input returns an empty string without raising."""
    from lib_python_projects.providers.github import _extract_log_excerpt

    assert _extract_log_excerpt("") == ""


# ---------- HTTP-level tests for get_run / tail_lines (ticket #76) -----------


def _failed_run_payload(run_id: int, head_sha: str) -> dict:
    """A completed failed workflow_run payload."""
    return {
        "id": run_id,
        "name": "CI",
        "head_sha": head_sha,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "html_url": f"https://github.com/acme/backend/actions/runs/{run_id}",
        "created_at": "2024-01-02T00:00:00Z",
        "updated_at": "2024-01-02T01:00:00Z",
        "run_attempt": 1,
        "display_title": "CI run",
    }


def _jobs_payload(job_id: int, job_name: str = "test", failed_step_name: str = "Run pytest") -> dict:
    """A /jobs response with one failed job."""
    return {
        "jobs": [
            {
                "id": job_id,
                "name": job_name,
                "conclusion": "failure",
                "html_url": f"https://github.com/acme/backend/actions/runs/1/jobs/{job_id}",
                "check_run_url": None,
                "steps": [
                    {
                        "name": failed_step_name,
                        "conclusion": "failure",
                        "number": 1,
                    }
                ],
            }
        ]
    }


def test_get_run_tail_lines_overrides_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_run(..., tail_lines=5) on a failed run must set log_excerpt to the
    last 5 lines of the job log, ignoring the smart-excerpt heuristics."""
    run_id = 12345
    job_id = 99
    head_sha = "abc123"

    # Build a 20-line job log whose last 5 lines are distinct sentinel values.
    log_lines = [f"setup-line-{i}" for i in range(15)] + [
        "TAIL-LINE-A",
        "TAIL-LINE-B",
        "TAIL-LINE-C",
        "TAIL-LINE-D",
        "TAIL-LINE-E",
    ]
    log_text = "\n".join(log_lines)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/repos/acme/backend/actions/runs/{run_id}":
            return _json(_failed_run_payload(run_id, head_sha))
        if path == f"/repos/acme/backend/actions/runs/{run_id}/jobs":
            return _json(_jobs_payload(job_id))
        raise AssertionError(f"unexpected JSON request: {req.url}")

    _install_mock(monkeypatch, handler)

    # Patch _fetch_job_log to avoid a real HTTP call (it uses its own client).
    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log",
        lambda token, url, *, max_bytes=256 * 1024: log_text,
    )

    run = GitHubProvider().get_run(
        _project(), token="t", run_id=str(run_id), tail_lines=5
    )
    assert run.failure is not None
    assert len(run.failure.failing_jobs) == 1
    excerpt = run.failure.failing_jobs[0].log_excerpt
    assert excerpt is not None
    excerpt_lines = excerpt.splitlines()
    assert excerpt_lines == ["TAIL-LINE-A", "TAIL-LINE-B", "TAIL-LINE-C", "TAIL-LINE-D", "TAIL-LINE-E"]


def test_get_run_tail_lines_bypasses_256kb_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_run(..., tail_lines=N) must return the true last N lines of the full
    log, even when the log exceeds 256 KB.

    The default _fetch_job_log caps the response body at 256 KB.  When
    tail_lines is set the cap must be removed so that the sentinel lines
    sitting beyond the 256 KB boundary are reachable.
    """
    run_id = 22222
    job_id = 88
    head_sha = "cafe1234"

    # Build a log whose total byte size exceeds 256 KB.
    # Pad the front with lines that fill > 256 KB, then append 3 distinct
    # sentinel lines at the very end.
    padding_line = "x" * 200          # 200 bytes + newline = 201 bytes each
    # 1400 lines × 201 bytes ≈ 281 KB — safely over the 256 KB boundary.
    padding_lines = [padding_line] * 1400
    tail_sentinels = ["OVER-CAP-LINE-1", "OVER-CAP-LINE-2", "OVER-CAP-LINE-3"]
    all_lines = padding_lines + tail_sentinels
    full_log_text = "\n".join(all_lines)
    # Sanity-check: the full log is indeed larger than 256 KB.
    assert len(full_log_text.encode("utf-8")) > 256 * 1024

    # Track which max_bytes value _fetch_job_log was called with.
    called_max_bytes: list = []

    def fake_fetch(token: str | None, url: str, *, max_bytes: int | None = 256 * 1024) -> str:
        called_max_bytes.append(max_bytes)
        # Honour the cap so we can verify the UNCAPPED path returns sentinels.
        if max_bytes is not None:
            return full_log_text.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")
        return full_log_text

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/repos/acme/backend/actions/runs/{run_id}":
            return _json(_failed_run_payload(run_id, head_sha))
        if path == f"/repos/acme/backend/actions/runs/{run_id}/jobs":
            return _json(_jobs_payload(job_id))
        raise AssertionError(f"unexpected JSON request: {req.url}")

    _install_mock(monkeypatch, handler)
    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log",
        fake_fetch,
    )

    run = GitHubProvider().get_run(
        _project(), token="t", run_id=str(run_id), tail_lines=3
    )
    assert run.failure is not None
    assert len(run.failure.failing_jobs) == 1
    excerpt = run.failure.failing_jobs[0].log_excerpt
    assert excerpt is not None

    # The excerpt must be the true last 3 lines — sitting beyond 256 KB.
    excerpt_lines = excerpt.splitlines()
    assert excerpt_lines == tail_sentinels, (
        f"Expected tail sentinels {tail_sentinels!r}, got {excerpt_lines!r}. "
        "This means the 256 KB cap was NOT bypassed for the tail_lines path."
    )
    # Confirm _fetch_job_log was called with max_bytes=None (cap removed).
    assert called_max_bytes == [None], (
        f"Expected _fetch_job_log to be called with max_bytes=None, got {called_max_bytes!r}"
    )


def test_get_run_failure_excerpt_no_module_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end test mirroring the ticket scenario.

    A failed run whose job log has a ``##[group]Run python -m pytest tests -v``
    block containing ``No module named pytest`` on stderr; the step name in the
    Jobs API response is ``"Run python -m pytest tests -v"``.  After the fix,
    ``log_excerpt`` must contain the error line rather than the log head.
    """
    run_id = 56789
    job_id = 77
    head_sha = "deadbeef"
    failed_step = "Run python -m pytest tests -v"

    log_text = "\n".join([
        "2024-01-02T00:00:00.000Z ##[group]Set up job",
        "2024-01-02T00:00:01.000Z Initializing runner",
        "2024-01-02T00:00:02.000Z ##[endgroup]",
        f"2024-01-02T00:00:03.000Z ##[group]Run python -m pytest tests -v",
        "2024-01-02T00:00:04.000Z /usr/bin/python: No module named pytest",
        "2024-01-02T00:00:05.000Z ##[endgroup]",
        "2024-01-02T00:00:06.000Z ##[error]Process completed with exit code 1",
        "2024-01-02T00:00:07.000Z Post step: Set up job",
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/repos/acme/backend/actions/runs/{run_id}":
            return _json(_failed_run_payload(run_id, head_sha))
        if path == f"/repos/acme/backend/actions/runs/{run_id}/jobs":
            return _json(_jobs_payload(job_id, failed_step_name=failed_step))
        raise AssertionError(f"unexpected JSON request: {req.url}")

    _install_mock(monkeypatch, handler)
    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log",
        lambda token, url, *, max_bytes=256 * 1024: log_text,
    )

    run = GitHubProvider().get_run(_project(), token="t", run_id=str(run_id))
    assert run.failure is not None
    assert len(run.failure.failing_jobs) == 1
    job = run.failure.failing_jobs[0]
    assert job.log_excerpt is not None
    assert "No module named pytest" in job.log_excerpt
