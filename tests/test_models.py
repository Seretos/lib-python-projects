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
    BoardAutoLabels,
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


class TestAreaPathField:
    """`area_path` is new in ticket #172 — Azure-DevOps-only scoping field,
    defaults to `None`, so existing `projects.yml` entries (any provider)
    behave exactly as before."""

    def test_area_path_defaults_to_none(self) -> None:
        p = ProjectConfig(
            id="x", provider="azuredevops", path="acme-org/acme-project/acme-repo"
        )
        assert p.area_path is None

    def test_area_path_round_trips_when_provided(self) -> None:
        p = ProjectConfig(
            id="x",
            provider="azuredevops",
            path="acme-org/acme-project/acme-repo",
            area_path="acme-project\\Team A",
        )
        assert p.area_path == "acme-project\\Team A"

    def test_area_path_appears_in_model_dump(self) -> None:
        p = ProjectConfig(
            id="x",
            provider="azuredevops",
            path="acme-org/acme-project/acme-repo",
            area_path="acme-project\\Team A",
        )
        d = p.model_dump()
        assert d["area_path"] == "acme-project\\Team A"

    def test_area_path_survives_dump_and_reload_round_trip(self) -> None:
        """Serializing via `model_dump()` and reconstructing (the same
        shape the YAML loader round-trips through) preserves `area_path`."""
        p = ProjectConfig(
            id="x",
            provider="azuredevops",
            path="acme-org/acme-project/acme-repo",
            area_path="acme-project\\Team A",
        )
        reloaded = ProjectConfig(**p.model_dump(exclude={"token_available"}))
        assert reloaded.area_path == "acme-project\\Team A"

    def test_area_path_ignored_by_github(self) -> None:
        """`area_path` is accepted (schema-wide field) but documented as
        ignored by non-Azure providers; it should not affect github's
        derived properties or validation."""
        p = ProjectConfig(
            id="x",
            provider="github",
            path="acme/backend",
            area_path="some-area",
        )
        assert p.area_path == "some-area"
        assert p.owner == "acme"
        assert p.repo == "backend"

    def test_unknown_field_still_forbidden_alongside_area_path(self) -> None:
        """`area_path` is whitelisted — other unknown keys must still be
        rejected (`extra="forbid"` on the model)."""
        with pytest.raises(ValidationError):
            ProjectConfig(
                id="x",
                provider="azuredevops",
                path="acme-org/acme-project/acme-repo",
                area_path="acme-project\\Team A",
                bogus_field="nope",  # type: ignore[call-arg]
            )


class TestAutoLabels:
    """`auto_labels` is new in ticket #153 — per-project AI-attribution
    names, defaulting to the module-level `ai-generated`/`ai-modified`
    constants from `markers.py` when unset."""

    def test_auto_labels_defaults_to_module_constants(self) -> None:
        p = _make_project()
        assert p.auto_labels.ai_generated == "ai-generated"
        assert p.auto_labels.ai_modified == "ai-modified"

    def test_auto_labels_accepts_custom_names(self) -> None:
        p = _make_project(
            auto_labels={"ai_generated": "robot-made", "ai_modified": "robot-touched"}
        )
        assert p.auto_labels.ai_generated == "robot-made"
        assert p.auto_labels.ai_modified == "robot-touched"

    def test_auto_labels_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            _make_project(auto_labels={"bogus_key": "nope"})

    def test_auto_labels_rejects_empty_ai_generated(self) -> None:
        with pytest.raises(ValidationError):
            _make_project(auto_labels={"ai_generated": ""})

    def test_auto_labels_rejects_empty_ai_modified(self) -> None:
        with pytest.raises(ValidationError):
            _make_project(auto_labels={"ai_modified": ""})

    def test_auto_labels_appears_in_model_dump(self) -> None:
        p = _make_project(auto_labels={"ai_generated": "robot-made"})
        d = p.model_dump()
        assert d["auto_labels"] == {
            "ai_generated": "robot-made",
            "ai_modified": "ai-modified",
        }


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
        """`owner` is now a real field (ticket #118) — use a genuinely
        unknown key to exercise `extra="forbid"`."""
        with pytest.raises(ValidationError):
            GithubProjectsV2Binding(kind="github-projects-v2", bogus="x")  # type: ignore[call-arg]

    def test_owner_project_number_status_field_accepted(self) -> None:
        """New in ticket #118: `owner`/`project_number`/`status_field`
        are real fields on `GithubProjectsV2Binding`."""
        binding = GithubProjectsV2Binding(
            kind="github-projects-v2",
            owner="acme",
            project_number=7,
            status_field="Workflow",
        )
        assert binding.owner == "acme"
        assert binding.project_number == 7
        assert binding.status_field == "Workflow"

    def test_status_field_defaults_to_status(self) -> None:
        binding = GithubProjectsV2Binding(kind="github-projects-v2")
        assert binding.status_field == "Status"
        assert binding.owner is None
        assert binding.project_number is None

    def test_iteration_field_accepted(self) -> None:
        """New in ticket #151: `iteration_field` names the Projects-v2
        iteration field backing the normalized `Ticket.milestone`
        projection."""
        binding = GithubProjectsV2Binding(
            kind="github-projects-v2",
            owner="acme",
            project_number=7,
            iteration_field="Sprint",
        )
        assert binding.iteration_field == "Sprint"

    def test_iteration_field_defaults_to_none(self) -> None:
        binding = GithubProjectsV2Binding(kind="github-projects-v2")
        assert binding.iteration_field is None

    def test_iteration_field_unknown_key_still_rejected(self) -> None:
        """`extra="forbid"` still rejects genuinely unknown keys even
        after adding `iteration_field`."""
        with pytest.raises(ValidationError):
            GithubProjectsV2Binding(
                kind="github-projects-v2", iteration_sprint="x",
            )  # type: ignore[call-arg]

    def test_board_with_enriched_github_binding_validates_and_resolves(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(
                kind="github-projects-v2",
                owner="acme",
                project_number=3,
                status_field="Status",
                map={"Todo": "Backlog"},
            ),
        )
        assert board.binding.owner == "acme"
        assert board.binding.project_number == 3
        assert board.resolve("Todo") == "Backlog"
        assert board.resolve("Doing") == "Doing"

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


class TestBoardAutoLabels:
    """`board.auto_labels` is new in ticket #154 — board-column-dependent
    auto-labels, distinct/independent from the top-level
    `ProjectConfig.auto_labels` (AI attribution)."""

    def test_defaults_to_empty(self) -> None:
        labels = BoardAutoLabels()
        assert labels.on_create == []
        assert labels.on_update == []
        assert labels.on_move_to == {}

    def test_rejects_unknown_key(self) -> None:
        with pytest.raises(ValidationError):
            BoardAutoLabels(bogus="nope")  # type: ignore[call-arg]

    def test_board_defaults_auto_labels_to_empty(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
        )
        assert board.auto_labels.on_create == []
        assert board.auto_labels.on_update == []
        assert board.auto_labels.on_move_to == {}

    def test_on_move_to_key_matching_column_case_insensitively_accepted(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            auto_labels={"on_move_to": {"done": ["shipped"]}},
        )
        assert board.auto_labels.on_move_to == {"done": ["shipped"]}

    def test_on_move_to_key_not_in_columns_rejected(self) -> None:
        with pytest.raises(ValidationError, match="stray"):
            Board(
                columns=["Todo", "Doing", "Done"],
                binding=GithubProjectsV2Binding(kind="github-projects-v2"),
                auto_labels={"on_move_to": {"stray": ["x"]}},
            )

    def test_on_move_to_empty_label_list_accepted(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            auto_labels={"on_move_to": {"Done": []}},
        )
        assert board.auto_labels.on_move_to == {"Done": []}

    def test_auto_label_names_on_create_dedups_preserving_order(self) -> None:
        board = Board(
            columns=["Todo"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            auto_labels={"on_create": ["b", "a", "b", "c", "a"]},
        )
        assert board.auto_label_names_on_create() == ["b", "a", "c"]

    def test_auto_label_names_on_update_dedups_preserving_order(self) -> None:
        board = Board(
            columns=["Todo"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            auto_labels={"on_update": ["x", "y", "x"]},
        )
        assert board.auto_label_names_on_update() == ["x", "y"]

    def test_auto_label_names_for_move_matches_logical_column_name(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            auto_labels={"on_move_to": {"Done": ["deployed"]}},
        )
        assert board.auto_label_names_for_move("Done") == ["deployed"]
        assert board.auto_label_names_for_move("done") == ["deployed"]

    def test_auto_label_names_for_move_matches_resolved_native_value(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(
                kind="github-projects-v2", map={"Done": "Closed"}
            ),
            auto_labels={"on_move_to": {"Done": ["deployed"]}},
        )
        assert board.auto_label_names_for_move("Closed") == ["deployed"]
        assert board.auto_label_names_for_move("closed") == ["deployed"]

    def test_auto_label_names_for_move_no_match_returns_empty(self) -> None:
        board = Board(
            columns=["Todo", "Doing", "Done"],
            binding=GithubProjectsV2Binding(kind="github-projects-v2"),
            auto_labels={"on_move_to": {"Done": ["deployed"]}},
        )
        assert board.auto_label_names_for_move("Doing") == []

    def test_auto_label_names_for_move_dedups_across_matching_keys(self) -> None:
        """Two logical columns that resolve to the same native value both
        match a single moved-to value; labels dedup across the union."""
        board = Board(
            columns=["Doing", "Review", "Done"],
            binding=GithubProjectsV2Binding(
                kind="github-projects-v2",
                map={"Doing": "InProgress", "Review": "InProgress"},
            ),
            auto_labels={
                "on_move_to": {
                    "Doing": ["in-progress", "shared"],
                    "Review": ["shared", "reviewing"],
                }
            },
        )
        assert board.auto_label_names_for_move("InProgress") == [
            "in-progress",
            "shared",
            "reviewing",
        ]


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

    def test_token_available_docstring_disclaims_authentication(self) -> None:
        """Ticket #231 (Finding 2, step 8): `token_available` currently has
        no docstring, which lets callers conflate "token_env is set" with
        "the token actually authenticates" — the exact confusion the
        `probe_token_capabilities` partial-flags work exists to correct.

        Pydantic's `@computed_field` auto-populates
        `ComputedFieldInfo.description` from the wrapped property's
        docstring when no explicit `description=` is passed, so a real
        docstring on the property is enough to satisfy this without any
        schema change.

        Expected RED: today the property has no docstring, so
        `description` is `None` and this test fails at the first
        `assert ... is not None`.
        """
        from lib_python_projects import ProjectConfig

        info = ProjectConfig.model_computed_fields["token_available"]
        doc = info.description
        assert doc is not None, (
            "token_available has no description — add a docstring to the "
            "property so pydantic's computed_field picks it up"
        )
        assert "token_env" in doc or "environment" in doc, (
            f"description does not explain the env-var-only meaning: {doc!r}"
        )
        assert ("not evidence" in doc or "does not mean" in doc) and (
            "authenticat" in doc or "authoriz" in doc
        ), (
            "description does not explicitly disclaim authentication/"
            f"authorization: {doc!r}"
        )


class TestPipelinesPermissions:
    """`PipelinesPermissions` is new in ticket #200 — a `pipelines.trigger`
    opt-in namespace, importable from the package root like its siblings.
    """

    def test_importable_from_package_root(self) -> None:
        from lib_python_projects import PipelinesPermissions

        perms = PipelinesPermissions()
        assert perms.trigger is False

    def test_permissions_default_includes_pipelines_namespace(self) -> None:
        from lib_python_projects import Permissions, PipelinesPermissions

        perms = Permissions()
        assert isinstance(perms.pipelines, PipelinesPermissions)
        assert perms.pipelines.trigger is False


class TestPermissionsVerifiedField:
    """Ticket #252 gen 2 — `Permissions.verified` / `Permissions.reason`
    on a bare `Permissions` instance. Since gen 2, these are derived,
    non-input fields (private attrs + computed properties) that default
    to unprobed on *any* `Permissions`, bare or nested inside a
    `ProjectConfig` — there is no longer a separate `ProjectConfig`-level
    validator that stamps the "not_probed" reason on top; see
    `tests/test_loader.py::TestPermissionsVerification` for the
    `ProjectConfig`/loader-level coverage of the same invariant, and only
    `Permissions.from_probe` can produce a probed state (see
    `test_forged_verified_unreachable_even_with_matching_source` below).
    """

    def test_bare_permissions_defaults_unverified_and_not_probed(self) -> None:
        """A `Permissions` built directly (no `ProjectConfig` wrapper, no
        `from_probe`) defaults to `verified=False, reason="not_probed"` —
        the private-attr defaults are the single source of the "unprobed"
        state, shared by bare `Permissions()` and any `ProjectConfig`'s
        nested `permissions` alike."""
        from lib_python_projects import Permissions

        perms = Permissions()
        assert perms.verified is False
        assert perms.reason == "not_probed"

    def test_permissions_serializes_verified_and_reason_at_same_level(self) -> None:
        """`verified`/`reason` sit in the same `permissions` object as
        `issues`/`pulls`/`board`/`pipelines` — the same nesting level a
        renderer already walks — not a separate top-level block."""
        p = _make_project()
        dumped = p.model_dump()["permissions"]
        assert set(dumped.keys()) == {
            "issues",
            "pulls",
            "board",
            "pipelines",
            "verified",
            "reason",
        }
        assert dumped["verified"] is False
        assert dumped["reason"] == "not_probed"

    def test_forged_verified_unreachable_even_with_matching_source(self) -> None:
        """Ticket #252 gen 2 (R1): `permissions.verified` must be
        unforgeable *at the model*, not merely gated by `source` — the
        gen-1 `_check_permissions_verification` validator only resets
        `verified`/`reason` when `source != "token-discovery"`, so a
        `ProjectConfig` built directly (e.g. `model_validate`, bypassing
        the loader boundary that always forces `source="config"`) with a
        forged `source="token-discovery"` *and* a hand-forged
        `permissions.verified=True` sails straight through unchanged.
        `verified`/`reason` must instead be derived, non-input fields that
        only a real probe (`Permissions.from_probe`) can set — regardless
        of what `source` the caller claims. Declared `issues`/`pulls`
        flags must still survive untouched."""
        p = ProjectConfig.model_validate(
            {
                "id": "x",
                "provider": "github",
                "path": "acme/backend",
                "source": "token-discovery",
                "permissions": {
                    "issues": {"create": True, "modify": True},
                    "pulls": {"create": True},
                    "verified": True,
                    "reason": "hand-forged",
                },
            }
        )
        assert p.permissions.verified is False
        assert p.permissions.reason == "not_probed"
        assert p.permissions.issues.create is True
        assert p.permissions.issues.modify is True
        assert p.permissions.pulls.create is True

    def test_direct_constructor_forgery_also_unreachable(self) -> None:
        """Test-critic note: the design (`PrivateAttr` + `computed_field`)
        is inherently unreachable from `__init__` too, not just from
        `model_validate` on a mapping — pin that down directly. A direct
        `Permissions(verified=True, reason="x")` call does not raise (the
        `mode="before"` validator silently drops the two computed-only
        keys before field validation, the same as it does for a mapping
        routed through `model_validate`) and does not produce a forged
        `verified=True`."""
        from lib_python_projects import Permissions

        perms = Permissions(verified=True, reason="x")  # type: ignore[call-arg]
        assert perms.verified is False
        assert perms.reason == "not_probed"
