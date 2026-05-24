"""Tests for the GitLab provider's pipeline (CI run) surface."""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.gitlab import GitLabProvider


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="gitlab", path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "test"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(
            base_url=gitlab_mod._base_url(project),
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _pipeline(pid: int, **overrides) -> dict:
    base = {
        "id": pid,
        "ref": "main",
        "sha": "abc",
        "source": "push",
        "status": "success",
        "web_url": f"https://gitlab.com/acme/backend/-/pipelines/{pid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:05:00Z",
    }
    base.update(overrides)
    return base


# ---------- list_runs_for_branch / tag / commit ------------------------------


def test_list_runs_for_branch_sends_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/repository/branches/" in str(req.url):
            return _json({"commit": {"id": "sha-main"}})
        assert "/pipelines" in str(req.url)
        assert req.url.params.get("ref") == "main"
        return _json([_pipeline(1), _pipeline(2)])

    _install_mock(monkeypatch, handler)
    runs, _ = GitLabProvider().list_runs_for_branch(_project(), "t", "main")
    assert [r.id for r in runs] == ["1", "2"]


def test_list_runs_for_tag_uses_ref_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab doesn't distinguish branch vs tag — both use `ref`."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("ref") == "v1.0.0"
        return _json([])

    _install_mock(monkeypatch, handler)
    runs, refs = GitLabProvider().list_runs_for_tag(_project(), "t", "v1.0.0")
    assert refs == ["v1.0.0"]
    assert runs == []


def test_list_runs_for_commit_sends_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/repository/commits/" in str(req.url):
            return _json({"id": "deadbeef"})
        assert req.url.params.get("sha") == "deadbeef"
        return _json([])

    _install_mock(monkeypatch, handler)
    runs, _ = GitLabProvider().list_runs_for_commit(_project(), "t", "deadbeef")


def test_list_runs_limit_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/repository/branches/" in str(req.url):
            return _json({"commit": {"id": "sha-main"}})
        assert req.url.params.get("per_page") == "100"
        return _json([])

    _install_mock(monkeypatch, handler)
    runs, _ = GitLabProvider().list_runs_for_branch(_project(), "t", "main", limit=500)


# ---------- list_runs_for_ticket ---------------------------------------------


def test_list_runs_for_ticket_walks_related_mrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Walk related_merge_requests → per-MR pipelines → flatten."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/issues/5/related_merge_requests"):
            return _json([{"iid": 10}, {"iid": 11}])
        if "/merge_requests/10/pipelines" in url:
            return _json([_pipeline(100, created_at="2024-01-01T00:00:00Z")])
        if "/merge_requests/11/pipelines" in url:
            return _json([
                _pipeline(101, created_at="2024-01-02T00:00:00Z"),
                _pipeline(102, created_at="2024-01-03T00:00:00Z"),
            ])
        return _json([], status_code=404)

    _install_mock(monkeypatch, handler)
    runs, refs = GitLabProvider().list_runs_for_ticket(_project(), "t", "5")
    # All 3 pipelines, sorted by created_at desc.
    assert [r.id for r in runs] == ["102", "101", "100"]
    # MR iids prefixed with `!` mirror GitHub's resolved_refs surface.
    assert refs == ["!10", "!11"]


def test_list_runs_for_ticket_no_related_mrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/related_merge_requests" in str(req.url):
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    runs, refs = GitLabProvider().list_runs_for_ticket(_project(), "t", "5")
    assert runs == []
    assert refs == []


# ---------- get_run ----------------------------------------------------------


def test_get_run_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_pipeline(100))

    _install_mock(monkeypatch, handler)
    run = GitLabProvider().get_run(_project(), "t", "100")
    assert run.id == "100"
    assert run.failure is None  # not requested


def test_get_run_succeeds_skips_failure_context_even_if_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_failure_excerpt=True on a successful run still returns
    `failure=None` — only failed runs trigger the jobs/traces walk."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_pipeline(100, status="success"))

    _install_mock(monkeypatch, handler)
    run = GitLabProvider().get_run(
        _project(), "t", "100", include_failure_excerpt=True,
    )
    assert run.conclusion == "success"
    assert run.failure is None


def test_get_run_failed_with_failure_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed run + flag set → walks jobs, fetches trace for failing
    ones, builds PipelineFailure."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/pipelines/100/jobs" in url:
            return _json([
                {
                    "id": 1, "name": "build", "status": "success",
                    "stage": "build", "web_url": "u1",
                },
                {
                    "id": 2, "name": "test", "status": "failed",
                    "stage": "test", "web_url": "u2",
                },
                {
                    "id": 3, "name": "lint", "status": "failed",
                    "stage": "test", "web_url": "u3",
                },
            ])
        if "/pipelines/100" in url and "/jobs" not in url:
            return _json(_pipeline(100, status="failed"))
        if "/jobs/2/trace" in url:
            return httpx.Response(
                200, content=b"build log...\nFAIL: assertion error\n",
                headers={"Content-Type": "text/plain"},
            )
        if "/jobs/3/trace" in url:
            # Make this trace huge → ensure tail-truncation logic works.
            return httpx.Response(
                200, content=b"x" * 10000 + b"\nLAST LINE\n",
                headers={"Content-Type": "text/plain"},
            )
        return _json({}, 404)

    _install_mock(monkeypatch, handler)
    run = GitLabProvider().get_run(
        _project(), "t", "100", include_failure_excerpt=True,
    )
    assert run.conclusion == "failed"
    assert run.failure is not None
    failing = run.failure.failing_jobs
    assert len(failing) == 2
    names = sorted(j.name for j in failing)
    assert names == ["lint", "test"]
    # Annotations are always [] for GitLab (no structured surface).
    for j in failing:
        assert j.annotations == []
    # Trace excerpt is truncated to a reasonable tail for the big one.
    big_job = [j for j in failing if j.name == "lint"][0]
    assert big_job.log_excerpt is not None
    assert len(big_job.log_excerpt) <= 4096
    assert "LAST LINE" in big_job.log_excerpt  # tail must include actual failure


def test_get_run_failed_handles_jobs_endpoint_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the jobs endpoint 404s/403s, `failure` carries a note rather
    than blowing up the whole get_run call."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/pipelines/100/jobs" in url:
            return _json({"message": "forbidden"}, 403)
        if "/pipelines/100" in url:
            return _json(_pipeline(100, status="failed"))
        return _json({}, 404)

    _install_mock(monkeypatch, handler)
    run = GitLabProvider().get_run(
        _project(), "t", "100", include_failure_excerpt=True,
    )
    assert run.failure is not None
    assert run.failure.failing_jobs == []
    assert run.failure.note is not None


# ---------- Issue #17: get_run non-numeric run_id ----------------------------


def test_get_run_non_numeric_run_id_raises_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_run with a non-numeric run_id must raise GitLabError(404, ...)
    naming the run_id — GitLab would otherwise return 400."""
    from lib_python_projects.providers.gitlab import GitLabError

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for non-numeric id")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().get_run(_project(), "t", "not-a-number")
    assert exc.value.status == 404
    assert "not-a-number" in exc.value.message


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
        GitLabProvider().list_runs_for_branch(
            _project(), "t", "main", limit=bad_limit,
        )


# ---------- list_runs_for_branch / list_runs_for_commit tuple shape ----------


def test_list_runs_for_branch_branch_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch probe 404 → ([], [])."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "/repository/branches/missing" in str(req.url):
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitLabProvider().list_runs_for_branch(
        _project(), "t", "missing",
    )
    assert runs == []
    assert resolved_refs == []


def test_list_runs_for_branch_found_no_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch resolves to sha, pipelines empty → ([], [sha])."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "/repository/branches/main" in str(req.url):
            return _json({"commit": {"id": "abc123"}})
        if "/pipelines" in str(req.url):
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitLabProvider().list_runs_for_branch(
        _project(), "t", "main",
    )
    assert runs == []
    assert resolved_refs == ["abc123"]


def test_list_runs_for_branch_found_with_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch resolves and pipelines returned → ([run], [sha])."""
    sha = "def456"

    def handler(req: httpx.Request) -> httpx.Response:
        if "/repository/branches/feat" in str(req.url):
            return _json({"commit": {"id": sha}})
        if "/pipelines" in str(req.url):
            return _json([_pipeline(42)])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitLabProvider().list_runs_for_branch(
        _project(), "t", "feat",
    )
    assert len(runs) == 1
    assert runs[0].id == "42"
    assert resolved_refs == [sha]


def test_list_runs_for_commit_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit probe 404 → ([], [])."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "/repository/commits/deadbeef" in str(req.url):
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitLabProvider().list_runs_for_commit(
        _project(), "t", "deadbeef",
    )
    assert runs == []
    assert resolved_refs == []


def test_list_runs_for_commit_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit found, one pipeline → ([run], [sha])."""
    sha = "deadbeef"

    def handler(req: httpx.Request) -> httpx.Response:
        if f"/repository/commits/{sha}" in str(req.url):
            return _json({"id": sha})
        if "/pipelines" in str(req.url):
            return _json([_pipeline(99)])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    runs, resolved_refs = GitLabProvider().list_runs_for_commit(
        _project(), "t", sha,
    )
    assert len(runs) == 1
    assert runs[0].id == "99"
    assert resolved_refs == [sha]
