"""Unit tests for `lib_python_projects.models`.

The bulk of the schema validation is already covered by the migrated
`test_config.py` (formerly `agent-project-issues/tests/test_config.py`).
This file focuses on the new-in-v0.1.0 surface: `ProjectConfig.local_path`.
"""
from __future__ import annotations

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult


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
