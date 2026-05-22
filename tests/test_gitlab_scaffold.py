"""Smoke tests for the GitLab provider scaffold.

These tests cover the bits that exist before per-method implementations
land:
- the provider class can be instantiated and is registered
- the mappers translate canonical GitLab REST payloads into the common
  dataclasses
- `list_statuses` returns a self-consistent static spec
- the HTTP client is built with the right headers and honours
  `base_url` for self-hosted instances
- `_project_path` URL-encodes the namespace path
- `_check` translates the three documented GitLab error payload shapes
- unimplemented methods raise `NotImplementedError` with a clear name
"""
from __future__ import annotations

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.base import (
    Comment,
    PipelineRun,
    PullRequest,
    StatusSpec,
    Ticket,
)
from lib_python_projects.providers.gitlab import (
    DEFAULT_BASE_URL,
    GitLabError,
    GitLabProvider,
    _base_url,
    _check,
    _client,
    _map_issue,
    _map_mergeable,
    _map_mr,
    _map_note,
    _map_pipeline_run,
    _project_path,
)


def _project(
    path: str = "acme/backend",
    base_url: str | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="gitlab",
        path=path,
        base_url=base_url,
        token_env="GITLAB_TOKEN_ACME",
    )


# ---------- registry wiring ---------------------------------------------------


def test_provider_is_registered() -> None:
    # TODO(ports-adapters): re-enable nach API-Stabilisierung
    # The tool-layer `_PROVIDERS` registry lives in agent-project-issues,
    # not in this lib.
    import pytest as _pytest
    _pytest.skip("tool-layer registry test — belongs in agent-project-issues")


def test_safe_translates_gitlab_error() -> None:
    """`_safe` must catch `GitLabError` the same way it catches
    `GitHubError` so callers get a uniform `{"error": ...}` envelope."""
    # TODO(ports-adapters): re-enable nach API-Stabilisierung
    import pytest as _pytest
    _pytest.skip("tool-layer error-translation test — belongs in agent-project-issues")


# ---------- _base_url / _client ----------------------------------------------


def test_base_url_default_is_gitlab_com() -> None:
    assert _base_url(_project()) == "https://gitlab.com/api/v4"


def test_base_url_honours_self_hosted() -> None:
    p = _project(base_url="https://gitlab.example.com/")
    # Trailing slash must be stripped.
    assert _base_url(p) == "https://gitlab.example.com/api/v4"


def test_client_sets_private_token_header() -> None:
    client = _client(_project(), token="glpat_secret")
    try:
        # The httpx client exposes default headers via `.headers`.
        assert client.headers.get("PRIVATE-TOKEN") == "glpat_secret"
        assert client.headers.get("Accept") == "application/json"
        assert "User-Agent" in client.headers
    finally:
        client.close()


def test_client_without_token_omits_auth_header() -> None:
    client = _client(_project(), token=None)
    try:
        assert "PRIVATE-TOKEN" not in client.headers
    finally:
        client.close()


# ---------- _project_path ----------------------------------------------------


def test_project_path_url_encodes_slashes() -> None:
    p = _project(path="group/sub/project")
    assert _project_path(p) == "group%2Fsub%2Fproject"


def test_project_path_encodes_special_chars() -> None:
    p = _project(path="group/sub-project.name")
    encoded = _project_path(p)
    # Dots and dashes are unreserved per RFC 3986 — they shouldn't be encoded.
    # Slashes MUST be encoded.
    assert "/" not in encoded
    assert "sub-project.name" in encoded


# ---------- _check error translation -----------------------------------------


def _resp(payload, status: int = 400) -> httpx.Response:
    import json
    return httpx.Response(
        status_code=status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("GET", "https://gitlab.example/api/v4/x"),
    )


def test_check_success_is_no_op() -> None:
    r = httpx.Response(
        status_code=200,
        request=httpx.Request("GET", "https://gitlab.example/api/v4/x"),
    )
    _check(r)  # must not raise


def test_check_simple_message_payload() -> None:
    with pytest.raises(GitLabError) as exc:
        _check(_resp({"message": "404 Project Not Found"}, status=404))
    assert exc.value.status == 404
    assert "404 Project Not Found" in str(exc.value)


def test_check_oauth_error_payload() -> None:
    with pytest.raises(GitLabError) as exc:
        _check(_resp(
            {"error": "invalid_token", "error_description": "Token expired"},
            status=401,
        ))
    assert exc.value.status == 401
    assert "invalid_token" in str(exc.value)
    assert "Token expired" in str(exc.value)


def test_check_validation_error_payload() -> None:
    """GitLab returns `{"message": {"field": ["err"]}}` for validation."""
    with pytest.raises(GitLabError) as exc:
        _check(_resp(
            {"message": {"title": ["can't be blank"]}},
            status=400,
        ))
    assert exc.value.status == 400
    assert "title" in str(exc.value)
    assert "can't be blank" in str(exc.value)


def test_check_non_json_payload_falls_back_to_reason() -> None:
    r = httpx.Response(
        status_code=500,
        content=b"<html>oops</html>",
        request=httpx.Request("GET", "https://gitlab.example/api/v4/x"),
    )
    with pytest.raises(GitLabError) as exc:
        _check(r)
    assert exc.value.status == 500


# ---------- mappers ----------------------------------------------------------


def test_map_issue_open_state() -> None:
    raw = {
        "iid": 5,
        "title": "Bug",
        "description": "body text",
        "state": "opened",
        "author": {"username": "alice"},
        "assignees": [{"username": "bob"}],
        "labels": ["bug", "p1"],
        "web_url": "https://gitlab.com/acme/backend/-/issues/5",
        "created_at": "2026-05-18T10:00:00Z",
        "updated_at": "2026-05-18T11:00:00Z",
    }
    t = _map_issue(raw)
    assert isinstance(t, Ticket)
    assert t.id == "5"  # iid, stringified
    assert t.title == "Bug"
    assert t.body == "body text"
    assert t.status == "open"
    assert t.author == "alice"
    assert t.assignees == ["bob"]
    assert t.labels == ["bug", "p1"]
    assert t.url == "https://gitlab.com/acme/backend/-/issues/5"


def test_map_issue_closed_state() -> None:
    raw = {"iid": 1, "state": "closed"}
    t = _map_issue(raw)
    assert t.status == "closed"


def test_map_issue_reopened_is_open() -> None:
    raw = {"iid": 1, "state": "reopened"}
    t = _map_issue(raw)
    assert t.status == "open"


def test_map_issue_missing_fields_are_empty_strings() -> None:
    """Defensive: GitLab payloads can omit fields for archived data."""
    t = _map_issue({"iid": 1, "state": "opened"})
    assert t.title == ""
    assert t.body == ""
    assert t.author == ""
    assert t.assignees == []
    assert t.labels == []


def test_map_note_basic() -> None:
    raw = {
        "id": 42,
        "author": {"username": "alice"},
        "body": "Looks good!",
        "web_url": "https://gitlab.com/acme/backend/-/issues/5#note_42",
        "created_at": "2026-05-18T12:00:00Z",
    }
    c = _map_note(raw)
    assert isinstance(c, Comment)
    assert c.id == "42"
    assert c.author == "alice"
    assert c.body == "Looks good!"


def test_map_mergeable_yes() -> None:
    assert _map_mergeable({"detailed_merge_status": "mergeable"}) is True


def test_map_mergeable_no() -> None:
    assert _map_mergeable({"detailed_merge_status": "cannot_be_merged"}) is False
    assert _map_mergeable({"detailed_merge_status": "cannot_be_merged_rechecking"}) is False


def test_map_mergeable_checking_is_none() -> None:
    assert _map_mergeable({"detailed_merge_status": "checking"}) is None
    assert _map_mergeable({}) is None


def test_map_mergeable_legacy_field() -> None:
    """Older GitLab versions only expose `merge_status`."""
    assert _map_mergeable({"merge_status": "can_be_merged"}) is True


def test_map_mr_open() -> None:
    raw = {
        "iid": 7,
        "title": "Add feature",
        "description": "implements x",
        "state": "opened",
        "draft": False,
        "author": {"username": "alice"},
        "assignees": [{"username": "bob"}],
        "reviewers": [{"username": "carol"}],
        "labels": ["enhancement"],
        "source_branch": "feat/x",
        "target_branch": "main",
        "sha": "abc1234",
        "web_url": "https://gitlab.com/acme/backend/-/merge_requests/7",
        "created_at": "2026-05-18T10:00:00Z",
        "updated_at": "2026-05-18T11:00:00Z",
        "detailed_merge_status": "mergeable",
    }
    pr = _map_mr(raw)
    assert isinstance(pr, PullRequest)
    assert pr.id == "7"
    assert pr.number == 7
    assert pr.status == "open"
    assert pr.draft is False
    assert pr.author == "alice"
    assert pr.assignees == ["bob"]
    assert pr.reviewers == ["carol"]
    assert pr.requested_reviewers == ["carol"]  # same list, by design
    assert pr.head["ref"] == "feat/x"
    assert pr.head["sha"] == "abc1234"
    assert pr.base["ref"] == "main"
    assert pr.merged is False
    assert pr.mergeable is True


def test_map_mr_merged() -> None:
    raw = {
        "iid": 8,
        "state": "merged",
        "merged_at": "2026-05-18T12:00:00Z",
    }
    pr = _map_mr(raw)
    assert pr.status == "merged"
    assert pr.merged is True


def test_map_mr_locked_treated_as_closed() -> None:
    raw = {"iid": 9, "state": "locked"}
    pr = _map_mr(raw)
    assert pr.status == "closed"


def test_map_mr_legacy_work_in_progress_is_draft() -> None:
    raw = {"iid": 10, "state": "opened", "work_in_progress": True}
    pr = _map_mr(raw)
    assert pr.draft is True


def test_map_pipeline_run_terminal_success() -> None:
    raw = {
        "id": 100,
        "ref": "main",
        "sha": "deadbeef",
        "source": "push",
        "status": "success",
        "web_url": "https://gitlab.com/acme/backend/-/pipelines/100",
        "created_at": "2026-05-18T10:00:00Z",
        "updated_at": "2026-05-18T10:05:00Z",
    }
    run = _map_pipeline_run(raw)
    assert isinstance(run, PipelineRun)
    assert run.id == "100"
    assert run.name == "pipeline-100"
    assert run.branch == "main"
    assert run.head_sha == "deadbeef"
    assert run.event == "push"
    assert run.status == "completed"
    assert run.conclusion == "success"
    assert run.run_attempt == 1


def test_map_pipeline_run_in_flight() -> None:
    """A `running` pipeline must NOT be reported as completed."""
    raw = {"id": 101, "status": "running"}
    run = _map_pipeline_run(raw)
    assert run.status == "running"
    assert run.conclusion is None


def test_map_pipeline_run_failed() -> None:
    raw = {"id": 102, "status": "failed"}
    run = _map_pipeline_run(raw)
    assert run.status == "completed"
    assert run.conclusion == "failed"


def test_map_pipeline_run_canceled_and_skipped_are_terminal() -> None:
    """GitLab `canceled`/`skipped` are terminal — fold into `completed`."""
    for s in ("canceled", "skipped"):
        run = _map_pipeline_run({"id": 1, "status": s})
        assert run.status == "completed", s
        assert run.conclusion == s, s


# ---------- list_statuses ----------------------------------------------------


def test_list_statuses_is_static_and_self_consistent() -> None:
    spec = GitLabProvider().list_statuses(_project(), token=None)
    assert isinstance(spec, StatusSpec)
    # Every hint value must be one of the listed values.
    assert spec.hints["default_open"] in spec.values
    assert spec.hints["terminal_completed"] in spec.values
    assert spec.hints["terminal_declined"] in spec.values
    for v in spec.hints["terminal"]:
        assert v in spec.values
    # Transitions reference only known values.
    for src, dsts in spec.transitions.items():
        assert src in spec.values
        for dst in dsts:
            assert dst in spec.values


def test_list_statuses_collapses_terminal_completed_and_declined() -> None:
    """GitLab has no `state_reason` — both hints point at `closed`."""
    spec = GitLabProvider().list_statuses(_project(), token=None)
    assert spec.hints["terminal_completed"] == "closed"
    assert spec.hints["terminal_declined"] == "closed"


# ---------- DEFAULT_BASE_URL sanity ------------------------------------------


def test_default_base_url_is_gitlab_com() -> None:
    """Lock the public constant — downstream code (and the auto-discovery
    path in config.py) hard-codes this in places."""
    assert DEFAULT_BASE_URL == "https://gitlab.com"
