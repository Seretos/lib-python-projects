"""AI-attribution markers added to tickets and comments by this server.

Provider implementations call these helpers; the agent never sets the
markers manually.

Body markers come in two flavours, mirroring the label vocabulary:
  - `#<ai_generated>\\n\\n` — resource was originally created by us
  - `#<ai_modified>\\n\\n`  — resource was originally human-authored and
                              this write is the first AI touch

By default these names are `ai-generated` / `ai-modified` (see
`AI_GENERATED_LABEL` / `AI_MODIFIED_LABEL` below), but each project in
`projects.yml` may configure its own pair via `ProjectConfig.auto_labels`
(see `models.py`). `MarkerSet` bundles a resolved name pair together with
the derived prefixes/regex; `DEFAULT_MARKERS` is the project-agnostic
default used when a caller doesn't pass `markers=`.

A resource carries exactly one marker line. Transitioning from one
flavour to the other strips the previous marker line before prepending
the new one — no stacking.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

AI_GENERATED_LABEL = "ai-generated"
AI_MODIFIED_LABEL = "ai-modified"
AI_NOT_PLANNED_LABEL = "ai-closed-not-planned"   # GitLab: stand-in for state_reason

AI_GENERATED_PREFIX = "#ai-generated\n\n"
AI_MODIFIED_PREFIX = "#ai-modified\n\n"

# Legacy aliases — kept so callers that still import the old names keep
# working. Prefer `AI_GENERATED_PREFIX` / `AI_MODIFIED_PREFIX` in new code.
AI_COMMENT_PREFIX = AI_GENERATED_PREFIX
AI_BODY_PREFIX = AI_GENERATED_PREFIX

LABEL_COLORS = {
    AI_GENERATED_LABEL: "0e8a16",     # green
    AI_MODIFIED_LABEL: "fbca04",      # yellow
    AI_NOT_PLANNED_LABEL: "cccccc",   # grey
}

LABEL_DESCRIPTIONS = {
    AI_GENERATED_LABEL: "Created by the project-issues AI agent",
    AI_MODIFIED_LABEL: "Modified by the project-issues AI agent",
    AI_NOT_PLANNED_LABEL: "Closed as 'not planned' by the project-issues AI agent",
}

# Role-keyed color/description tables — used to resolve the correct
# swatch/description for a *renamed* label (custom `auto_labels`), since
# `LABEL_COLORS`/`LABEL_DESCRIPTIONS` above are keyed by the literal
# default name and would miss a custom one. `AI_NOT_PLANNED_LABEL` has
# no role (it stays hardcoded, out of scope for #153) — it is a bare
# string constant, not resolved through any color/description table.
_ROLE_COLORS = {
    "generated": "0e8a16",   # green
    "modified": "fbca04",    # yellow
}

_ROLE_DESCRIPTIONS = {
    "generated": "Created by the project-issues AI agent",
    "modified": "Modified by the project-issues AI agent",
}


def label_color(role: str) -> str:
    """Return the swatch color for an auto-label role.

    `role` is `"generated"` or `"modified"`. Falls back to grey
    (`"ededed"`) for any other role, matching the historical fallback
    for unrecognized label names.
    """
    return _ROLE_COLORS.get(role, "ededed")


def label_description(role: str) -> str:
    """Return the label description for an auto-label role.

    `role` is `"generated"` or `"modified"`. Falls back to `""` for any
    other role.
    """
    return _ROLE_DESCRIPTIONS.get(role, "")


@dataclass(frozen=True)
class MarkerSet:
    """Resolved `#ai-generated` / `#ai-modified` marker vocabulary.

    Bundles a project's configured attribution names (`generated`,
    `modified`) together with the derived body-marker prefixes and the
    regex used to strip a leading marker line before re-stamping.
    Providers build one per project (see each provider's private
    `_marker_set(project)` helper) from `project.auto_labels`.
    """

    generated: str
    modified: str

    @property
    def generated_prefix(self) -> str:
        return f"#{self.generated}\n\n"

    @property
    def modified_prefix(self) -> str:
        return f"#{self.modified}\n\n"

    @property
    def strip_re(self) -> re.Pattern[str]:
        """Regex matching a leading marker line to strip before re-stamping.

        Matches this marker set's own `generated`/`modified` names *and*
        any generic `#ai-<kebab>` marker line — so a body still carrying
        a differently-configured (e.g. the legacy default) marker is
        recognized and stripped rather than stacked underneath the new
        one.
        """
        names = "|".join(re.escape(n) for n in (self.generated, self.modified))
        return re.compile(rf"\A\s*#(?:{names}|ai-[a-z][a-z0-9-]*)\s*(?:\n+|\Z)")


DEFAULT_MARKERS = MarkerSet(AI_GENERATED_LABEL, AI_MODIFIED_LABEL)

# Matches a leading `#ai-<kebab>` marker line (with optional surrounding
# blank lines). Kept for backward compatibility with any external caller
# that imported this constant directly; internal helpers now go through
# `MarkerSet.strip_re` instead.
_AI_MARKER_LINE_RE = DEFAULT_MARKERS.strip_re


def apply_body_marker(
    body: str | None,
    *,
    will_be_ai_generated: bool,
    markers: MarkerSet = DEFAULT_MARKERS,
) -> str:
    """Return `body` with exactly one `#<marker>` line at the top.

    Strips any existing leading marker line (this marker set's own
    names, or a generic `#ai-<kebab>` line from a differently-configured
    marker set) and prepends the marker that matches the resource's
    label state after the pending write:
      - `will_be_ai_generated=True`  → `markers.generated_prefix`
      - `will_be_ai_generated=False` → `markers.modified_prefix`

    `None` is treated as an empty body so callers can pipe through
    optional inputs without a `None`-check. Calling the helper twice
    in a row (with the same `markers`) produces the same string as
    calling it once.

    Trailing newlines are stripped so the canonical form for empty
    body collapses to the bare marker line (e.g. `"#ai-generated"`) —
    this matches what GitLab persists and removes the GitHub/GitLab
    asymmetry called out by ticket #49 finding 9.
    """
    text = body or ""
    # Strip any single existing marker line (idempotent + handles
    # generated↔modified transitions without stacking).
    stripped = markers.strip_re.sub("", text)
    prefix = markers.generated_prefix if will_be_ai_generated else markers.modified_prefix
    return (prefix + stripped).rstrip("\n")


def strip_leading_ai_marker(
    body: str | None, *, markers: MarkerSet = DEFAULT_MARKERS
) -> str:
    """Return `body` with any leading marker line removed.

    Idempotent and tolerant of leading blank lines (the same prefix
    pattern `apply_body_marker` uses internally). Used by callers that
    need to splice content above the marker before re-stamping via
    `apply_body_marker`.
    """
    return markers.strip_re.sub("", body or "")


def has_ai_generated_marker(
    body: str | None, *, markers: MarkerSet = DEFAULT_MARKERS
) -> bool:
    """Return True if `body` already starts with the generated marker.

    Used by `update_comment` to decide whether a comment's existing body
    indicates AI authorship (which preserves the marker on edit) or
    human authorship (which switches the marker to the modified one).

    Deliberately checks only *this* `markers` set's `generated` name —
    a body carrying a different project's (or the default's) generated
    marker does not count as a match here, so differently-configured
    projects never false-positive off one another.

    Requires a boundary (whitespace or end-of-string) immediately after
    the matched name, so a name that is a literal string-prefix of
    another project's configured name (e.g. `"ai-generated"` vs.
    `"ai-generated-v2"`) does not false-positive-match.
    """
    if not body:
        return False
    pattern = rf"\A\s*#{re.escape(markers.generated)}(?:\s|\Z)"
    return re.match(pattern, body) is not None


def ensure_comment_prefix(body: str, *, markers: MarkerSet = DEFAULT_MARKERS) -> str:
    """Prepend the AI-generated marker unless already present.

    Backward-compat wrapper — equivalent to
    `apply_body_marker(body, will_be_ai_generated=True, markers=markers)`.
    New code on the create-path should call `apply_body_marker` directly.
    """
    return apply_body_marker(body, will_be_ai_generated=True, markers=markers)


def ensure_body_prefix(
    body: str | None, *, markers: MarkerSet = DEFAULT_MARKERS
) -> str:
    """Prepend the AI-generated marker unless already present.

    Backward-compat wrapper — equivalent to
    `apply_body_marker(body, will_be_ai_generated=True, markers=markers)`.
    New code on the create-path should call `apply_body_marker` directly.
    """
    return apply_body_marker(body, will_be_ai_generated=True, markers=markers)
