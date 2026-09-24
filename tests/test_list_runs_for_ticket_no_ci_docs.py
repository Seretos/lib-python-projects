"""Tests for ticket #288 -- `list_runs_for_ticket` NO_CI_SENTINEL docstrings.

`list_runs_for_ticket`'s `resolved_refs` return value can end with
`NO_CI_SENTINEL` (`"no-ci"`) — a marker, not a real ref/SHA/`!iid`/
`build/{id}` entry — appended only when no run matched AND the project has
no CI configured at all. None of the three providers' `list_runs_for_ticket`
docstrings explained this before #288 (ticket #209 wired the runtime
behaviour but never documented it on this particular method), so a caller
reading only the docstring has no way to know why `resolved_refs` sometimes
ends with a string that isn't a ref. This is a documentation-only fix —
runtime is unchanged (see `tests/test_github_pipelines.py`,
`tests/test_gitlab_pipelines.py`, `tests/test_azuredevops_misc.py` for the
runtime regression coverage, unmodified by #288).

Follows the pattern of `tests/test_add_comment_id_space_docs.py`.
"""
from __future__ import annotations

import inspect
import json
import re

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers.base import NO_CI_SENTINEL
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider

_PROVIDERS = [GitHubProvider, GitLabProvider, AzureDevOpsProvider]

# The exact paragraph the plan (#288, round 2) specifies for every
# provider's `list_runs_for_ticket` docstring (plan.md, "Item 1 (docs
# only)"). Pinned verbatim (case-sensitive) rather than approximated by
# keyword/phrase regexes: round-2 test-critique showed that phrase-adjacency
# regexes are structurally beatable by a docstring that arranges the same
# words/phrases into a negated, false statement (e.g. "NO_CI_SENTINEL is
# never the last element of resolved_refs; it is not a ref unless there is
# no CI configured"). Such a negated paraphrase does not match this exact
# sentence, so it cannot satisfy this test.
_PLANNED_PARAGRAPH = (
    '`resolved_refs` may end with `NO_CI_SENTINEL` (`"no-ci"`). It is '
    "appended as the last element only when no run matched and the "
    "project has no CI configured. It is a marker, not a ref: strip it "
    "before treating entries as SHAs / `!iid` / `build/{id}`. See "
    "`NO_CI_SENTINEL` in `base.py`."
)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _sentinel_paragraph(provider_cls: type) -> str:
    """Return the paragraph of `provider_cls`'s OWN (non-inherited)
    `list_runs_for_ticket` docstring that mentions `NO_CI_SENTINEL`.

    Reads `provider_cls.__dict__[...]` rather than
    `inspect.getdoc(provider_cls.list_runs_for_ticket)` so an inherited
    docstring can never satisfy this — each provider must document the
    sentinel on its own override. Raises `AssertionError` (via a plain
    assert) if no such paragraph exists, which is the expected RED
    failure before #288's docstring paragraphs are added.
    """
    raw_doc = provider_cls.__dict__["list_runs_for_ticket"].__doc__
    assert raw_doc is not None, (
        f"{provider_cls.__name__}.list_runs_for_ticket has no docstring "
        f"of its own"
    )
    doc = inspect.cleandoc(raw_doc)
    paragraphs = [p for p in doc.split("\n\n") if p.strip()]
    matches = [p for p in paragraphs if "NO_CI_SENTINEL" in p]
    assert matches, (
        f"{provider_cls.__name__}.list_runs_for_ticket's docstring has no "
        f"paragraph mentioning NO_CI_SENTINEL"
    )
    return matches[0]


@pytest.mark.parametrize("provider_cls", _PROVIDERS, ids=lambda c: c.__name__)
def test_list_runs_for_ticket_docstring_explains_no_ci_sentinel(
    provider_cls: type,
) -> None:
    """Each provider's own `list_runs_for_ticket` docstring must contain
    the plan's exact paragraph (verbatim, whitespace-normalized) tying
    `NO_CI_SENTINEL` to `resolved_refs`: last element, only when no CI is
    configured, and not a real ref.

    This is a text pin, not a keyword/phrase heuristic: round-2
    test-critique showed that phrase-adjacency regexes are beatable by a
    docstring paragraph that arranges the same required words/phrases into
    a negated, false statement (e.g. "NO_CI_SENTINEL is never the last
    element of resolved_refs; it is not a ref unless there is no CI
    configured" -- every phrase present, meaning reversed). That negated
    paraphrase is not equal, even after whitespace normalization, to the
    plan's exact sentence, so it cannot satisfy a verbatim substring check
    the way it could satisfy separate adjacency regexes."""
    paragraph = _sentinel_paragraph(provider_cls)
    collapsed = _collapse_whitespace(paragraph)
    assert _PLANNED_PARAGRAPH in collapsed, collapsed


def test_github_list_runs_for_ticket_no_ci_sentinel_is_actually_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ties the docstring's claim to the real runtime behaviour (test-critique
    finding tautology::F1): the sibling test above only pins the docstring
    text -- it never calls `list_runs_for_ticket`, so pasting the planned
    paragraph into a docstring while the runtime appends the sentinel
    somewhere other than last (or appends it even when CI exists) would
    still pass it. This test actually exercises
    `GitHubProvider.list_runs_for_ticket` in a no-run-matched /
    no-CI-configured scenario and confirms `NO_CI_SENTINEL` is genuinely
    the LAST element of the returned `resolved_refs`.

    Mirrors `test_ticket_is_pr_with_no_runs` in
    tests/test_github_pipelines.py (PR-as-ticket_id resolves a head_sha,
    no runs match), except `/actions/workflows` answers 404 here (not
    configured) instead of 200 -- that is what turns the sentinel on, per
    `GitHubProvider.list_runs_for_ticket`'s own `if not _has_workflows(...):
    return [], [*shas, NO_CI_SENTINEL]` (github.py).
    """

    def _json(payload: object, status_code: int = 200) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    head_sha = "abc123def456"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json({
                "number": 42,
                "title": "PR 42",
                "body": "",
                "state": "open",
                "user": {"login": "alice"},
                "assignees": [],
                "labels": [],
                "html_url": "https://github.com/acme/backend/pull/42",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
                "pull_request": {
                    "url": "https://api.github.com/repos/acme/backend/pulls/42",
                    "html_url": "https://github.com/acme/backend/pull/42",
                    "merged_at": None,
                },
            })
        if path == "/repos/acme/backend/pulls/42":
            return _json({
                "number": 42,
                "title": "PR 42",
                "state": "open",
                "head": {
                    "sha": head_sha,
                    "ref": "feature-branch",
                    "label": "acme:feature-branch",
                },
                "base": {"sha": "base000", "ref": "main"},
                "html_url": "https://github.com/acme/backend/pull/42",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
            })
        if path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        if path == "/repos/acme/backend/actions/workflows":
            # No CI configured -- this is what turns NO_CI_SENTINEL on.
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.url}")

    transport = httpx.MockTransport(handler)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    project = ProjectConfig(
        id="acme", provider="github", path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )
    runs, resolved_refs = GitHubProvider().list_runs_for_ticket(
        project, token="t", ticket_id="42",
    )
    assert runs == []
    assert resolved_refs == [head_sha, NO_CI_SENTINEL]
    assert resolved_refs[-1] == NO_CI_SENTINEL


def test_github_list_runs_for_ticket_docstring_drops_stale_head_shas_wording() -> None:
    """The pre-#288 GitHub docstring described `resolved_refs` as "the
    de-duped list of head_shas we queried" -- true for the SHA-only case
    but no longer complete once the sentinel is documented. #288 replaces
    it; this stale phrase must not survive, AND it must be replaced by the
    real explanation, not merely deleted or reworded away (test-critique
    finding tautology::F2: a bare absence check on the stale phrase would
    also pass a rewording like "the head SHAs queried" -- still stale and
    incomplete -- or an outright deletion with nothing put in its place).

    Whitespace is collapsed before both checks (same as the sibling test
    above) so that source-level line-wrapping cannot accidentally satisfy
    -- or accidentally fail to satisfy -- either assertion either way."""
    doc = inspect.getdoc(GitHubProvider.list_runs_for_ticket)
    assert doc is not None
    collapsed_doc = re.sub(r"\s+", " ", doc.lower())
    assert "head_shas we queried" not in collapsed_doc
    # The stale sentence must be replaced by the real explanation, not
    # just removed: reuse the same verbatim planned paragraph the sibling
    # test pins, so deleting/rewording the stale line without adding the
    # correct paragraph still fails this test.
    collapsed_doc_ws = _collapse_whitespace(doc)
    assert _PLANNED_PARAGRAPH in collapsed_doc_ws, collapsed_doc_ws
