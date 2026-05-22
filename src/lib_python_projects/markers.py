"""AI-attribution markers added to tickets and comments by this server.

Provider implementations call these helpers; the agent never sets the
markers manually.

Body markers come in two flavours, mirroring the label vocabulary:
  - `#ai-generated\\n\\n` — resource was originally created by us
  - `#ai-modified\\n\\n`  — resource was originally human-authored and
                            this write is the first AI touch

A resource carries exactly one marker line. Transitioning from one
flavour to the other strips the previous marker line before prepending
the new one — no stacking.
"""
from __future__ import annotations

import re

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

# Matches a leading `#ai-<kebab>` marker line (with optional surrounding
# blank lines). Used to strip any prior marker before re-stamping.
_AI_MARKER_LINE_RE = re.compile(r"\A\s*#ai-[a-z][a-z0-9-]*\s*\n+")


def apply_body_marker(body: str | None, *, will_be_ai_generated: bool) -> str:
    """Return `body` with exactly one `#ai-*` marker line at the top.

    Strips any existing leading `#ai-<kebab>` marker line and prepends
    the marker that matches the resource's label state after the
    pending write:
      - `will_be_ai_generated=True`  → `#ai-generated\\n\\n`
      - `will_be_ai_generated=False` → `#ai-modified\\n\\n`

    `None` is treated as an empty body so callers can pipe through
    optional inputs without a `None`-check. Calling the helper twice
    in a row produces the same string as calling it once.

    Trailing newlines are stripped so the canonical form for empty
    body collapses to the bare marker line (`"#ai-generated"`) — this
    matches what GitLab persists and removes the GitHub/GitLab
    asymmetry called out by ticket #49 finding 9.
    """
    text = body or ""
    # Strip any single existing marker line (idempotent + handles
    # generated↔modified transitions without stacking).
    stripped = _AI_MARKER_LINE_RE.sub("", text)
    prefix = AI_GENERATED_PREFIX if will_be_ai_generated else AI_MODIFIED_PREFIX
    return (prefix + stripped).rstrip("\n")


def strip_leading_ai_marker(body: str | None) -> str:
    """Return `body` with any leading `#ai-*` marker line removed.

    Idempotent and tolerant of leading blank lines (the same prefix
    pattern `apply_body_marker` uses internally). Used by callers that
    need to splice content above the marker before re-stamping via
    `apply_body_marker`.
    """
    return _AI_MARKER_LINE_RE.sub("", body or "")


def has_ai_generated_marker(body: str | None) -> bool:
    """Return True if `body` already starts with the `#ai-generated` marker.

    Used by `update_comment` to decide whether a comment's existing body
    indicates AI authorship (which preserves the marker on edit) or
    human authorship (which switches the marker to `#ai-modified`).
    """
    if not body:
        return False
    return body.lstrip().startswith("#ai-generated")


def ensure_comment_prefix(body: str) -> str:
    """Prepend the AI-generated marker unless already present.

    Backward-compat wrapper — equivalent to
    `apply_body_marker(body, will_be_ai_generated=True)`. New code on
    the create-path should call `apply_body_marker` directly.
    """
    return apply_body_marker(body, will_be_ai_generated=True)


def ensure_body_prefix(body: str | None) -> str:
    """Prepend the AI-generated marker unless already present.

    Backward-compat wrapper — equivalent to
    `apply_body_marker(body, will_be_ai_generated=True)`. New code on
    the create-path should call `apply_body_marker` directly.
    """
    return apply_body_marker(body, will_be_ai_generated=True)
