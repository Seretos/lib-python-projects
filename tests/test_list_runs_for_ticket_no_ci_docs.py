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
import re

import pytest

from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
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


def test_github_list_runs_for_ticket_docstring_drops_stale_head_shas_wording() -> None:
    """The pre-#288 GitHub docstring described `resolved_refs` as "the
    de-duped list of head_shas we queried" -- true for the SHA-only case
    but no longer complete once the sentinel is documented. #288 replaces
    it; this stale phrase must not survive.

    Whitespace is collapsed before the check (same as the sibling test
    above) so that source-level line-wrapping cannot accidentally satisfy
    -- or accidentally fail to satisfy -- this assertion either way."""
    doc = inspect.getdoc(GitHubProvider.list_runs_for_ticket)
    assert doc is not None
    collapsed_doc = re.sub(r"\s+", " ", doc.lower())
    assert "head_shas we queried" not in collapsed_doc
