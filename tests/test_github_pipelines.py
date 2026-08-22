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
from lib_python_projects.providers.github import GitHubError, GitHubProvider


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
        if path == "/repos/acme/backend/actions/workflows":
            # CI configured (ticket #209) — no runs matched, but the
            # repository does have a workflow, so no "no-ci" sentinel.
            return _json({"total_count": 1, "workflows": [{"id": 1, "path": ".github/workflows/ci.yml"}]})
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


def test_ticket_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine 404 on the initial issue fetch must raise `GitHubError`,
    not collapse into the same `([], [])` result as "ticket exists but
    nothing linked" (issue #135)."""
    from lib_python_projects.providers.github import GitHubError

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/999999":
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    with pytest.raises(GitHubError) as exc:
        provider.list_runs_for_ticket(_project(), token="t", ticket_id="999999")
    assert exc.value.status == 404


def test_ticket_is_pr_but_pr_fetch_fails_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PR early-bail branch must keep returning `[]` (not raise) when
    the ticket resolves fine (issue fetch succeeds, `pull_request` key is
    present) but the follow-up `/pulls/{id}` fetch fails — this is a
    "no resolvable head sha" case, distinct from "ticket missing"."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_pr_issue_payload(42))
        if path == "/repos/acme/backend/pulls/42":
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    runs, resolved_refs = provider.list_runs_for_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert resolved_refs == []
    assert runs == []


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
        if path.endswith("/actions/workflows"):
            # CI configured (ticket #209) — no runs matched, but the
            # repository does have a workflow, so no "no-ci" sentinel.
            return _json({"total_count": 1, "workflows": [{"id": 1, "path": ".github/workflows/ci.yml"}]})
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
        if req.url.path.endswith("/actions/runs"):
            captured["params"] = dict(req.url.params)
        # ticket #209: no runs matched, so `list_runs_recent` probes
        # `/actions/workflows` — report CI as configured so no "no-ci"
        # sentinel lands in `resolved_refs`, matching this test's
        # existing `resolved_refs == []` assertion below.
        return _json({"workflow_runs": [], "total_count": 1, "workflows": [{"id": 1}]})

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
        if req.url.path.endswith("/actions/runs"):
            captured["params"] = dict(req.url.params)
        return _json({"workflow_runs": [], "total_count": 1, "workflows": [{"id": 1}]})

    _install_mock(monkeypatch, handler)
    GitHubProvider().list_runs_recent(_project(), token="t", status="all")
    assert "status" not in captured["params"]


def test_list_runs_recent_status_completed_sends_status_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status='completed'` must send `status=completed` in the query."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/actions/runs"):
            captured["params"] = dict(req.url.params)
        return _json({"workflow_runs": [], "total_count": 1, "workflows": [{"id": 1}]})

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


# ---------- ticket #152: structured annotations (_normalize_gh_annotations) --


def _jobs_payload_with_check_run(
    job_id: int,
    job_name: str = "test",
    failed_step_name: str = "Run pytest",
    check_run_url: str = "https://api.github.com/repos/acme/backend/check-runs/555",
) -> dict:
    """A /jobs response with one failed job carrying a check_run_url."""
    return {
        "jobs": [
            {
                "id": job_id,
                "name": job_name,
                "conclusion": "failure",
                "html_url": f"https://github.com/acme/backend/actions/runs/1/jobs/{job_id}",
                "check_run_url": check_run_url,
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


def test_get_run_failure_annotations_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test for ticket #152.

    A mocked `.../annotations` response containing a realistic check-run
    annotation (`path`, `start_line`, `annotation_level="failure"`,
    `message`, `title`) must come back on `FailingJob.annotations` as a
    typed `list[FailureAnnotation]` with correctly mapped fields — not
    the raw GitHub JSON dict. The log-excerpt annotation-anchor behaviour
    (which consumes the raw payload internally) must still work.
    """
    from lib_python_projects.providers.base import FailureAnnotation

    run_id = 33333
    job_id = 44
    head_sha = "feedface"
    job_name = "test"
    check_run_url = "https://api.github.com/repos/acme/backend/check-runs/555"

    raw_annotation = {
        "path": "src/app.py",
        "start_line": 10,
        "end_line": 12,
        "annotation_level": "failure",
        "message": "AssertionError: expected 1 got 2",
        "title": "Test failed",
    }

    # Log text with no group headers so the excerpt anchors on the
    # annotation's start_line (anchor strategy #2) — proves the raw
    # payload is still consumed by _extract_log_excerpt unaffected.
    log_lines = [f"log-line-{i}" for i in range(1, 30)]
    log_text = "\n".join(log_lines)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/repos/acme/backend/actions/runs/{run_id}":
            return _json(_failed_run_payload(run_id, head_sha))
        if path == f"/repos/acme/backend/actions/runs/{run_id}/jobs":
            return _json(_jobs_payload_with_check_run(
                job_id, job_name=job_name, check_run_url=check_run_url,
            ))
        if path == "/repos/acme/backend/check-runs/555/annotations":
            return _json([raw_annotation])
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
    assert job.annotations == [
        FailureAnnotation(
            step=job_name,
            message="AssertionError: expected 1 got 2",
            file="src/app.py",
            line=10,
            severity="failure",
            title="Test failed",
        )
    ]
    # The annotation-anchor path in _extract_log_excerpt still consumed
    # the raw payload — the excerpt is anchored around start_line=10,
    # i.e. "log-line-10" must be present.
    assert job.log_excerpt is not None
    assert "log-line-10" in job.log_excerpt


class TestNormalizeGhAnnotations:
    """Table-driven unit tests for `_normalize_gh_annotations` — a pure
    function, no HTTP involved (ticket #152)."""

    def test_empty_list_returns_empty_list(self) -> None:
        from lib_python_projects.providers.github import _normalize_gh_annotations

        assert _normalize_gh_annotations([], "build") == []

    def test_none_input_returns_empty_list(self) -> None:
        from lib_python_projects.providers.github import _normalize_gh_annotations

        assert _normalize_gh_annotations(None, "build") == []

    def test_missing_path_yields_none_file(self) -> None:
        from lib_python_projects.providers.github import _normalize_gh_annotations

        raw = [{"start_line": 5, "message": "boom", "annotation_level": "warning"}]
        out = _normalize_gh_annotations(raw, "build")
        assert len(out) == 1
        assert out[0].file is None
        assert out[0].line == 5
        assert out[0].step == "build"

    def test_missing_start_line_falls_back_to_end_line(self) -> None:
        from lib_python_projects.providers.github import _normalize_gh_annotations

        raw = [{"path": "a.py", "end_line": 7, "message": "boom"}]
        out = _normalize_gh_annotations(raw, "build")
        assert out[0].line == 7

    def test_missing_message_defaults_to_empty_string(self) -> None:
        from lib_python_projects.providers.github import _normalize_gh_annotations

        raw = [{"path": "a.py", "start_line": 3}]
        out = _normalize_gh_annotations(raw, "build")
        assert out[0].message == ""

    def test_missing_both_start_and_end_line_yields_none(self) -> None:
        from lib_python_projects.providers.github import _normalize_gh_annotations

        raw = [{"path": "a.py", "message": "boom"}]
        out = _normalize_gh_annotations(raw, "build")
        assert out[0].line is None

    def test_step_applied_to_every_annotation(self) -> None:
        from lib_python_projects.providers.github import _normalize_gh_annotations

        raw = [{"message": "one"}, {"message": "two"}]
        out = _normalize_gh_annotations(raw, "lint")
        assert [a.step for a in out] == ["lint", "lint"]


# ---------- ticket #168: get_step_log ----------------------------------------


def test_get_step_log_returns_full_unbounded_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_step_log must return the ENTIRE log body, uncapped — larger than
    the 256 KB excerpt cap that _fetch_job_log normally applies."""
    run_id = 12345
    job_id = 99

    padding_line = "x" * 200
    padding_lines = [padding_line] * 1400  # ~281 KB, over the 256 KB cap
    full_log_text = "\n".join(padding_lines + ["END-OF-LOG-SENTINEL"])
    assert len(full_log_text.encode("utf-8")) > 256 * 1024

    seen_max_bytes: list = []

    def fake_fetch(token, url, *, max_bytes=256 * 1024):
        seen_max_bytes.append(max_bytes)
        assert url == f"/repos/acme/backend/actions/jobs/{job_id}/logs"
        return full_log_text

    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log", fake_fetch,
    )

    result = GitHubProvider().get_step_log(
        _project(), token="t", run_id=str(run_id), job_id=str(job_id)
    )
    assert result == full_log_text
    assert result.splitlines()[-1] == "END-OF-LOG-SENTINEL"
    assert seen_max_bytes == [None]


def test_get_step_log_404_raises_github_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When _fetch_job_log returns None (403/404 on the redirect), get_step_log
    must raise a typed GitHubError(404, ...) rather than returning None."""
    from lib_python_projects.providers.github import GitHubError

    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log",
        lambda token, url, *, max_bytes=256 * 1024: None,
    )

    with pytest.raises(GitHubError) as exc:
        GitHubProvider().get_step_log(
            _project(), token="t", run_id="12345", job_id="99"
        )
    assert exc.value.status == 404


def test_get_step_log_empty_body_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty (but present) log body must round-trip as "" rather than
    being treated as an error."""
    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log",
        lambda token, url, *, max_bytes=256 * 1024: "",
    )

    result = GitHubProvider().get_step_log(
        _project(), token="t", run_id="12345", job_id="99"
    )
    assert result == ""


def test_get_step_log_non_numeric_run_id_raises_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric run_id must raise GitHubError(404, ...) without calling
    _fetch_job_log at all."""
    from lib_python_projects.providers.github import GitHubError

    def fail_fetch(token, url, *, max_bytes=256 * 1024):
        raise AssertionError("_fetch_job_log must not be called for non-numeric run_id")

    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log", fail_fetch,
    )

    with pytest.raises(GitHubError) as exc:
        GitHubProvider().get_step_log(
            _project(), token="t", run_id="main", job_id="99"
        )
    assert exc.value.status == 404
    assert "main" in exc.value.message


def test_get_run_to_get_step_log_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FailingJob.job_id populated by get_run(include_failure_excerpt=True)
    must be usable, unmodified, as the job_id argument to get_step_log — and
    it must hit the same log endpoint."""
    run_id = 56789
    job_id = 4242
    head_sha = "cafefeed"
    full_log_text = "full raw job log contents\nline 2\n"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/repos/acme/backend/actions/runs/{run_id}":
            return _json(_failed_run_payload(run_id, head_sha))
        if path == f"/repos/acme/backend/actions/runs/{run_id}/jobs":
            return _json(_jobs_payload(job_id))
        raise AssertionError(f"unexpected JSON request: {req.url}")

    _install_mock(monkeypatch, handler)

    requested_urls: list[str] = []

    def fake_fetch(token, url, *, max_bytes=256 * 1024):
        requested_urls.append(url)
        return full_log_text

    monkeypatch.setattr(
        "lib_python_projects.providers.github._fetch_job_log", fake_fetch,
    )

    run = GitHubProvider().get_run(_project(), token="t", run_id=str(run_id))
    assert run.failure is not None
    assert len(run.failure.failing_jobs) == 1
    resolved_job_id = run.failure.failing_jobs[0].job_id
    assert resolved_job_id == str(job_id)

    result = GitHubProvider().get_step_log(
        _project(), token="t", run_id=str(run_id), job_id=resolved_job_id
    )
    assert result == full_log_text
    assert requested_urls[-1] == f"/repos/acme/backend/actions/jobs/{job_id}/logs"


# ---------- ticket #200 -- run-listing filters (workflow/event/since) -------


def _run(run_id, name="CI", event="push", created_at="2026-08-21T10:00:00Z", head_sha="a" * 40):
    return {
        "id": run_id,
        "name": name,
        "head_sha": head_sha,
        "head_branch": "main",
        "event": event,
        "status": "completed",
        "conclusion": "success",
        "html_url": f"https://github.com/acme/backend/actions/runs/{run_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "run_attempt": 1,
        "display_title": name,
    }


def test_list_runs_recent_filters_by_workflow_client_side(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/actions/runs"
        return _json({"workflow_runs": [
            _run(1, name="release"),
            _run(2, name="CI"),
        ]})

    _install_mock(monkeypatch, handler)
    runs, _ = GitHubProvider().list_runs_recent(_project(), token="t", workflow="release")
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_pushes_event_and_since_as_query_params(monkeypatch):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["event"] = req.url.params.get("event")
        seen["created"] = req.url.params.get("created")
        return _json({"workflow_runs": [_run(1, event="workflow_dispatch")]})

    _install_mock(monkeypatch, handler)
    runs, _ = GitHubProvider().list_runs_recent(
        _project(), token="t", event="manual", since="2026-08-21T09:00:00Z",
    )
    assert seen["event"] == "workflow_dispatch"
    assert seen["created"] == ">=2026-08-21T09:00:00Z"
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_workflow_numeric_id_swaps_path(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/actions/workflows/123/runs"
        return _json({"workflow_runs": [_run(1, name="release")]})

    _install_mock(monkeypatch, handler)
    runs, _ = GitHubProvider().list_runs_recent(_project(), token="t", workflow="123")
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_workflow_filename_swaps_path(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/actions/workflows/release.yml/runs"
        return _json({"workflow_runs": [_run(1, name="release")]})

    _install_mock(monkeypatch, handler)
    runs, _ = GitHubProvider().list_runs_recent(_project(), token="t", workflow="release.yml")
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_workflow_bare_name_does_not_swap_path(monkeypatch):
    """A bare workflow name (no id, no extension) isn't accepted by the
    per-workflow endpoint, so the request stays on `/actions/runs` and
    matching happens purely client-side via `apply_run_filters`."""
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/actions/runs"
        return _json({"workflow_runs": [
            _run(1, name="release"), _run(2, name="CI"),
        ]})

    _install_mock(monkeypatch, handler)
    runs, _ = GitHubProvider().list_runs_recent(_project(), token="t", workflow="release")
    assert [r.id for r in runs] == ["1"]


def test_list_runs_for_branch_accepts_filter_kwargs(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "branchsha1"}})
        if path == "/repos/acme/backend/actions/runs":
            assert req.url.params.get("branch") == "main"
            return _json({"workflow_runs": [
                _run(1, name="release"), _run(2, name="CI"),
            ]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="main", workflow="release",
    )
    assert refs == ["branchsha1"]
    assert [r.id for r in runs] == ["1"]


def test_list_runs_recent_bare_workflow_filter_sees_full_raw_page_before_limit(monkeypatch):
    """Round-2 finding 1: the raw page fetched from the API must not be
    sized to the caller's `limit` when `workflow` can only be matched
    client-side (a bare display name, no server-side equivalent for
    `/actions/runs`) — otherwise a genuine match positioned beyond the
    first `limit` raw results is silently missed, because the server
    already truncated the page before `apply_run_filters` ever saw it.
    Unlike most mocks in this file, this one actually honors `per_page`
    (mirroring the real GitHub API) — that's what let this bug through
    the existing tests unnoticed.
    """
    all_runs = [_run(1, name="CI"), _run(2, name="release"), _run(3, name="CI")]

    def handler(req: httpx.Request) -> httpx.Response:
        per_page = int(req.url.params.get("per_page", "30"))
        return _json({"workflow_runs": all_runs[:per_page]})

    _install_mock(monkeypatch, handler)
    runs, _ = GitHubProvider().list_runs_recent(
        _project(), token="t", workflow="release", limit=1,
    )
    assert [r.id for r in runs] == ["2"]


def test_list_runs_for_commit_limit_applied_after_filtering(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/commits/deadbeef":
            return _json({"sha": "deadbeef"})
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": [
                _run(1, name="CI"), _run(2, name="release"), _run(3, name="release"),
            ]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, refs = GitHubProvider().list_runs_for_commit(
        _project(), token="t", sha="deadbeef", workflow="release", limit=1,
    )
    assert refs == ["deadbeef"]
    assert [r.id for r in runs] == ["2"]


# ---------- ticket #200 -- trigger_pipeline / wait_for_run -------------------


FIXED_NOW = "2026-08-21T10:00:00.123456Z"


def _patch_now(monkeypatch, github_provider_mod):
    monkeypatch.setattr(github_provider_mod, "now_utc", lambda: FIXED_NOW)


def _no_sleep(monkeypatch, github_provider_mod):
    monkeypatch.setattr(github_provider_mod, "_trigger_sleep", lambda seconds: None)


def test_trigger_pipeline_dispatches_then_polls_and_skips_stale_run(monkeypatch):
    """A run created before t0 (the dispatch time) must NOT be selected;
    a run created after t0 must be."""
    _patch_now(monkeypatch, github_provider)
    _no_sleep(monkeypatch, github_provider)
    dispatch_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/workflows/release.yml/dispatches":
            dispatch_calls.append(req)
            return httpx.Response(status_code=204)
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "branchsha1"}})
        if path == "/repos/acme/backend/actions/workflows/release.yml/runs":
            return _json({"workflow_runs": [
                _run(1, name="release", event="workflow_dispatch",
                     created_at="2026-08-21T09:59:00Z"),  # stale, pre-t0
                _run(2, name="release", event="workflow_dispatch",
                     created_at="2026-08-21T10:00:01Z"),  # fresh, post-t0
            ]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    run = GitHubProvider().trigger_pipeline(
        _project(), token="t", workflow="release.yml", ref="main",
    )
    assert len(dispatch_calls) == 1
    assert run is not None
    assert run.id == "2"


def test_trigger_pipeline_two_post_t0_runs_oldest_wins(monkeypatch):
    _patch_now(monkeypatch, github_provider)
    _no_sleep(monkeypatch, github_provider)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/workflows/release.yml/dispatches":
            return httpx.Response(status_code=204)
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "branchsha1"}})
        if path == "/repos/acme/backend/actions/workflows/release.yml/runs":
            return _json({"workflow_runs": [
                _run(1, name="release", event="workflow_dispatch",
                     created_at="2026-08-21T10:00:05Z"),
                _run(2, name="release", event="workflow_dispatch",
                     created_at="2026-08-21T10:00:01Z"),  # oldest post-t0
            ]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    run = GitHubProvider().trigger_pipeline(
        _project(), token="t", workflow="release.yml", ref="main",
    )
    assert run is not None
    assert run.id == "2"


def test_trigger_pipeline_wait_false_returns_none_and_does_not_poll(monkeypatch):
    dispatch_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/workflows/release.yml/dispatches":
            dispatch_calls.append(req)
            return httpx.Response(status_code=204)
        raise AssertionError(f"unexpected request (wait=False must not poll): {req.url}")

    _install_mock(monkeypatch, handler)
    run = GitHubProvider().trigger_pipeline(
        _project(), token="t", workflow="release.yml", ref="main", wait=False,
    )
    assert run is None
    assert len(dispatch_calls) == 1


def test_trigger_pipeline_non_2xx_dispatch_raises(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError):
        GitHubProvider().trigger_pipeline(
            _project(), token="t", workflow="release.yml", ref="main",
        )


def test_trigger_pipeline_empty_workflow_raises_before_http(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty workflow")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError):
        GitHubProvider().trigger_pipeline(_project(), token="t", workflow="")


def test_trigger_pipeline_empty_ref_raises_before_http(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty ref")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError):
        GitHubProvider().trigger_pipeline(_project(), token="t", workflow="release.yml", ref="")


def test_trigger_pipeline_with_tag_ref_resolves_run(monkeypatch):
    """Reviewer fix pass (ticket #200): `workflow_dispatch` accepts a TAG
    as `ref`, not just a branch — `wait_for_run` must resolve the
    resulting run without assuming `ref` is a branch (it must not call
    the branches endpoint at all, since a tag would 404 there and the
    old code polled `list_runs_for_branch`, which returns `([], [])`
    for a non-branch ref and spuriously times out)."""
    _patch_now(monkeypatch, github_provider)
    _no_sleep(monkeypatch, github_provider)
    dispatch_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/workflows/release.yml/dispatches":
            dispatch_calls.append(req)
            return httpx.Response(status_code=204)
        if path == "/repos/acme/backend/branches/v1.2.3":
            raise AssertionError(
                "wait_for_run must not resolve ref as a branch — v1.2.3 is a tag"
            )
        if path == "/repos/acme/backend/actions/workflows/release.yml/runs":
            return _json({"workflow_runs": [
                {
                    "id": 7,
                    "name": "release",
                    "head_sha": "b" * 40,
                    "head_branch": "v1.2.3",
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/acme/backend/actions/runs/7",
                    "created_at": "2026-08-21T10:00:01Z",
                    "updated_at": "2026-08-21T10:00:01Z",
                    "run_attempt": 1,
                    "display_title": "release",
                },
            ]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    run = GitHubProvider().trigger_pipeline(
        _project(), token="t", workflow="release.yml", ref="v1.2.3",
    )
    assert len(dispatch_calls) == 1
    assert run is not None
    assert run.id == "7"
    assert run.branch == "v1.2.3"


def test_trigger_pipeline_bare_workflow_name_raises_before_http(monkeypatch):
    """`trigger_pipeline` requires a filename (`release.yml`) or numeric
    workflow id — GitHub's dispatch endpoint 404s on a bare display name
    like `"Release"`. Reviewer fix pass (ticket #200): this must raise a
    clear `ValueError` up front instead of silently forwarding the bare
    name to the dispatch URL and surfacing an opaque `GitHubError` from
    the resulting 404."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for a bare workflow display name")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError):
        GitHubProvider().trigger_pipeline(
            _project(), token="t", workflow="Release", ref="main",
        )


def test_wait_for_run_standalone_call(monkeypatch):
    _no_sleep(monkeypatch, github_provider)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "branchsha1"}})
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": [_run(1, created_at="2026-08-21T10:05:00Z")]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    run = GitHubProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", ref="main",
    )
    assert run is not None
    assert run.id == "1"


def test_wait_for_run_timeout_returns_none(monkeypatch):
    _no_sleep(monkeypatch, github_provider)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "branchsha1"}})
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        if path == "/repos/acme/backend/actions/workflows":
            return _json({"total_count": 1})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    run = GitHubProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", ref="main", timeout=0.05,
    )
    assert run is None


def test_wait_for_run_without_since_raises_type_error():
    with pytest.raises(TypeError):
        GitHubProvider().wait_for_run(_project(), token="t", ref="main")


# ---------- ticket #209 -- CI workflow discovery -----------------------------


def test_list_workflows_maps_payload(monkeypatch):
    """`list_workflows` maps the Actions `/actions/workflows` payload into
    `Workflow` objects; `[]` on an empty listing and on 404."""
    from lib_python_projects.providers.base import Workflow

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/actions/workflows"
        return _json({
            "total_count": 1,
            "workflows": [{
                "id": 161335,
                "name": "CI",
                "path": ".github/workflows/ci.yml",
                "state": "active",
                "html_url": "https://github.com/acme/backend/blob/main/.github/workflows/ci.yml",
            }],
        })

    _install_mock(monkeypatch, handler)
    workflows = GitHubProvider().list_workflows(_project(), token="t")
    assert workflows == [Workflow(
        id="161335", name="CI", path=".github/workflows/ci.yml",
        state="active",
        url="https://github.com/acme/backend/blob/main/.github/workflows/ci.yml",
        dispatch_target="ci.yml",
    )]


def test_list_workflows_missing_path_falls_back_to_id(monkeypatch):
    """A workflow entry with no `path` still yields a usable
    `dispatch_target` — falls back to the numeric id as a string."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({
            "total_count": 1,
            "workflows": [{"id": 42, "name": "Legacy"}],
        })

    _install_mock(monkeypatch, handler)
    workflows = GitHubProvider().list_workflows(_project(), token="t")
    assert len(workflows) == 1
    assert workflows[0].dispatch_target == "42"
    assert workflows[0].url is None


def test_list_workflows_empty_on_no_workflows(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"total_count": 0, "workflows": []})

    _install_mock(monkeypatch, handler)
    assert GitHubProvider().list_workflows(_project(), token="t") == []


def test_list_workflows_empty_on_404(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    assert GitHubProvider().list_workflows(_project(), token="t") == []


def test_is_ci_configured_true_when_workflows_exist(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"total_count": 1, "workflows": [{"id": 1}]})

    _install_mock(monkeypatch, handler)
    assert GitHubProvider().is_ci_configured(_project(), token="t") is True


def test_is_ci_configured_false_when_no_workflows(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    assert GitHubProvider().is_ci_configured(_project(), token="t") is False


def test_list_runs_for_branch_appends_no_ci_sentinel_when_not_configured(
    monkeypatch,
):
    """Driving test (ticket #209): branch resolves, no runs, and the
    repository has no Actions workflows at all → the uniform
    `NO_CI_SENTINEL` is appended as the last element of resolved_refs."""
    from lib_python_projects.providers.base import NO_CI_SENTINEL

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "sha1"}})
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        if path == "/repos/acme/backend/actions/workflows":
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="main",
    )
    assert runs == []
    assert resolved_refs == ["sha1", NO_CI_SENTINEL]


def test_list_runs_for_branch_no_sentinel_when_ci_configured(monkeypatch):
    """Counterpart: branch resolves, no runs, but the repository DOES
    have Actions workflows → no sentinel, matching the pre-ticket-#209
    branch-mode shape exactly."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "sha1"}})
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        if path == "/repos/acme/backend/actions/workflows":
            return _json({"total_count": 1, "workflows": [{"id": 1}]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_for_branch(
        _project(), token="t", branch="main",
    )
    assert runs == []
    assert resolved_refs == ["sha1"]


def test_list_runs_recent_appends_no_ci_sentinel_when_not_configured(
    monkeypatch,
):
    """Driving test (ticket #209): no runs at all, and no workflows
    configured → `([], [NO_CI_SENTINEL])`."""
    from lib_python_projects.providers.base import NO_CI_SENTINEL

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        if path == "/repos/acme/backend/actions/workflows":
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitHubProvider().list_runs_recent(_project(), token="t")
    assert runs == []
    assert resolved_refs == [NO_CI_SENTINEL]


def test_wait_for_run_never_probes_ci_configuration(monkeypatch):
    """Regression guard (ticket #209): `wait_for_run` must poll through
    the unprobed helper — every empty poll iteration must NOT trigger
    the `/actions/workflows` probe request. A strict handler that raises
    on that path, combined with a timeout that forces several empty
    polls, proves the probe is never hit."""
    _no_sleep(monkeypatch, github_provider)
    poll_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/runs":
            poll_count["n"] += 1
            return _json({"workflow_runs": []})
        raise AssertionError(f"unexpected request (probe?): {req.url}")

    _install_mock(monkeypatch, handler)
    run = GitHubProvider().wait_for_run(
        _project(), token="t", since="2026-08-21T10:00:00Z", timeout=0.05,
    )
    assert run is None
    assert poll_count["n"] >= 1


def test_dispatch_target_round_trips_into_trigger_pipeline(monkeypatch):
    """The `dispatch_target` from `list_workflows` works verbatim as the
    `workflow` argument to `trigger_pipeline` — hits the filename-scoped
    dispatch endpoint, never the full `.github/workflows/...` path."""
    _patch_now(monkeypatch, github_provider)
    _no_sleep(monkeypatch, github_provider)
    dispatch_paths = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/workflows":
            return _json({
                "total_count": 1,
                "workflows": [{
                    "id": 9, "name": "CI", "path": ".github/workflows/ci.yml",
                }],
            })
        if path.startswith("/repos/acme/backend/actions/workflows/") and path.endswith("/dispatches"):
            dispatch_paths.append(path)
            return httpx.Response(status_code=204)
        if path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "branchsha1"}})
        if path == "/repos/acme/backend/actions/workflows/ci.yml/runs":
            return _json({"workflow_runs": [
                _run(5, name="CI", event="workflow_dispatch",
                     created_at="2026-08-21T10:00:01Z"),
            ]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    workflows = GitHubProvider().list_workflows(_project(), token="t")
    assert workflows[0].dispatch_target == "ci.yml"

    run = GitHubProvider().trigger_pipeline(
        _project(), token="t", workflow=workflows[0].dispatch_target, ref="main",
    )
    assert dispatch_paths == ["/repos/acme/backend/actions/workflows/ci.yml/dispatches"]
    assert all(".github/workflows/" not in p for p in dispatch_paths)
    assert run is not None
    assert run.id == "5"


# ---------- ticket #200 -- get_ref -------------------------------------------


def test_get_ref_branch(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "branchsha1"}})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    ref = GitHubProvider().get_ref(_project(), token="t", ref="main")
    assert ref is not None
    assert ref.kind == "branch"
    assert ref.sha == "branchsha1"
    assert ref.name == "main"


def test_get_ref_lightweight_tag_single_hop(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/v1.0.0":
            return _json({"message": "Branch not found"}, status_code=404)
        if path == "/repos/acme/backend/git/refs/tags/v1.0.0":
            return _json({"object": {"sha": "commitsha1", "type": "commit"}})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    ref = GitHubProvider().get_ref(_project(), token="t", ref="v1.0.0")
    assert ref is not None
    assert ref.kind == "tag"
    assert ref.sha == "commitsha1"


def test_get_ref_annotated_tag_double_hop(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/v2.0.0":
            return _json({"message": "Branch not found"}, status_code=404)
        if path == "/repos/acme/backend/git/refs/tags/v2.0.0":
            return _json({"object": {"sha": "tagobjsha1", "type": "tag"}})
        if path == "/repos/acme/backend/git/tags/tagobjsha1":
            return _json({"object": {"sha": "commitsha2", "type": "commit"}})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    ref = GitHubProvider().get_ref(_project(), token="t", ref="v2.0.0")
    assert ref is not None
    assert ref.kind == "tag"
    # sha must be the *peeled commit* sha, not the tag object's own sha.
    assert ref.sha == "commitsha2"


def test_get_ref_commit_sha(monkeypatch):
    sha = "c" * 40

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/repos/acme/backend/branches/{sha}":
            return _json({"message": "Branch not found"}, status_code=404)
        if path == f"/repos/acme/backend/git/refs/tags/{sha}":
            return _json({"message": "Not Found"}, status_code=404)
        if path == f"/repos/acme/backend/commits/{sha}":
            return _json({"sha": sha})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    ref = GitHubProvider().get_ref(_project(), token="t", ref=sha)
    assert ref is not None
    assert ref.kind == "commit"
    assert ref.sha == sha


def test_get_ref_unknown_returns_none(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    ref = GitHubProvider().get_ref(_project(), token="t", ref="does-not-exist")
    assert ref is None


def test_get_ref_branch_shadows_same_named_tag(monkeypatch):
    """When a branch and a tag share a name, the branch wins — the tag
    lookup must never even fire."""
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/branches/shared":
            return _json({"commit": {"sha": "branchsha-shared"}})
        raise AssertionError(f"tag lookup must not fire: {req.url}")

    _install_mock(monkeypatch, handler)
    ref = GitHubProvider().get_ref(_project(), token="t", ref="shared")
    assert ref is not None
    assert ref.kind == "branch"
    assert ref.sha == "branchsha-shared"


# ---------- ticket #200 -- list_releases -------------------------------------


def test_list_releases_empty(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/releases"
        return _json([])

    _install_mock(monkeypatch, handler)
    releases = GitHubProvider().list_releases(_project(), token="t")
    assert releases == []


def test_list_releases_maps_fields_and_resolves_peeled_sha(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/releases":
            return _json([{
                "tag_name": "v1.0.0",
                "name": "Version 1.0.0",
                "html_url": "https://github.com/acme/backend/releases/tag/v1.0.0",
                "draft": False,
                "prerelease": True,
                "created_at": "2026-01-01T00:00:00Z",
                "published_at": "2026-01-02T00:00:00Z",
                "body": "Release notes",
            }])
        if path == "/repos/acme/backend/git/refs/tags/v1.0.0":
            return _json({"object": {"sha": "commitsha3", "type": "commit"}})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    releases = GitHubProvider().list_releases(_project(), token="t")
    assert len(releases) == 1
    rel = releases[0]
    assert rel.tag == "v1.0.0"
    assert rel.name == "Version 1.0.0"
    assert rel.sha == "commitsha3"
    assert rel.draft is False
    assert rel.prerelease is True
    assert rel.body == "Release notes"
    assert rel.created_at == "2026-01-01T00:00:00Z"
    assert rel.published_at == "2026-01-02T00:00:00Z"
