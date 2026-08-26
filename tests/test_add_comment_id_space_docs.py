"""Tests for ticket #235 -- add_comment id-space hazard docstrings.

`add_comment` posts to a ticket/issue id-space that is provider-specific
and NOT the same id-space as a PR/MR/pull-request number. Each provider's
`add_comment` docstring must document what happens when a caller passes a
PR/MR id here by mistake, and must point at `add_pr_comment` for that case:

- GitHub aliases issue and PR numbers at this endpoint — a PR number
  silently succeeds and posts to the PR's conversation tab. This is
  GitHub-specific behavior, not portable to the other providers.
- GitLab's issue and MR id-spaces (iids) are disjoint — an MR iid passed
  here either targets an unrelated issue that happens to share the number,
  or 404s.
- Azure DevOps's work-item and PR id-spaces are disjoint — a PR id passed
  here 404s.

GitHub's "aliasing" framing must not leak into the GitLab/Azure docstrings,
which describe rejection (404 / wrong target), not aliasing.
"""
from __future__ import annotations

import inspect

from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider


def test_github_add_comment_docstring_documents_aliasing_hazard() -> None:
    """GitHub's add_comment currently has NO docstring at all — this must
    fail until one is added stating the id-aliasing hazard, that it's
    GitHub-specific/not portable, and pointing at add_pr_comment."""
    assert GitHubProvider.__dict__["add_comment"].__doc__ is not None
    doc = inspect.getdoc(GitHubProvider.add_comment)
    assert doc is not None
    doc_lower = doc.lower()
    assert "alias" in doc_lower
    assert "add_pr_comment" in doc
    assert "not portable" in doc_lower or "github-specific" in doc_lower


def test_gitlab_add_comment_docstring_documents_disjoint_id_space_hazard() -> None:
    """GitLab's add_comment has a one-line docstring today; it must be
    extended to document the disjoint issue/MR id-space hazard (404 or
    wrong-ticket) and point at add_pr_comment for merge requests."""
    assert GitLabProvider.__dict__["add_comment"].__doc__ is not None
    doc = inspect.getdoc(GitLabProvider.add_comment)
    assert doc is not None
    doc_lower = doc.lower()
    assert "disjoint" in doc_lower
    assert "404" in doc
    assert "add_pr_comment" in doc


def test_azuredevops_add_comment_docstring_documents_disjoint_id_space_hazard() -> None:
    """Azure DevOps's add_comment currently has NO docstring at all — this
    must fail until one is added documenting the disjoint work-item/PR
    id-space hazard (404) and pointing at add_pr_comment."""
    assert AzureDevOpsProvider.__dict__["add_comment"].__doc__ is not None
    doc = inspect.getdoc(AzureDevOpsProvider.add_comment)
    assert doc is not None
    doc_lower = doc.lower()
    assert "disjoint" in doc_lower
    assert "404" in doc
    assert "add_pr_comment" in doc


def test_github_aliasing_language_does_not_leak_into_gitlab_or_azure() -> None:
    """Negative guard: GitHub's 'aliases' framing describes GitHub-specific
    behavior and must not appear in GitLab/Azure DevOps docstrings, which
    describe rejection (404), not aliasing."""
    gitlab_doc = (inspect.getdoc(GitLabProvider.add_comment) or "").lower()
    azure_doc = (inspect.getdoc(AzureDevOpsProvider.add_comment) or "").lower()
    assert "alias" not in gitlab_doc
    assert "alias" not in azure_doc
