"""Tests for GitHubProvider.list_statuses.

Covers:
- Regression (ticket #50 Issue B): cross-terminal transitions were missing.
- Structural self-consistency: every hint and transition value exists in
  `spec.values`.
- Open-to-both-terminals: `open` can transition to both closed variants.
- Hint values match the expected GitHub semantics.
- Round-trip: every value in `spec.values` is accepted by
  `_split_github_status` without raising.
"""
from __future__ import annotations

import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers.base import StatusSpec
from lib_python_projects.providers.github import GitHubProvider, _split_github_status


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="owner/repo",
    )


# ---------- regression: cross-terminal transitions (ticket #50 Issue B) ------


def test_closed_completed_can_transition_to_closed_not_planned() -> None:
    """closed:completed must list closed:not_planned as a valid next state."""
    spec = GitHubProvider().list_statuses(_project(), token=None)
    assert "closed:not_planned" in spec.transitions["closed:completed"]


def test_closed_not_planned_can_transition_to_closed_completed() -> None:
    """closed:not_planned must list closed:completed as a valid next state."""
    spec = GitHubProvider().list_statuses(_project(), token=None)
    assert "closed:completed" in spec.transitions["closed:not_planned"]


# ---------- structural self-consistency --------------------------------------


def test_list_statuses_is_static_and_self_consistent() -> None:
    spec = GitHubProvider().list_statuses(_project(), token=None)
    assert isinstance(spec, StatusSpec)
    # Every hint scalar / list element must be a known value.
    assert spec.hints["default_open"] in spec.values
    assert spec.hints["terminal_completed"] in spec.values
    assert spec.hints["terminal_declined"] in spec.values
    for v in spec.hints["terminal"]:
        assert v in spec.values
    # Every transitions src and every dst must be in spec.values.
    for src, dsts in spec.transitions.items():
        assert src in spec.values, f"transitions key {src!r} not in values"
        for dst in dsts:
            assert dst in spec.values, f"transitions dst {dst!r} not in values"


# ---------- open-to-both-terminals -------------------------------------------


def test_open_can_transition_to_both_closed_variants() -> None:
    spec = GitHubProvider().list_statuses(_project(), token=None)
    assert "closed:completed" in spec.transitions["open"]
    assert "closed:not_planned" in spec.transitions["open"]


# ---------- hint values ------------------------------------------------------


def test_hints_match_github_semantics() -> None:
    spec = GitHubProvider().list_statuses(_project(), token=None)
    assert spec.hints["terminal_completed"] == "closed:completed"
    assert spec.hints["terminal_declined"] == "closed:not_planned"
    assert spec.hints["default_open"] == "open"
    terminal = spec.hints["terminal"]
    assert "closed:completed" in terminal
    assert "closed:not_planned" in terminal


# ---------- round-trip: _split_github_status accepts all spec values ---------


@pytest.mark.parametrize("value", ["open", "closed:completed", "closed:not_planned"])
def test_split_github_status_accepts_all_spec_values(value: str) -> None:
    """Every value in spec.values must be accepted by _split_github_status."""
    # Should not raise.
    state, reason = _split_github_status(value)
    assert state is not None
