"""Unit tests for `lib_python_projects.models`.

The bulk of the schema validation is already covered by the migrated
`test_config.py` (formerly `agent-project-issues/tests/test_config.py`).
This file focuses on the new-in-v0.1.0 surface: `ProjectConfig.local_path`.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from lib_python_projects import (
    AzureBoardsBinding,
    Board,
    GithubProjectsV2Binding,
    ProjectConfig,
    ProjectsLoadResult,
)


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


class TestBoard:
    """`board` is new in ticket #117 — optional ordered `columns` plus a
    provider-discriminated `binding`. Schema + model only; no provider
    board-resolution logic (that's #118/#119)."""

    def test_board_none_by_default(self) -> None:
        p = _make_project()
        assert p.board is None

    def test_board_with_github_binding_accepted(self) -> None:
        p = _make_project(
            board={
                "columns": ["Todo", "Doing", "Done"],
                "binding": {
                    "kind": "github-projects-v2",
                    "map": {"Todo": "Backlog"},
                },
            }
        )
        assert isinstance(p.board, Board)
        assert p.board.columns == ["Todo", "Doing", "Done"]
        assert isinstance(p.board.binding, GithubProjectsV2Binding)
        assert p.board.binding.map == {"Todo": "Backlog"}

    def test_board_with_azure_binding_accepted(self) -> None:
        p = _make_project(
            board={
                "columns": ["Todo", "Doing", "Done"],
                "binding": {"kind": "azure-boards"},
            }
        )
        assert isinstance(p.board.binding, AzureBoardsBinding)

    def test_resolve_returns_mapped_value(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(
                kind="github-projects-v2", map={"Todo": "Backlog"}
            ),
        )
        assert board.resolve("Todo") == "Backlog"

    def test_resolve_falls_back_to_identity_when_unmapped(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(
                kind="github-projects-v2", map={"Todo": "Backlog"}
            ),
        )
        assert board.resolve("Doing") == "Doing"

    def test_resolve_falls_back_to_identity_when_no_map(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
        )
        assert board.resolve("Todo") == "Todo"

    def test_resolve_matches_map_key_case_insensitively(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(
                kind="github-projects-v2", map={"todo": "Backlog"}
            ),
        )
        assert board.resolve("Todo") == "Backlog"

    def test_empty_columns_rejected(self) -> None:
        with pytest.raises(ValidationError, match="columns"):
            Board(
                columns=[],
                binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            )

    def test_case_insensitive_duplicate_columns_rejected(self) -> None:
        with pytest.raises(ValidationError, match="[Dd]one"):
            Board(
                columns=["Done", "done"],
                binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            )

    def test_map_key_not_in_columns_rejected(self) -> None:
        with pytest.raises(ValidationError, match="stray"):
            Board(
                columns=["Todo", "Doing", "Done"],
                binding=GithubProjectsV2Binding(
                    kind="github-projects-v2", map={"stray": "x"}
                ),
            )

    def test_map_key_matching_column_case_insensitively_accepted(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(
                kind="github-projects-v2", map={"DONE": "Closed"}
            ),
        )
        assert board.binding.map == {"DONE": "Closed"}

    def test_missing_kind_raises_discriminator_error(self) -> None:
        with pytest.raises(ValidationError):
            Board(
                columns=["Todo"],
                binding={"map": {"Todo": "x"}},  # type: ignore[arg-type]
            )

    def test_unknown_kind_raises_discriminator_error(self) -> None:
        with pytest.raises(ValidationError):
            Board(
                columns=["Todo"],
                binding={"kind": "trello"},  # type: ignore[arg-type]
            )

    def test_unknown_key_inside_binding_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GithubProjectsV2Binding(kind="github-projects-v2", owner="acme")  # type: ignore[call-arg]

    def test_provider_extras_accepts_arbitrary_dict(self) -> None:
        binding = GithubProjectsV2Binding(
            kind="github-projects-v2",
            provider_extras={"anything": 1, "goes": [1, 2, 3]},
        )
        assert binding.provider_extras == {"anything": 1, "goes": [1, 2, 3]}

    def test_project_config_board_appears_in_model_dump(self) -> None:
        p = _make_project(
            board={
                "columns": ["Todo"],
                "binding": {"kind": "azure-boards"},
            }
        )
        d = p.model_dump()
        assert d["board"]["columns"] == ["Todo"]
        assert d["board"]["binding"]["kind"] == "azure-boards"


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
