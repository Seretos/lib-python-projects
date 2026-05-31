"""Tests for `lib_python_projects.finder.find_projects`.

Includes the regression test for ticket #64 (nonsense query must return
empty matches, not false-positive results) plus edge-case coverage.
"""
from __future__ import annotations

import pytest

from lib_python_projects import FindResult, ProjectConfig, ProjectMatch, find_projects
from lib_python_projects.finder import DEFAULT_MIN_SCORE, HIGH_CONFIDENCE_SCORE, RELATIVE_SCORE_CUTOFF


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


# ---------------------------------------------------------------------------
# Regression tests — dominant-match noise suppression (ticket #74)
# ---------------------------------------------------------------------------


class TestDominantMatchNoiseSuppression:
    """When the top match clears HIGH_CONFIDENCE_SCORE, low-scoring noise
    must be suppressed.  When the top score is below the threshold, all
    results above the floor are returned unchanged.
    """

    def test_exact_id_match_suppresses_low_scoring_noise(self) -> None:
        """Exact-id query (score 1.0) suppresses incidental lower-scoring hits.

        Regression for the reported symptom: querying "agent-project-issues"
        should return only the project whose id is "agent-project-issues"
        (score 1.0), not also "some-issues-tracker" or "project-management"
        whose incidental token overlaps score ~0.3–0.45.
        """
        # "agent-project-issues" matches id exactly → F1 = 1.0
        p_exact = _make_project(id="agent-project-issues", path="Seretos/agent-project-issues")
        # "some-issues-tracker" shares "issues" token with query but is far
        # from a full match.  Expected F1 ~ 0.35 (well below 1.0 * 0.5).
        p_noise1 = _make_project(id="some-issues-tracker", path="acme/tracker")
        # "project-management" shares "project" token.
        p_noise2 = _make_project(id="project-management", path="acme/pm")

        result = find_projects(
            [p_exact, p_noise1, p_noise2],
            query="agent-project-issues",
            fields="id",
        )
        assert len(result.matches) == 1
        assert result.matches[0].project.id == "agent-project-issues"
        assert result.matches[0].score == pytest.approx(1.0)

    def test_no_dominant_hit_preserves_all_above_floor(self) -> None:
        """When no match clears HIGH_CONFIDENCE_SCORE, all results above
        the floor are returned (gating must not fire).

        Both "project-alpha" and "project-beta" share "project" with the
        query.  The top score (~0.5) is below HIGH_CONFIDENCE_SCORE (0.7)
        so neither is suppressed.
        """
        # query "project": 1 query token, each id has 2 tokens.
        # precision = 1/1 = 1.0, recall = 1/2 = 0.5 → F1 = 2/3 ≈ 0.667
        # 0.667 < HIGH_CONFIDENCE_SCORE (0.7) → gating must NOT fire
        p1 = _make_project(id="project-alpha", path="acme/project-alpha")
        p2 = _make_project(id="project-beta", path="acme/project-beta")

        result = find_projects([p1, p2], query="project", fields="id")
        # Both must survive because top score does not clear the threshold
        ids = {m.project.id for m in result.matches}
        assert "project-alpha" in ids
        assert "project-beta" in ids

    def test_two_equal_high_confidence_matches_both_survive(self) -> None:
        """Two projects each scoring F1 = 1.0 must both appear in results.

        When top_score >= HIGH_CONFIDENCE_SCORE and the second match also
        equals top_score, the relative cutoff (top*0.5) keeps both.
        """
        p1 = _make_project(id="config", path="acme/config")
        p2 = _make_project(id="config", path="other/config")
        # Two distinct ProjectConfig objects with same id both score 1.0
        # for query "config"; both should survive the cutoff.
        result = find_projects([p1, p2], query="config", fields="id")
        assert len(result.matches) == 2
        for m in result.matches:
            assert m.score == pytest.approx(1.0)

    def test_moderate_top_score_no_suppression(self) -> None:
        """When top_score < HIGH_CONFIDENCE_SCORE, the relative cutoff is
        NOT applied, even if a second result scores much lower.

        query "agent" against ids ["agent-project-issues", "project-management"]:
        - "agent-project-issues": precision=1.0, recall=1/3≈0.333 → F1≈0.5
        - "project-management":  "agent" hits "management"? No (low ratio).
          Actually recall = 0, so F1 = 0.  But to make this test meaningful
          we need a second project that IS above the floor.
        Use query "tracker" against "issue-tracker" (F1≈0.5) and
        "bug-tracker-pro" (F1 slightly lower but still above floor).
        """
        # query "tracker": 1 token
        # "issue-tracker" has tokens ["issue","tracker"]: precision=1/1=1.0,
        #   recall=1/2=0.5, F1=2/3≈0.667 — below HIGH_CONFIDENCE_SCORE
        # "bug-tracker-pro" has tokens ["bug","tracker","pro"]: precision=1.0,
        #   recall=1/3≈0.333, F1=0.5 — above DEFAULT_MIN_SCORE (0.3)
        p1 = _make_project(id="issue-tracker", path="acme/issue-tracker")
        p2 = _make_project(id="bug-tracker-pro", path="acme/bug-tracker-pro")

        result = find_projects([p1, p2], query="tracker", fields="id")
        ids = {m.project.id for m in result.matches}
        # top score ~0.667 < HIGH_CONFIDENCE_SCORE (0.7): no suppression
        assert "issue-tracker" in ids
        assert "bug-tracker-pro" in ids

    def test_high_confidence_constants_exported(self) -> None:
        """HIGH_CONFIDENCE_SCORE and RELATIVE_SCORE_CUTOFF must be importable
        from lib_python_projects.finder and have values in (0, 1)."""
        assert isinstance(HIGH_CONFIDENCE_SCORE, float)
        assert 0.0 < HIGH_CONFIDENCE_SCORE < 1.0
        assert isinstance(RELATIVE_SCORE_CUTOFF, float)
        assert 0.0 < RELATIVE_SCORE_CUTOFF < 1.0
