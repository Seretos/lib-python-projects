"""Unit tests for `lib_python_projects.markers` (ticket #153).

Covers the new `MarkerSet` value object and the keyword-only `markers=`
param added to the five marker helpers, plus the role-based
`label_color`/`label_description` helpers. Pre-existing default-marker
behavior (no `markers=` passed) is covered by `test_provider_parity.py`
and the various provider test files — this file focuses on the new
custom-`MarkerSet` surface and backward compatibility.
"""
from __future__ import annotations

from lib_python_projects.markers import (
    AI_GENERATED_LABEL,
    AI_MODIFIED_LABEL,
    DEFAULT_MARKERS,
    MarkerSet,
    apply_body_marker,
    ensure_body_prefix,
    ensure_comment_prefix,
    has_ai_generated_marker,
    label_color,
    label_description,
    strip_leading_ai_marker,
)


# ---------- MarkerSet basics -------------------------------------------------


def test_default_markers_matches_module_constants():
    assert DEFAULT_MARKERS.generated == AI_GENERATED_LABEL
    assert DEFAULT_MARKERS.modified == AI_MODIFIED_LABEL


def test_custom_marker_set_produces_custom_prefixes():
    ms = MarkerSet("robot-made", "robot-touched")
    assert ms.generated_prefix == "#robot-made\n\n"
    assert ms.modified_prefix == "#robot-touched\n\n"


def test_marker_set_is_frozen():
    ms = MarkerSet("robot-made", "robot-touched")
    import dataclasses
    assert dataclasses.is_dataclass(ms)
    try:
        ms.generated = "other"  # type: ignore[misc]
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised


# ---------- apply_body_marker with a custom MarkerSet ------------------------


def test_apply_body_marker_custom_generated():
    ms = MarkerSet("robot-made", "robot-touched")
    out = apply_body_marker("Hello.", will_be_ai_generated=True, markers=ms)
    assert out == "#robot-made\n\nHello."


def test_apply_body_marker_custom_modified():
    ms = MarkerSet("robot-made", "robot-touched")
    out = apply_body_marker("Hello.", will_be_ai_generated=False, markers=ms)
    assert out == "#robot-touched\n\nHello."


def test_apply_body_marker_custom_empty_body_canonical_form():
    ms = MarkerSet("robot-made", "robot-touched")
    assert apply_body_marker(None, will_be_ai_generated=True, markers=ms) == "#robot-made"
    assert apply_body_marker("", will_be_ai_generated=True, markers=ms) == "#robot-made"


def test_apply_body_marker_idempotent_under_custom_marker_set():
    """Calling apply_body_marker twice in a row with the same custom
    MarkerSet must not stack markers."""
    ms = MarkerSet("robot-made", "robot-touched")
    once = apply_body_marker("Body text.", will_be_ai_generated=True, markers=ms)
    twice = apply_body_marker(once, will_be_ai_generated=True, markers=ms)
    assert once == twice
    assert twice.count("#robot-made") == 1


def test_apply_body_marker_transition_under_custom_marker_set_no_stacking():
    """Transitioning generated -> modified under the same custom MarkerSet
    strips the old marker rather than stacking a new one on top."""
    ms = MarkerSet("robot-made", "robot-touched")
    generated = apply_body_marker("Body.", will_be_ai_generated=True, markers=ms)
    modified = apply_body_marker(generated, will_be_ai_generated=False, markers=ms)
    assert modified == "#robot-touched\n\nBody."
    assert "robot-made" not in modified


def test_apply_body_marker_legacy_default_marker_stripped_and_restamped():
    """A body carrying the legacy default `#ai-generated` marker (e.g. from
    before a project configured custom auto_labels, or written by a
    differently-configured caller) is recognized and stripped — not
    stacked underneath — when re-stamped with a custom project's
    MarkerSet."""
    ms = MarkerSet("robot-made", "robot-touched")
    legacy_body = "#ai-generated\n\nOriginal text."
    restamped = apply_body_marker(legacy_body, will_be_ai_generated=True, markers=ms)
    assert restamped == "#robot-made\n\nOriginal text."
    assert "ai-generated" not in restamped
    assert restamped.count("#robot-made") == 1


# ---------- strip_leading_ai_marker with a custom MarkerSet ------------------


def test_strip_leading_ai_marker_custom_marker_set():
    ms = MarkerSet("robot-made", "robot-touched")
    assert strip_leading_ai_marker("#robot-made\n\nBody.", markers=ms) == "Body."


def test_strip_leading_ai_marker_strips_legacy_default_under_custom_set():
    ms = MarkerSet("robot-made", "robot-touched")
    assert strip_leading_ai_marker("#ai-modified\n\nBody.", markers=ms) == "Body."


def test_strip_leading_ai_marker_none_body_returns_empty():
    assert strip_leading_ai_marker(None) == ""


# ---------- has_ai_generated_marker: no cross-project false positives -------


def test_has_ai_generated_marker_true_for_own_custom_marker():
    ms = MarkerSet("robot-made", "robot-touched")
    assert has_ai_generated_marker("#robot-made\n\nBody.", markers=ms) is True


def test_has_ai_generated_marker_false_for_other_project_default_marker():
    """Deliberate no-false-positive behavior: a body carrying a *different*
    project's default `#ai-generated` marker must NOT match this project's
    custom `MarkerSet` — differently-configured projects never
    false-positive off one another."""
    ms = MarkerSet("robot-made", "robot-touched")
    assert has_ai_generated_marker("#ai-generated\n\nBody.", markers=ms) is False


def test_has_ai_generated_marker_false_for_modified_marker_of_same_set():
    ms = MarkerSet("robot-made", "robot-touched")
    assert has_ai_generated_marker("#robot-touched\n\nBody.", markers=ms) is False


def test_has_ai_generated_marker_default_behavior_unchanged():
    assert has_ai_generated_marker("#ai-generated\n\nBody.") is True
    assert has_ai_generated_marker("#ai-modified\n\nBody.") is False
    assert has_ai_generated_marker(None) is False
    assert has_ai_generated_marker("") is False


def test_has_ai_generated_marker_no_cross_match_on_literal_prefix():
    """A configured `generated` name that is a literal string-prefix of a
    sibling project's differently-named marker (e.g. default
    `ai-generated` vs. a custom `ai-generated-v2`) must require a boundary
    after the matched name — a bare `startswith` would false-positive."""
    default = MarkerSet("ai-generated", "ai-modified")
    cross_body = "#ai-generated-v2\n\nHello"
    assert has_ai_generated_marker(cross_body, markers=default) is False

    exact_body = "#ai-generated\n\nHello"
    assert has_ai_generated_marker(exact_body, markers=default) is True


# ---------- ensure_comment_prefix / ensure_body_prefix with markers= --------


def test_ensure_comment_prefix_custom_marker_set():
    ms = MarkerSet("robot-made", "robot-touched")
    assert ensure_comment_prefix("Hi.", markers=ms) == "#robot-made\n\nHi."


def test_ensure_body_prefix_custom_marker_set():
    ms = MarkerSet("robot-made", "robot-touched")
    assert ensure_body_prefix("Hi.", markers=ms) == "#robot-made\n\nHi."


def test_ensure_body_prefix_none_defaults_to_default_markers():
    assert ensure_body_prefix(None) == "#ai-generated"


# ---------- backward compatibility: default `markers=` param ----------------


def test_helpers_default_to_default_markers_when_unpassed():
    """Every helper's behavior with no `markers=` kwarg must be unchanged
    from before ticket #153."""
    assert apply_body_marker("x", will_be_ai_generated=True) == "#ai-generated\n\nx"
    assert apply_body_marker("x", will_be_ai_generated=False) == "#ai-modified\n\nx"
    assert strip_leading_ai_marker("#ai-generated\n\nx") == "x"
    assert has_ai_generated_marker("#ai-generated\n\nx") is True
    assert ensure_comment_prefix("x") == "#ai-generated\n\nx"
    assert ensure_body_prefix("x") == "#ai-generated\n\nx"


# ---------- role-based label_color / label_description ----------------------


def test_label_color_generated_role():
    assert label_color("generated") == "0e8a16"


def test_label_color_modified_role():
    assert label_color("modified") == "fbca04"


def test_label_color_unknown_role_falls_back_to_grey():
    assert label_color("bogus") == "ededed"


def test_label_description_generated_role_nonempty():
    assert label_description("generated") != ""


def test_label_description_modified_role_nonempty():
    assert label_description("modified") != ""


def test_label_description_unknown_role_is_empty():
    assert label_description("bogus") == ""
