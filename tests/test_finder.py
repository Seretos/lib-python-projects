"""Tests for `lib_python_projects.finder.find_projects`.

Includes the regression test for ticket #64 (nonsense query must return
empty matches, not false-positive results) plus edge-case coverage.
"""
from __future__ import annotations

import pytest

from lib_python_projects import FindResult, ProjectConfig, ProjectMatch, find_projects
from lib_python_projects.finder import DEFAULT_MIN_SCORE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(
    id: str = "test-project",
    description: str = "",
    path: str = "owner/repo",
    provider: str = "github",
) -> ProjectConfig:
    return ProjectConfig(id=id, description=description, path=path, provider=provider)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Regression test — the reported problem (ticket #64)
# ---------------------------------------------------------------------------


class TestNonsenseQueryReturnsEmpty:
    """Regression: purely nonsense queries must not surface false positives."""

    def test_nonsense_query_returns_empty_matches(self) -> None:
        """Querying with 'nonexistent-project-xyz-99' against real projects
        must return no matches and set the hint."""
        projects = [
            _make_project(
                id="agent-project-issues",
                description="Issue tracking",
                path="Seretos/agent-project-issues",
            ),
            _make_project(
                id="lib-python-config",
                description="Python config library",
                path="Seretos/lib-python-config",
            ),
        ]
        result = find_projects(projects, query="nonexistent-project-xyz-99", fields="full")
        assert result.matches == []
        assert result.hint == "no matches above relevance floor"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEmptyProjectList:
    """Empty input → empty matches, hint is None (no candidates at all)."""

    def test_empty_project_list_returns_no_matches_and_no_hint(self) -> None:
        result = find_projects([], query="anything")
        assert result.matches == []
        assert result.hint is None


class TestExactIdMatch:
    """An exact id query should produce a score of 1.0 and rank first."""

    def test_exact_id_match_scores_one_and_ranks_first(self) -> None:
        p1 = _make_project(id="agent-project-issues", path="Seretos/agent-project-issues")
        p2 = _make_project(id="lib-python-config", path="Seretos/lib-python-config")
        result = find_projects([p1, p2], query="agent-project-issues", fields="id")
        assert len(result.matches) >= 1
        top = result.matches[0]
        assert top.project.id == "agent-project-issues"
        assert top.score == pytest.approx(1.0)


class TestPartialIdMatch:
    """A partial query ('agent') should still match 'agent-project-issues'."""

    def test_partial_id_match_is_above_floor(self) -> None:
        p = _make_project(id="agent-project-issues", path="Seretos/agent-project-issues")
        result = find_projects([p], query="agent", fields="id")
        assert len(result.matches) == 1
        assert result.matches[0].project.id == "agent-project-issues"
        assert result.matches[0].score >= DEFAULT_MIN_SCORE


class TestDescriptionOnlyMatch:
    """When fields='full', a query matching only the description is surfaced."""

    def test_description_only_match_appears_in_full_fields(self) -> None:
        p = _make_project(
            id="zzz-unrelated",
            description="configuration loader",
            path="acme/zzz",
        )
        result = find_projects([p], query="configuration", fields="full")
        assert len(result.matches) == 1
        assert result.matches[0].project.id == "zzz-unrelated"

    def test_description_only_match_absent_in_id_fields(self) -> None:
        """Same query with fields='id' must return empty because 'configuration'
        does not appear in the id 'zzz-unrelated'."""
        p = _make_project(
            id="zzz-unrelated",
            description="configuration loader",
            path="acme/zzz",
        )
        result = find_projects([p], query="configuration", fields="id")
        assert result.matches == []
        assert result.hint == "no matches above relevance floor"


class TestMultipleMatchesSortedDescending:
    """When multiple projects match, they must be sorted descending by score."""

    def test_matches_sorted_descending_by_score(self) -> None:
        p_exact = _make_project(id="config", path="acme/config")
        p_partial = _make_project(id="config-extended", path="acme/config-extended")
        result = find_projects([p_partial, p_exact], query="config", fields="id")
        assert len(result.matches) >= 2
        scores = [m.score for m in result.matches]
        assert scores == sorted(scores, reverse=True)
        # The exact match 'config' must be first
        assert result.matches[0].project.id == "config"


class TestMinScoreOverride:
    """Custom min_score values behave as expected."""

    def test_min_score_zero_returns_all_projects(self) -> None:
        p1 = _make_project(id="agent-project-issues", path="Seretos/api")
        p2 = _make_project(id="lib-python-config", path="Seretos/lpc")
        result = find_projects(
            [p1, p2], query="nonexistent-project-xyz-99", fields="full", min_score=0.0
        )
        assert len(result.matches) == 2
        assert result.hint is None

    def test_min_score_one_filters_non_exact_matches(self) -> None:
        p_exact = _make_project(id="config", path="acme/config")
        p_partial = _make_project(id="config-extended", path="acme/config-extended")
        result = find_projects([p_exact, p_partial], query="config", fields="id", min_score=1.0)
        # Only the project whose id equals the query exactly survives
        ids = [m.project.id for m in result.matches]
        assert "config" in ids
        assert "config-extended" not in ids


class TestUnknownFieldsRaisesValueError:
    """Unknown fields value must raise ValueError."""

    def test_unknown_fields_raises_value_error(self) -> None:
        p = _make_project()
        with pytest.raises(ValueError, match="Unknown fields value"):
            find_projects([p], query="test", fields="description")  # type: ignore[arg-type]


class TestHintBehaviourWhenMatchesFound:
    """hint must be None when at least one match is above the floor."""

    def test_hint_is_none_when_matches_present(self) -> None:
        p = _make_project(id="config", path="acme/config")
        result = find_projects([p], query="config", fields="id")
        assert result.matches != []
        assert result.hint is None


# ---------------------------------------------------------------------------
# Type and export smoke tests
# ---------------------------------------------------------------------------


class TestPublicApiExports:
    """Sanity-check that the new symbols are accessible from the package root."""

    def test_find_projects_importable_from_package(self) -> None:
        import lib_python_projects as pkg
        assert hasattr(pkg, "find_projects")

    def test_find_result_importable_from_package(self) -> None:
        import lib_python_projects as pkg
        assert hasattr(pkg, "FindResult")

    def test_project_match_importable_from_package(self) -> None:
        import lib_python_projects as pkg
        assert hasattr(pkg, "ProjectMatch")

    def test_find_result_is_pydantic_model(self) -> None:
        from pydantic import BaseModel
        assert issubclass(FindResult, BaseModel)

    def test_project_match_is_pydantic_model(self) -> None:
        from pydantic import BaseModel
        assert issubclass(ProjectMatch, BaseModel)
