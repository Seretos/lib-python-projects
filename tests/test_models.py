"""Unit tests for `lib_python_projects.models`.

The bulk of the schema validation is already covered by the migrated
`test_config.py` (formerly `agent-project-issues/tests/test_config.py`).
This file focuses on the new-in-v0.1.0 surface: `ProjectConfig.local_path`.
"""
from __future__ import annotations

import json

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult


def _make_project(**kwargs) -> ProjectConfig:
    return ProjectConfig(id="x", provider="github", path="acme/backend", **kwargs)


class TestLocalPathField:
    """`local_path` is new in v0.1.0 — optional, defaults to None,
    accepted on both config-sourced and git-remote-sourced entries.
    """

    def test_local_path_defaults_to_none(self) -> None:
        p = ProjectConfig(id="x", provider="github", path="acme/backend")
        assert p.local_path is None

    def test_local_path_round_trips_when_provided(self) -> None:
        p = ProjectConfig(
            id="x",
            provider="github",
            path="acme/backend",
            local_path="/home/user/code/backend",
        )
        assert p.local_path == "/home/user/code/backend"

    def test_local_path_accepted_on_git_remote_source(self) -> None:
        p = ProjectConfig(
            id="_auto",
            provider="github",
            path="acme/backend",
            source="git-remote",
            local_path="/repos/backend",
        )
        assert p.source == "git-remote"
        assert p.local_path == "/repos/backend"

    def test_unknown_fields_still_forbidden(self) -> None:
        """`local_path` is whitelisted — other unknown keys must still
        be rejected (extra="forbid" on the model)."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ProjectConfig(
                id="x",
                provider="github",
                path="acme/backend",
                bogus_field="nope",  # type: ignore[call-arg]
            )

    def test_local_path_appears_in_model_dump(self) -> None:
        p = ProjectConfig(
            id="x",
            provider="github",
            path="acme/backend",
            local_path="/tmp/x",
        )
        d = p.model_dump()
        assert d["local_path"] == "/tmp/x"


class TestDefaultBranchField:
    """`default_branch` is new in v0.2.0 — optional str, defaults to "main",
    accepted for all providers so downstream consumers stop hard-coding the
    base branch.
    """

    def test_default_branch_defaults_to_main(self) -> None:
        p = _make_project()
        assert p.default_branch == "main"

    def test_default_branch_round_trips_when_provided(self) -> None:
        p = _make_project(default_branch="master")
        assert p.default_branch == "master"

    def test_default_branch_accepts_custom_value(self) -> None:
        p = _make_project(default_branch="develop")
        assert p.default_branch == "develop"

    def test_default_branch_appears_in_model_dump(self) -> None:
        p = _make_project(default_branch="release")
        d = p.model_dump()
        assert d["default_branch"] == "release"

    def test_default_branch_default_appears_in_model_dump(self) -> None:
        p = _make_project()
        d = p.model_dump()
        assert d["default_branch"] == "main"


class TestProjectsLoadResult:
    """Subclass of `lib_python_config.LoadResult` plus a `projects` field."""

    def test_inherits_load_result_fields(self) -> None:
        from lib_python_config import LoadResult
        assert issubclass(ProjectsLoadResult, LoadResult)

    def test_default_projects_is_empty_list(self) -> None:
        r = ProjectsLoadResult(state="no_config", search_root="/x")
        assert r.projects == []
        assert r.state == "no_config"

    def test_projects_carried_through(self) -> None:
        p = ProjectConfig(id="x", provider="github", path="a/b")
        r = ProjectsLoadResult(
            state="ok",
            search_root="/x",
            projects=[p],
        )
        assert len(r.projects) == 1
        assert r.projects[0].id == "x"


class TestTokenAvailableField:
    """`token_env` is excluded from serialization; `token_available` is
    exposed as a computed boolean that reflects whether the named env var
    holds a non-empty value at call time.
    """

    def test_token_available_false_when_token_env_is_none(self) -> None:
        p = _make_project()
        assert p.token_available is False

    def test_token_available_false_when_env_var_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MY_TEST_TOKEN", raising=False)
        p = _make_project(token_env="MY_TEST_TOKEN")
        assert p.token_available is False

    def test_token_available_false_when_env_var_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_TEST_TOKEN", "")
        p = _make_project(token_env="MY_TEST_TOKEN")
        assert p.token_available is False

    def test_token_available_true_when_env_var_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_TEST_TOKEN", "secret-value")
        p = _make_project(token_env="MY_TEST_TOKEN")
        assert p.token_available is True

    def test_token_env_excluded_from_model_dump(self) -> None:
        p = _make_project(token_env="MY_TEST_TOKEN")
        assert "token_env" not in p.model_dump()
        # The JSON wire format (MCP-facing) must also omit it.
        assert "token_env" not in json.loads(p.model_dump_json())

    def test_token_available_in_model_dump(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_TEST_TOKEN", "secret-value")
        p = _make_project(token_env="MY_TEST_TOKEN")
        d = p.model_dump()
        assert "token_available" in d
        assert d["token_available"] is True

    def test_token_available_false_in_model_dump_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MY_TEST_TOKEN", raising=False)
        p = _make_project(token_env="MY_TEST_TOKEN")
        d = p.model_dump()
        assert "token_available" in d
        assert d["token_available"] is False
