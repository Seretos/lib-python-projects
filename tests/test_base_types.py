"""Tests for ticket #80 — token discovery foundation types.

Covers:
- Source Literal widening in ProjectConfig ("token-discovery").
- Importability of the three new types from lib_python_projects.providers.base.
- DiscoveredProject dataclass construction and round-trip.
- ProjectDiscoveryResult defaults, failure-contract taxonomy, and truncation.
- TokenProjectDiscoveryProvider interface (NotImplementedError, keyword-only
  limit, subclass contract).
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest


# ---------- helpers -----------------------------------------------------------


def _make_project_config(**kwargs):
    from lib_python_projects import ProjectConfig

    return ProjectConfig(id="x", provider="github", path="acme/backend", **kwargs)


def _make_capabilities(**kwargs):
    from lib_python_projects.providers.base import TokenCapabilities

    return TokenCapabilities(**kwargs)


# ---------- Source Literal widening ------------------------------------------


class TestSourceLiteralWidening:
    """ProjectConfig.source now accepts 'token-discovery' in addition to the
    two pre-existing values."""

    def test_token_discovery_source_is_valid(self):
        p = _make_project_config(source="token-discovery")
        assert p.source == "token-discovery"

    def test_config_source_still_valid(self):
        p = _make_project_config(source="config")
        assert p.source == "config"

    def test_git_remote_source_still_valid(self):
        p = _make_project_config(source="git-remote")
        assert p.source == "git-remote"

    def test_unknown_source_raises_validation_error(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_project_config(source="bogus-source")  # type: ignore[arg-type]


# ---------- importability of new types ----------------------------------------


class TestImportability:
    """All three new types must be importable from
    lib_python_projects.providers.base — and only from there (no top-level
    re-export)."""

    def test_discovered_project_importable(self):
        from lib_python_projects.providers.base import DiscoveredProject  # noqa: F401

    def test_project_discovery_result_importable(self):
        from lib_python_projects.providers.base import ProjectDiscoveryResult  # noqa: F401

    def test_token_project_discovery_provider_importable(self):
        from lib_python_projects.providers.base import TokenProjectDiscoveryProvider  # noqa: F401


# ---------- DiscoveredProject dataclass ---------------------------------------


class TestDiscoveredProject:
    def test_required_only_construction_yields_defaults(self):
        from lib_python_projects.providers.base import DiscoveredProject

        caps = _make_capabilities()
        dp = DiscoveredProject(
            provider="github",
            path="owner/repo",
            permissions=caps,
        )
        assert dp.provider == "github"
        assert dp.path == "owner/repo"
        assert dp.permissions is caps
        assert dp.description == ""
        assert dp.default_work_item_type is None
        assert dp.base_url is None

    def test_all_fields_round_trip(self):
        from lib_python_projects.providers.base import DiscoveredProject

        caps = _make_capabilities(issues_create=True, pulls_create=True)
        dp = DiscoveredProject(
            provider="gitlab",
            path="namespace/project",
            permissions=caps,
            description="My project",
            default_work_item_type="Issue",
            base_url="https://gitlab.example.com",
        )
        assert dp.provider == "gitlab"
        assert dp.path == "namespace/project"
        assert dp.permissions.issues_create is True
        assert dp.permissions.pulls_create is True
        assert dp.description == "My project"
        assert dp.default_work_item_type == "Issue"
        assert dp.base_url == "https://gitlab.example.com"

    def test_is_dataclass(self):
        from lib_python_projects.providers.base import DiscoveredProject

        assert dataclasses.is_dataclass(DiscoveredProject)

    def test_not_frozen(self):
        """DiscoveredProject must be mutable (mirrors TokenCapabilities pattern)."""
        from lib_python_projects.providers.base import DiscoveredProject

        caps = _make_capabilities()
        dp = DiscoveredProject(provider="github", path="a/b", permissions=caps)
        dp.description = "updated"  # must not raise FrozenInstanceError
        assert dp.description == "updated"

    def test_field_order(self):
        """Required fields come before defaulted ones (dataclass declaration order)."""
        from lib_python_projects.providers.base import DiscoveredProject

        field_names = [f.name for f in dataclasses.fields(DiscoveredProject)]
        assert field_names[:3] == ["provider", "path", "permissions"]
        assert set(field_names[3:]) == {"description", "default_work_item_type", "base_url"}


# ---------- ProjectDiscoveryResult dataclass ----------------------------------


class TestProjectDiscoveryResult:
    def test_bare_defaults(self):
        from lib_python_projects.providers.base import ProjectDiscoveryResult

        r = ProjectDiscoveryResult(projects=[])
        assert r.projects == []
        assert r.truncated is False
        assert r.reason is None

    def test_is_dataclass(self):
        from lib_python_projects.providers.base import ProjectDiscoveryResult

        assert dataclasses.is_dataclass(ProjectDiscoveryResult)

    # --- failure-contract taxonomy ---

    @pytest.mark.parametrize("reason", [
        "bad_credentials",
        "network_error",
        "http_403",
        "repo_invisible_to_token",
        "permissions_field_missing",
        "insufficient_scope",
    ])
    def test_failure_reason_taxonomy(self, reason: str):
        """On failure: projects=[], reason set, truncated=False."""
        from lib_python_projects.providers.base import ProjectDiscoveryResult

        r = ProjectDiscoveryResult(projects=[], reason=reason)
        assert r.projects == []
        assert r.reason == reason
        assert r.truncated is False

    def test_truncated_true_with_non_empty_projects_is_valid(self):
        """truncated=True is NOT a failure — projects may be non-empty."""
        from lib_python_projects.providers.base import (
            DiscoveredProject,
            ProjectDiscoveryResult,
        )

        caps = _make_capabilities()
        dp = DiscoveredProject(provider="github", path="a/b", permissions=caps)
        r = ProjectDiscoveryResult(projects=[dp], truncated=True)
        assert len(r.projects) == 1
        assert r.truncated is True
        assert r.reason is None

    def test_happy_path_reason_is_none(self):
        from lib_python_projects.providers.base import (
            DiscoveredProject,
            ProjectDiscoveryResult,
        )

        caps = _make_capabilities(issues_create=True)
        dp = DiscoveredProject(provider="azuredevops", path="org/proj/repo", permissions=caps)
        r = ProjectDiscoveryResult(projects=[dp])
        assert r.reason is None


# ---------- TokenProjectDiscoveryProvider interface ---------------------------


class TestTokenProjectDiscoveryProvider:
    def test_base_raises_not_implemented(self):
        from lib_python_projects.providers.base import TokenProjectDiscoveryProvider

        provider = TokenProjectDiscoveryProvider()
        with pytest.raises(NotImplementedError):
            provider.discover_projects(token="x", limit=50)

    def test_limit_is_keyword_only(self):
        """discover_projects(token, limit) as positional must raise TypeError."""
        from lib_python_projects.providers.base import TokenProjectDiscoveryProvider

        provider = TokenProjectDiscoveryProvider()
        with pytest.raises(TypeError):
            provider.discover_projects("tok", 50)  # type: ignore[misc]

    def test_subclass_returning_fixed_result_works(self):
        """A minimal inline subclass that returns a real result is accepted."""
        from lib_python_projects.providers.base import (
            ProjectDiscoveryResult,
            TokenProjectDiscoveryProvider,
        )

        class _Stub(TokenProjectDiscoveryProvider):
            def discover_projects(
                self, token: str, *, limit: int
            ) -> ProjectDiscoveryResult:
                return ProjectDiscoveryResult(projects=[])

        stub = _Stub()
        result = stub.discover_projects(token="secret", limit=100)
        assert isinstance(result, ProjectDiscoveryResult)
        assert result.projects == []
        assert result.truncated is False
        assert result.reason is None

    def test_subclass_still_enforces_keyword_only_limit(self):
        """Even after subclassing the keyword-only contract must hold."""
        from lib_python_projects.providers.base import (
            ProjectDiscoveryResult,
            TokenProjectDiscoveryProvider,
        )

        class _Stub(TokenProjectDiscoveryProvider):
            def discover_projects(
                self, token: str, *, limit: int
            ) -> ProjectDiscoveryResult:
                return ProjectDiscoveryResult(projects=[])

        stub = _Stub()
        with pytest.raises(TypeError):
            stub.discover_projects("tok", 50)  # type: ignore[misc]


# ---------- AzureDevOpsProvider implements TokenProjectDiscoveryProvider -----


def test_azuredevops_provider_implements_token_discovery():
    """AzureDevOpsProvider must be a subclass of TokenProjectDiscoveryProvider."""
    from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
    from lib_python_projects.providers.base import TokenProjectDiscoveryProvider

    assert issubclass(AzureDevOpsProvider, TokenProjectDiscoveryProvider)


# ---------- ticket #151: Ticket.parent_id / Ticket.milestone -----------------


def _make_ticket(**overrides):
    from lib_python_projects.providers.base import Ticket

    base = dict(
        id="1",
        title="t",
        body="b",
        status="open",
        author="a",
        assignees=[],
        labels=[],
        url="https://example.invalid/1",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return Ticket(**base)


class TestTicketHierarchyAndMilestoneFields:
    """`Ticket.parent_id` / `Ticket.milestone` are new optional fields
    (ticket #151) — both default to `None` and every pre-existing
    `Ticket(...)` construction call site (which never passes them) must
    keep working unchanged."""

    def test_parent_id_defaults_to_none(self):
        t = _make_ticket()
        assert t.parent_id is None

    def test_milestone_defaults_to_none(self):
        t = _make_ticket()
        assert t.milestone is None

    def test_parent_id_settable(self):
        t = _make_ticket(parent_id="#7")
        assert t.parent_id == "#7"

    def test_milestone_settable(self):
        t = _make_ticket(milestone="v2.0")
        assert t.milestone == "v2.0"

    def test_existing_construction_without_new_fields_still_works(self):
        """A `Ticket(...)` call using only the pre-#151 fields (no
        `parent_id`/`milestone`) must not raise — locks in backward
        compatibility for every existing call site."""
        t = _make_ticket(idempotent_replay=True, custom_fields={"a": 1})
        assert t.parent_id is None
        assert t.milestone is None
        assert t.idempotent_replay is True
        assert t.custom_fields == {"a": 1}


class TestExtractParentId:
    """`_extract_parent_id` is the shared projection helper all three
    providers' `get_ticket` use to populate `Ticket.parent_id` from the
    `_fetch_relations` output — a pure function over a `list[Relation]`,
    no network calls."""

    def test_returns_none_for_empty_relations(self):
        from lib_python_projects.providers.base import _extract_parent_id

        assert _extract_parent_id([]) is None

    def test_returns_none_when_no_parent_relation(self):
        from lib_python_projects.providers.base import Relation, _extract_parent_id

        relations = [
            Relation(
                kind="blocks", ticket_id="#2", title="", url="", state="open",
                is_pull_request=False,
            ),
            Relation(
                kind="child", ticket_id="#3", title="", url="", state="open",
                is_pull_request=False,
            ),
        ]
        assert _extract_parent_id(relations) is None

    def test_returns_parent_ticket_id_when_present(self):
        from lib_python_projects.providers.base import Relation, _extract_parent_id

        relations = [
            Relation(
                kind="blocks", ticket_id="#2", title="", url="", state="open",
                is_pull_request=False,
            ),
            Relation(
                kind="parent", ticket_id="#9", title="Epic", url="", state="open",
                is_pull_request=False,
            ),
        ]
        assert _extract_parent_id(relations) == "#9"

    def test_returns_first_parent_relation_when_multiple(self):
        """Defensive: `_fetch_relations` should never emit two `parent`
        relations, but the helper picks the first deterministically if it
        ever did, rather than raising."""
        from lib_python_projects.providers.base import Relation, _extract_parent_id

        relations = [
            Relation(
                kind="parent", ticket_id="#5", title="", url="", state="open",
                is_pull_request=False,
            ),
            Relation(
                kind="parent", ticket_id="#6", title="", url="", state="open",
                is_pull_request=False,
            ),
        ]
        assert _extract_parent_id(relations) == "#5"


# ---------- ticket #152: PipelineFailure.failures / FailureAnnotation --------


class TestPipelineFailureFailuresProperty:
    """`PipelineFailure.failures` is a computed property (ticket #152)
    flattening `failing_jobs[*].annotations` in job order — not a stored
    field, just a convenience projection."""

    def test_empty_when_no_failing_jobs(self):
        from lib_python_projects.providers.base import PipelineFailure

        pf = PipelineFailure(failing_jobs=[])
        assert pf.failures == []

    def test_empty_when_failing_jobs_have_no_annotations(self):
        from lib_python_projects.providers.base import FailingJob, PipelineFailure

        pf = PipelineFailure(
            failing_jobs=[
                FailingJob(
                    name="build", url="u1", failed_step="compile",
                    annotations=[], log_excerpt=None,
                ),
            ]
        )
        assert pf.failures == []

    def test_flattens_annotations_across_jobs_in_order(self):
        from lib_python_projects.providers.base import (
            FailingJob,
            FailureAnnotation,
            PipelineFailure,
        )

        a1 = FailureAnnotation(step="build", message="m1")
        a2 = FailureAnnotation(step="build", message="m2")
        a3 = FailureAnnotation(step="test", message="m3")
        pf = PipelineFailure(
            failing_jobs=[
                FailingJob(
                    name="build", url="u1", failed_step="compile",
                    annotations=[a1, a2], log_excerpt=None,
                ),
                FailingJob(
                    name="test", url="u2", failed_step="pytest",
                    annotations=[a3], log_excerpt=None,
                ),
            ]
        )
        assert pf.failures == [a1, a2, a3]

    def test_is_not_a_dataclass_field(self):
        """`failures` must be a computed property, not a stored dataclass
        field — `PipelineFailure(...)` construction must not accept it as
        a constructor kwarg."""
        import dataclasses
        from lib_python_projects.providers.base import PipelineFailure

        field_names = {f.name for f in dataclasses.fields(PipelineFailure)}
        assert "failures" not in field_names


class TestFailureAnnotation:
    """`FailureAnnotation` dataclass construction and defaults (ticket #152)."""

    def test_required_only_construction_yields_defaults(self):
        from lib_python_projects.providers.base import FailureAnnotation

        ann = FailureAnnotation(step="build", message="boom")
        assert ann.step == "build"
        assert ann.message == "boom"
        assert ann.file is None
        assert ann.line is None
        assert ann.severity is None
        assert ann.title is None

    def test_all_fields_round_trip(self):
        from lib_python_projects.providers.base import FailureAnnotation

        ann = FailureAnnotation(
            step="build", message="boom", file="src/x.py", line=42,
            severity="failure", title="Compile error",
        )
        assert ann.file == "src/x.py"
        assert ann.line == 42
        assert ann.severity == "failure"
        assert ann.title == "Compile error"

    def test_failing_job_annotations_field_accepts_failure_annotation_list(self):
        from lib_python_projects.providers.base import FailingJob, FailureAnnotation

        ann = FailureAnnotation(step="build", message="boom")
        job = FailingJob(
            name="build", url="u", failed_step="compile",
            annotations=[ann], log_excerpt=None,
        )
        assert job.annotations == [ann]


# ---------- ticket #148: review_decision_from_states + PullRequest.reviews ---


class TestReviewDecisionFromStates:
    """`review_decision_from_states` is the pure helper `get_pr` uses on
    every provider to derive `review_decision` from a list of already
    latest-per-author normalized review states."""

    def test_request_changes_wins_over_approve(self):
        from lib_python_projects.providers.base import review_decision_from_states

        assert (
            review_decision_from_states(["approve", "request_changes"])
            == "CHANGES_REQUESTED"
        )

    def test_approve_only_yields_approved(self):
        from lib_python_projects.providers.base import review_decision_from_states

        assert review_decision_from_states(["approve"]) == "APPROVED"

    def test_comment_only_yields_none(self):
        """A comment-only review carries no approval/blocking signal."""
        from lib_python_projects.providers.base import review_decision_from_states

        assert review_decision_from_states(["comment"]) is None

    def test_empty_states_yields_none(self):
        from lib_python_projects.providers.base import review_decision_from_states

        assert review_decision_from_states([]) is None


def _make_pr(**overrides):
    from lib_python_projects.providers.base import PullRequest

    base = dict(
        id="1",
        number=1,
        title="t",
        body="b",
        status="open",
        draft=False,
        author="a",
        assignees=[],
        reviewers=[],
        requested_reviewers=[],
        labels=[],
        head={},
        base={},
        merged=False,
        mergeable=None,
        url="https://example.invalid/1",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return PullRequest(**base)


# ---------- ticket #191: ViewerIdentity / ViewerIdentityProvider -------------


class TestViewerIdentity:
    """`ViewerIdentity` dataclass construction and defaults (ticket #191)."""

    def test_all_fields_default_to_none(self):
        from lib_python_projects.providers.base import ViewerIdentity

        vi = ViewerIdentity()
        assert vi.login is None
        assert vi.display_name is None
        assert vi.provider is None
        assert vi.reason is None

    def test_field_set_is_exactly_login_display_name_provider_reason(self):
        from lib_python_projects.providers.base import ViewerIdentity

        field_names = {f.name for f in dataclasses.fields(ViewerIdentity)}
        assert field_names == {"login", "display_name", "provider", "reason"}

    def test_is_dataclass(self):
        from lib_python_projects.providers.base import ViewerIdentity

        assert dataclasses.is_dataclass(ViewerIdentity)

    def test_all_fields_round_trip(self):
        from lib_python_projects.providers.base import ViewerIdentity

        vi = ViewerIdentity(
            login="octocat", display_name="The Octocat", provider="github",
        )
        assert vi.login == "octocat"
        assert vi.display_name == "The Octocat"
        assert vi.provider == "github"
        assert vi.reason is None


class TestViewerIdentityProvider:
    def test_base_raises_not_implemented(self):
        from lib_python_projects.providers.base import ViewerIdentityProvider

        provider = ViewerIdentityProvider()
        with pytest.raises(NotImplementedError):
            provider.resolve_viewer_login(project=object(), token="x")


class TestPullRequestReviewsField:
    """`PullRequest.reviews` (ticket #148) defaults to an empty list that
    is independent per instance — the classic mutable-default-argument
    trap dataclasses guard against via `field(default_factory=list)`."""

    def test_defaults_to_empty_list(self):
        pr = _make_pr()
        assert pr.reviews == []

    def test_default_is_independent_per_instance(self):
        """Mutating one instance's `reviews` must not leak into another
        freshly constructed instance — proves the default isn't a single
        shared list object."""
        pr1 = _make_pr()
        pr2 = _make_pr()
        pr1.reviews.append("not-a-real-review-but-proves-independence")
        assert pr2.reviews == []
        assert pr1.reviews != pr2.reviews

    def test_reviews_settable(self):
        from lib_python_projects.providers.base import Review

        rv = Review(
            id="1", state="approve", author="alice", body="lgtm",
            url="https://example.invalid/review/1", submitted_at="2024-01-01T00:00:00Z",
        )
        pr = _make_pr(reviews=[rv])
        assert pr.reviews == [rv]


# ---------- ticket #221 ----------


def test_pullrequest_docstring_documents_per_path_mergeability() -> None:
    """Ticket #221: the `Mergeability note (Issue 6)` block in
    `PullRequest`'s docstring falsely claimed `list_prs` never populates
    `mergeable`/`mergeable_state` and that both are always `None`. Real
    behaviour is per-provider, per-path: GitHub's plain `/pulls` list
    payload simply omits the keys (`_map_pr`'s `raw.get(...)` falls
    through), while `github_batch._map_graphql_pr` hard-codes `None`
    regardless of payload — two different mechanisms previously
    conflated under one rationale; GitLab returns a real tri-state
    `mergeable` from `detailed_merge_status`; GitHub's search-filtered
    path back-fills full payloads via `/search/issues` + `GET
    /pulls/{n}`; and Azure DevOps derives `mergeable` from `mergeStatus`
    (`succeeded`/`rejectedByPolicy`/etc). This guard follows the repo's
    established `inspect.getdoc` pattern (`tests/test_github_board.py`),
    including the `__dict__` own-entry check that bypasses
    `inspect.getdoc`'s MRO fallback to `object.__doc__` — without it,
    this test would still pass even if the docstring were deleted
    entirely (ticket #211 caveat).

    Expected RED: the current docstring still says "never populates" and
    lacks the per-path wording (`tri-state`, `back-fill`, etc).
    """
    from lib_python_projects.providers.base import PullRequest

    assert PullRequest.__dict__["__doc__"] is not None
    doc = inspect.getdoc(PullRequest)
    assert doc is not None

    # False claims must be gone. Note: the corrected text legitimately
    # still contains the phrase "always `None`" (for the GitHub
    # plain-path/batch bullet), so these guards target the specific
    # false claims rather than that substring.
    assert "never populates" not in doc
    assert "does not compute mergeability for cost reasons" not in doc
    assert "Both fields are always `None` in `list_prs` results" not in doc

    # Positive guards: GitLab's tri-state contract.
    assert "tri-state" in doc
    assert "detailed_merge_status" in doc

    # GitHub search-filtered back-fill path (deliberate, test-locked,
    # unchanged).
    assert "back-fill" in doc
    assert "/search/issues" in doc

    # The two distinct GitHub "always None" mechanisms, worded so a
    # docstring that conflates them under one rationale cannot satisfy
    # both guards: the plain REST path's payload simply lacks the keys...
    assert "the list payload carries neither key" in doc
    # ...while github_batch hard-codes None regardless of payload.
    assert "github_batch._map_graphql_pr" in doc
    assert "hard-coded" in doc

    # Azure DevOps mergeStatus mapping.
    assert "rejectedByPolicy" in doc
    assert "succeeded" in doc

    # get_pr remains authoritative.
    assert "`get_pr` remains the authoritative" in doc


# ---------- ticket #200 - event aliases / run filters / Ref / Release -------


def _make_run(**kwargs):
    from lib_python_projects.providers.base import PipelineRun

    defaults = dict(
        id="1",
        name="CI",
        branch="main",
        head_sha="a" * 40,
        event="push",
        status="completed",
        conclusion="success",
        url="https://example.invalid/runs/1",
        created_at="2026-08-21T10:00:00Z",
        updated_at="2026-08-21T10:00:00Z",
        run_attempt=1,
    )
    defaults.update(kwargs)
    return PipelineRun(**defaults)


class TestResolveEventAlias:
    """resolve_event_alias (ticket #200) - pure, provider-keyed lookup
    over the canonical D1 event vocabulary table."""

    _TABLE = [
        ("manual", "github", "workflow_dispatch"),
        ("manual", "gitlab", "web"),
        ("manual", "azuredevops", "manual"),
        ("workflow_dispatch", "github", "workflow_dispatch"),
        ("workflow_dispatch", "gitlab", "web"),
        ("workflow_dispatch", "azuredevops", "manual"),
        ("push", "github", "push"),
        ("push", "gitlab", "push"),
        ("push", "azuredevops", "individualCI"),
        ("schedule", "github", "schedule"),
        ("schedule", "gitlab", "schedule"),
        ("schedule", "azuredevops", "schedule"),
        ("pull_request", "github", "pull_request"),
        ("pull_request", "gitlab", "merge_request_event"),
        ("pull_request", "azuredevops", "pullRequest"),
        ("api", "github", "repository_dispatch"),
        ("api", "gitlab", "trigger"),
        ("api", "azuredevops", "userCreated"),
    ]

    @pytest.mark.parametrize("canonical,provider,native", _TABLE)
    def test_full_d1_table(self, canonical, provider, native):
        from lib_python_projects.providers.base import resolve_event_alias

        assert resolve_event_alias(canonical, provider) == native

    @pytest.mark.parametrize("canonical,provider,native", _TABLE)
    def test_case_insensitive_lookup(self, canonical, provider, native):
        from lib_python_projects.providers.base import resolve_event_alias

        assert resolve_event_alias(canonical.upper(), provider) == native

    @pytest.mark.parametrize("provider", ["github", "gitlab", "azuredevops"])
    def test_unmapped_string_passes_through_verbatim(self, provider):
        from lib_python_projects.providers.base import resolve_event_alias

        assert resolve_event_alias("workflow_run", provider) == "workflow_run"
        assert resolve_event_alias("individualCI", provider) == "individualCI"
        assert (
            resolve_event_alias("merge_request_event", provider)
            == "merge_request_event"
        )

    def test_event_none_short_circuits(self):
        from lib_python_projects.providers.base import resolve_event_alias

        assert resolve_event_alias(None, "github") is None


class TestApplyRunFilters:
    """apply_run_filters (ticket #200) - the shared client-side filter
    pass all three providers' listing methods delegate to."""

    def test_none_filters_are_a_no_op(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [_make_run(id="1"), _make_run(id="2")]
        assert apply_run_filters(runs, provider="github") == runs

    def test_empty_list_returns_empty_list(self):
        from lib_python_projects.providers.base import apply_run_filters

        assert apply_run_filters([], provider="github", workflow="ci") == []

    def test_all_filtered_out_returns_empty_list(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [_make_run(name="CI")]
        assert (
            apply_run_filters(runs, provider="github", workflow="release") == []
        )

    def test_workflow_matches_case_insensitively(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [_make_run(id="1", name="Release"), _make_run(id="2", name="CI")]
        result = apply_run_filters(runs, provider="github", workflow="release")
        assert [r.id for r in result] == ["1"]

    @pytest.mark.parametrize("workflow_arg", ["release.yml", "release.yaml", "release"])
    def test_workflow_filename_equivalence(self, workflow_arg):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [_make_run(id="1", name="release"), _make_run(id="2", name="CI")]
        result = apply_run_filters(runs, provider="github", workflow=workflow_arg)
        assert [r.id for r in result] == ["1"]

    def test_event_filter_resolves_alias_then_matches(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [
            _make_run(id="1", event="workflow_dispatch"),
            _make_run(id="2", event="push"),
        ]
        result = apply_run_filters(runs, provider="github", event="manual")
        assert [r.id for r in result] == ["1"]

    def test_event_filter_provider_native_string_still_works(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [
            _make_run(id="1", event="individualCI"),
            _make_run(id="2", event="push"),
        ]
        result = apply_run_filters(runs, provider="azuredevops", event="individualCI")
        assert [r.id for r in result] == ["1"]

    def test_since_filters_out_older_runs(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [
            _make_run(id="old", created_at="2026-08-21T09:00:00Z"),
            _make_run(id="new", created_at="2026-08-21T11:00:00Z"),
        ]
        result = apply_run_filters(runs, provider="github", since="2026-08-21T10:00:00Z")
        assert [r.id for r in result] == ["new"]

    def test_since_boundary_is_inclusive(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [_make_run(id="exact", created_at="2026-08-21T10:00:00Z")]
        result = apply_run_filters(runs, provider="github", since="2026-08-21T10:00:00Z")
        assert [r.id for r in result] == ["exact"]

    def test_unparseable_since_raises_provider_error(self):
        from lib_python_projects.providers.base import ProviderError, apply_run_filters

        with pytest.raises(ProviderError):
            apply_run_filters([_make_run()], provider="github", since="not-a-timestamp")

    def test_limit_applied_after_filtering(self):
        """limit=1 means "one matching run", not "one of the recent runs,
        maybe filtered away" - the filter must run before the limit slice."""
        from lib_python_projects.providers.base import apply_run_filters

        runs = [
            _make_run(id="1", name="CI"),
            _make_run(id="2", name="Release"),
            _make_run(id="3", name="Release"),
        ]
        result = apply_run_filters(runs, provider="github", workflow="release", limit=1)
        assert [r.id for r in result] == ["2"]

    def test_limit_none_is_unbounded(self):
        from lib_python_projects.providers.base import apply_run_filters

        runs = [_make_run(id="1"), _make_run(id="2")]
        assert len(apply_run_filters(runs, provider="github", limit=None)) == 2


class TestRunMatchesRef:
    """run_matches_ref (ticket #200 round-2 finding 3) — the direct unit
    test this helper previously had none of. The two existing indirect
    exercises (GitHub's and GitLab's `wait_for_run` tag tests) compare a
    bare tag string against itself on both sides, so they never actually
    exercised the `refs/heads/`/`refs/tags/` prefix-stripping logic —
    these cases do.
    """

    def test_bare_branch_matches_refs_heads_prefixed(self):
        from lib_python_projects.providers.base import run_matches_ref

        run = _make_run(branch="main")
        assert run_matches_ref(run, "refs/heads/main") is True

    def test_bare_tag_matches_refs_tags_prefixed(self):
        from lib_python_projects.providers.base import run_matches_ref

        run = _make_run(branch="v1.2.3")
        assert run_matches_ref(run, "refs/tags/v1.2.3") is True

    def test_refs_tags_prefixed_run_matches_bare_tag(self):
        """Azure DevOps's `sourceBranch` keeps a tag's `refs/tags/`
        prefix in place (only `refs/heads/` is stripped by
        `_map_build_run`) — the reverse direction of the previous case."""
        from lib_python_projects.providers.base import run_matches_ref

        run = _make_run(branch="refs/tags/v1.2.3")
        assert run_matches_ref(run, "v1.2.3") is True

    def test_genuine_mismatch_returns_false(self):
        from lib_python_projects.providers.base import run_matches_ref

        run = _make_run(branch="main")
        assert run_matches_ref(run, "refs/heads/develop") is False


class TestNowUtc:
    def test_returns_z_suffixed_string(self):
        from lib_python_projects.providers.base import now_utc

        assert now_utc().endswith("Z")

    def test_normalize_timestamp_of_now_utc_is_second_precision(self):
        from lib_python_projects.providers.base import normalize_timestamp, now_utc

        normalized = normalize_timestamp(now_utc())
        assert normalized.endswith("Z")
        assert "." not in normalized


class TestRefDataclass:
    def test_is_dataclass(self):
        from lib_python_projects.providers.base import Ref

        assert dataclasses.is_dataclass(Ref)

    def test_field_set(self):
        from lib_python_projects.providers.base import Ref

        field_names = {f.name for f in dataclasses.fields(Ref)}
        assert field_names == {"name", "kind", "sha", "url"}

    def test_round_trip(self):
        from lib_python_projects.providers.base import Ref

        ref = Ref(name="main", kind="branch", sha="a" * 40, url="https://example.invalid")
        assert ref.name == "main"
        assert ref.kind == "branch"
        assert ref.sha == "a" * 40
        assert ref.url == "https://example.invalid"


class TestReleaseDataclass:
    def test_is_dataclass(self):
        from lib_python_projects.providers.base import Release

        assert dataclasses.is_dataclass(Release)

    def test_field_set(self):
        from lib_python_projects.providers.base import Release

        field_names = {f.name for f in dataclasses.fields(Release)}
        assert field_names == {
            "tag", "name", "sha", "url", "draft", "prerelease",
            "created_at", "published_at", "body",
        }

    def test_round_trip(self):
        from lib_python_projects.providers.base import Release

        rel = Release(
            tag="v1.0.0", name="v1.0.0", sha="a" * 40,
            url="https://example.invalid/releases/v1.0.0",
            draft=False, prerelease=False,
            created_at="2026-08-21T10:00:00Z",
            published_at="2026-08-21T10:00:00Z",
            body="Release notes",
        )
        assert rel.tag == "v1.0.0"
        assert rel.body == "Release notes"


# ---------- epic #224 (217/219/220): _not_found_message helper --------------


class TestNotFoundMessage:
    """`_not_found_message` (epic #224 / ticket #219) is the shared,
    internal (not exported from `providers/__init__.py`) helper that
    composes the canonical 404 wording used across all three provider
    modules: `<kind> '<id>' not found` for a single id, and
    `<kind> '<id1>' or '<id2>' not found` for the GitLab issue-link
    two-id case. These are pure-function tests — they fail first only
    because the symbol does not exist yet on `providers/base.py`
    (`ImportError`), not because of a wrong composed string."""

    def test_single_id_reproduces_pr_shape(self) -> None:
        from lib_python_projects.providers.base import _not_found_message

        assert _not_found_message("PR", "acme#55") == "PR 'acme#55' not found"

    def test_two_ids_reproduces_gitlab_link_shape(self) -> None:
        from lib_python_projects.providers.base import _not_found_message

        assert (
            _not_found_message("ticket", "acme#5", "acme#9")
            == "ticket 'acme#5' or 'acme#9' not found"
        )

    def test_single_id_review_comment_shape(self) -> None:
        from lib_python_projects.providers.base import _not_found_message

        assert (
            _not_found_message("review comment", "123")
            == "review comment '123' not found"
        )

    def test_single_id_ticket_shape(self) -> None:
        from lib_python_projects.providers.base import _not_found_message

        assert _not_found_message("ticket", "acme#999") == "ticket 'acme#999' not found"


# ---------- ticket #237: Relation vs Ticket.id docstring contrast ----------


def test_relation_docstring_contrasts_ticket_id_formats() -> None:
    """Ticket #237: `Relation`'s docstring already states the `"#N"` /
    `"owner/repo#N"` `ticket_id` format, but never says this is
    *deliberately* a different format from `Ticket.id` (the bare
    provider-native id, e.g. issue.number / iid). A reader could
    reasonably — and wrongly — assume the two line up. This guard locks
    in the tightened docstring's explicit contrast, following the repo's
    established `inspect.getdoc` pattern (see
    `test_pullrequest_docstring_documents_per_path_mergeability`),
    including the `__dict__` own-entry check that bypasses
    `inspect.getdoc`'s MRO fallback to `object.__doc__` — without it,
    this test would still pass even if the docstring were deleted
    entirely.

    Expected RED: the current docstring never mentions `Ticket.id`.
    """
    from lib_python_projects.providers.base import Relation

    assert Relation.__dict__["__doc__"] is not None
    doc = inspect.getdoc(Relation)
    assert doc is not None

    # The docstring must name `Ticket.id` explicitly and say the two
    # formats are deliberately different, not an oversight.
    assert "Ticket.id" in doc
    assert "deliberately different" in doc

    # It must characterise `Ticket.id`'s format (bare provider-native id)
    # to make the contrast concrete, not just assert "different" in the
    # abstract.
    assert "bare provider-native id" in doc

    # `owner/repo#N` is read-path-only today (write targets are
    # same-project-only on every provider) -- the docstring should say so
    # explicitly, contrasting the read path against a write-path
    # restriction. A bare "read" substring alone could be satisfied by
    # unrelated prose, so require both sides of the contrast.
    assert "read" in doc.lower()
    assert "write" in doc.lower()


# ---------- ticket #231 (Finding 2): partial-flags contract -----------------


class TestTokenCapabilitiesPartialFlagsContract:
    """The `TokenCapabilities`/`TokenCapabilityProvider` docstrings
    currently mandate all-False flags whenever `reason` is set. That
    contract is too strict for Azure DevOps's new partial result
    (`work_items_unavailable`, step 1-3 of the plan): `issues_create`/
    `issues_modify` go False while the `pulls_*` flags — verified only by
    the org-scoped `connectionData` call — legitimately stay True. This
    guard pins the relaxed wording so a future edit can't silently
    reintroduce the "all flags False" mandate.

    `TokenCapabilities` is a `@dataclass` that already carries a
    hand-written docstring today, so `inspect.getdoc` reflects it
    directly; per the plan we rely on content assertions (key phrases)
    rather than an own-`__dict__`-entry existence check, since a
    dataclass with no docstring synthesizes a signature-shaped one
    instead of returning `None` — an own-entry check would only matter if
    the docstring were removed outright, which isn't in scope here.

    Expected RED: the docstrings still say "all boolean flags should be
    False" / "all flags False" and do not yet mention
    "work_items_unavailable" or "partial, surface-specific failure".
    """

    def test_token_capabilities_docstring_allows_partial_flags(self) -> None:
        from lib_python_projects.providers.base import TokenCapabilities

        doc = inspect.getdoc(TokenCapabilities)
        assert doc is not None

        assert "work_items_unavailable" in doc, (
            "TokenCapabilities docstring must document the new "
            f"work_items_unavailable reason: {doc!r}"
        )
        assert "partial, surface-specific failure" in doc, (
            "TokenCapabilities docstring must use the pinned phrase "
            f"'partial, surface-specific failure': {doc!r}"
        )
        assert "all boolean flags should be False" not in doc, (
            "TokenCapabilities docstring still asserts the old "
            f"all-False-on-any-reason mandate: {doc!r}"
        )

    def test_token_capability_provider_docstring_allows_partial_flags(
        self,
    ) -> None:
        from lib_python_projects.providers.base import TokenCapabilityProvider

        doc = inspect.getdoc(TokenCapabilityProvider)
        assert doc is not None

        assert "all flags False" not in doc, (
            "TokenCapabilityProvider docstring still mandates all-False "
            f"flags on any failure: {doc!r}"
        )
        assert "partial, surface-specific failure" in doc, (
            "TokenCapabilityProvider docstring must use the pinned "
            f"phrase 'partial, surface-specific failure': {doc!r}"
        )

    def test_partial_flags_construct_and_roundtrip(self) -> None:
        """Structural pinning guard, not expected to be RED — plain
        dataclass construction already supports this shape today."""
        from lib_python_projects.providers.base import TokenCapabilities

        caps = TokenCapabilities(
            issues_create=False,
            issues_modify=False,
            pulls_create=True,
            pulls_modify=True,
            pulls_merge=True,
            reason="work_items_unavailable",
        )
        assert caps.issues_create is False
        assert caps.issues_modify is False
        assert caps.pulls_create is True
        assert caps.pulls_modify is True
        assert caps.pulls_merge is True
        assert caps.reason == "work_items_unavailable"

    def test_no_pipeline_or_board_flag_on_token_capabilities(self) -> None:
        """Structural pinning guard for the probe docstring's claim (step
        6c) that there is 'no dedicated pipeline flag' — should already
        pass; if it ever fails, the probe docstring's claim goes stale
        too."""
        import dataclasses

        from lib_python_projects.providers.base import TokenCapabilities

        names = {f.name for f in dataclasses.fields(TokenCapabilities)}
        assert not any("pipeline" in n for n in names)


# ---------- ticket #240: parse_diff_hunk_ranges (R4) ---------------------------


class TestParseDiffHunkRanges:
    """`parse_diff_hunk_ranges(patch)` is the shared helper (imported by
    both github.py and gitlab.py) that turns `@@ -a,b +c,d @@` hunk
    headers into `DiffHunkRange` entries: a `LEFT` range `[a, a+b-1]`
    when `b > 0`, a `RIGHT` range `[c, c+d-1]` when `d > 0`, omitted
    counts mean `1`, and `None`/empty/malformed input returns `[]`."""

    def test_multi_hunk_yields_all_ranges_in_order(self) -> None:
        """Two hunks -> all four ranges, LEFT then RIGHT per hunk, in
        the order the hunks appear in the patch."""
        from lib_python_projects.providers.base import (
            DiffHunkRange, parse_diff_hunk_ranges,
        )

        patch = (
            "@@ -10,5 +12,7 @@ def foo():\n"
            " context\n"
            "-old\n"
            "+new\n"
            "@@ -30,3 +35,4 @@ def bar():\n"
            " context\n"
            "+added\n"
        )
        ranges = parse_diff_hunk_ranges(patch)
        assert ranges == [
            DiffHunkRange("LEFT", 10, 14),
            DiffHunkRange("RIGHT", 12, 18),
            DiffHunkRange("LEFT", 30, 32),
            DiffHunkRange("RIGHT", 35, 38),
        ]

    def test_none_and_empty_return_empty_list(self) -> None:
        from lib_python_projects.providers.base import parse_diff_hunk_ranges

        assert parse_diff_hunk_ranges(None) == []
        assert parse_diff_hunk_ranges("") == []

    def test_omitted_counts_default_to_single_line_ranges(self) -> None:
        """`@@ -5 +5 @@` (both counts omitted) means a single-line hunk
        on each side: `b`/`d` default to `1`."""
        from lib_python_projects.providers.base import (
            DiffHunkRange, parse_diff_hunk_ranges,
        )

        ranges = parse_diff_hunk_ranges("@@ -5 +5 @@\n-old\n+new\n")
        assert ranges == [
            DiffHunkRange("LEFT", 5, 5),
            DiffHunkRange("RIGHT", 5, 5),
        ]

    def test_pure_addition_omits_left_side(self) -> None:
        """`b == 0` (pure addition, `-0,0`) emits nothing for LEFT."""
        from lib_python_projects.providers.base import (
            DiffHunkRange, parse_diff_hunk_ranges,
        )

        ranges = parse_diff_hunk_ranges("@@ -0,0 +1,3 @@\n+a\n+b\n+c\n")
        assert ranges == [DiffHunkRange("RIGHT", 1, 3)]

    def test_pure_deletion_omits_right_side(self) -> None:
        """`d == 0` (pure deletion, `+0,0`) emits nothing for RIGHT."""
        from lib_python_projects.providers.base import (
            DiffHunkRange, parse_diff_hunk_ranges,
        )

        ranges = parse_diff_hunk_ranges("@@ -1,3 +0,0 @@\n-a\n-b\n-c\n")
        assert ranges == [DiffHunkRange("LEFT", 1, 3)]

    def test_trailing_section_context_after_header_is_ignored(self) -> None:
        """The free-text section-heading suffix after the second `@@`
        (e.g. `def foo():`) must not interfere with parsing."""
        from lib_python_projects.providers.base import (
            DiffHunkRange, parse_diff_hunk_ranges,
        )

        ranges = parse_diff_hunk_ranges(
            "@@ -1,3 +1,4 @@ def foo(arg1, arg2):\n context\n+new\n"
        )
        assert ranges == [
            DiffHunkRange("LEFT", 1, 3),
            DiffHunkRange("RIGHT", 1, 4),
        ]

    def test_garbage_text_with_no_hunk_headers_returns_empty_list(self) -> None:
        from lib_python_projects.providers.base import parse_diff_hunk_ranges

        assert parse_diff_hunk_ranges("this is not a diff at all\njust text\n") == []

    def test_mixed_valid_and_malformed_headers_skips_only_the_malformed_one(
        self,
    ) -> None:
        """A malformed hunk header must not nuke the whole patch to `[]`
        — only that header is skipped; a recognizable header elsewhere in
        the same patch is still parsed. `'malformed input returns []'`
        means 'no recognizable headers at all', not 'any single malformed
        header nukes everything' (round-2 plan note)."""
        from lib_python_projects.providers.base import (
            DiffHunkRange, parse_diff_hunk_ranges,
        )

        patch = (
            "@@ garbage not a real header @@\n"
            "+stray\n"
            "@@ -20,2 +22,3 @@ def baz():\n"
            " context\n"
            "+new\n"
        )
        ranges = parse_diff_hunk_ranges(patch)
        assert ranges == [
            DiffHunkRange("LEFT", 20, 21),
            DiffHunkRange("RIGHT", 22, 24),
        ]
