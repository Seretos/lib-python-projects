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
    """Each provider's own `list_runs_for_ticket` docstring must have a
    paragraph tying `NO_CI_SENTINEL` to `resolved_refs` and explaining,
    in one place: it's the last element, it only appears when no CI is
    configured, and it is not a real ref. Every check below is scoped to
    that ONE paragraph (not the whole docstring), so a stray mention of
    the constant elsewhere in the docstring cannot make this pass."""
    paragraph = _sentinel_paragraph(provider_cls)
    collapsed = re.sub(r"\s+", " ", paragraph.lower())
    assert "resolved_refs" in collapsed
    assert '"no-ci"' in collapsed
    assert "last element" in collapsed
    assert "no ci configured" in collapsed
    assert "not a ref" in collapsed


def test_github_list_runs_for_ticket_docstring_drops_stale_head_shas_wording() -> None:
    """The pre-#288 GitHub docstring described `resolved_refs` as "the
    de-duped list of head_shas we queried" -- true for the SHA-only case
    but no longer complete once the sentinel is documented. #288 replaces
    it; this stale phrase must not survive."""
    doc = inspect.getdoc(GitHubProvider.list_runs_for_ticket)
    assert doc is not None
    assert "head_shas we queried" not in doc
