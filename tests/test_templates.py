"""Driving tests for `lib_python_projects.templates` (ticket #259).

Covers `validate_ticket_body` (per-violation-reason correctness),
`render_skeleton` (round-trip against the three GitHub form fixtures plus a
markdown-sandwich case), and the "helper measurement" against the two real
`agent-web-tester` tickets (#20/#25) fetched verbatim into
`tests/fixtures/issue_templates/`.

`templates.py` currently defines only the shared dataclasses (phase=tests
compile-level skeleton) -- `validate_ticket_body`/`render_skeleton` don't
exist yet, so every test below is expected to fail with `AttributeError`
(module `lib_python_projects.templates` has no such attribute) until
phase=implement adds them. The module is imported and called via attribute
access (`templates.validate_ticket_body(...)`), not `from ... import
validate_ticket_body`, precisely so each test fails at its own call site
instead of the whole file failing to collect.
"""
from __future__ import annotations

import re
from pathlib import Path

from lib_python_projects import templates
from lib_python_projects.templates import IssueTemplate, TemplateField

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "issue_templates"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _field(
    label: str,
    *,
    field_id: str | None = None,
    type: str = "textarea",
    required: bool = False,
    options: list[str] | None = None,
    description: str | None = None,
    placeholder: str | None = None,
) -> TemplateField:
    return TemplateField(
        label=label,
        field_id=field_id if field_id is not None else label.lower().replace(" ", "-"),
        type=type,
        required=required,
        options=options,
        description=description,
        placeholder=placeholder,
    )


def _assert_expected_sentences(violations) -> None:
    """Every violation must carry a non-empty, single-line `expected`
    sentence (plan requirement) -- wording itself is never pinned."""
    for v in violations:
        assert isinstance(v.expected, str)
        assert v.expected.strip() != "", f"{v.field_label}/{v.reason} has empty 'expected'"
        assert "\n" not in v.expected, f"{v.field_label}/{v.reason} 'expected' spans multiple lines"


# ---------- hand-built IssueTemplate fixtures mirroring the three GitHub ----
# ---------- form YAML fixtures on disk (tests/fixtures/issue_templates/) ----
#
# These mirror bug.yml/task.yml/epic.yml field-for-field so the same shapes
# can be used both here (render_skeleton/validate_ticket_body) and in
# tests/test_github_issue_templates.py (list_issue_templates parsing of the
# identical YAML) without a production YAML-to-IssueTemplate parser existing
# yet (that parser is phase=implement's GitHub reader).

BUG_TEMPLATE = IssueTemplate(
    name="Bug Report",
    filename="bug.yml",
    title_prefix="[Bug]: ",
    labels=["bug"],
    kind="form",
    fields=[
        _field(
            "Problem", field_id="problem", type="textarea", required=False,
            description="Describe the problem context (optional background).",
            placeholder="What's the background?",
        ),
        _field(
            "Acceptance", field_id="acceptance", type="textarea", required=True,
            description="What must be true for this to be considered fixed?",
            placeholder="Describe the acceptance criteria",
        ),
        _field(
            "Prior attempts", field_id="prior-attempts", type="textarea", required=True,
            description="What has already been tried?",
            placeholder="Describe prior attempts",
        ),
    ],
)

TASK_TEMPLATE = IssueTemplate(
    name="Task",
    filename="task.yml",
    title_prefix="[Task]: ",
    labels=["task"],
    kind="form",
    fields=[
        # markdown field has an explicit `id: intro` in task.yml -> label
        # falls back to the id (plan: "fall back to their id or the literal
        # string 'Notes'").
        _field(
            "intro", field_id="intro", type="markdown", required=False,
            description="Thanks for filing a task! Please fill out the sections below.\n",
        ),
        _field(
            "Summary", field_id="summary", type="input", required=True,
            description="One-line summary of the task.",
            placeholder="ex. Update the deploy script",
        ),
        _field(
            "Details", field_id="details", type="textarea", required=False,
            description="Any extra context (optional).",
            placeholder="Add extra context here",
        ),
        _field(
            "Priority", field_id="priority", type="dropdown", required=True,
            options=["Low", "Medium", "High"],
        ),
        _field(
            "Confirmation", field_id="confirm", type="checkboxes", required=True,
            options=["I have read the contributing guidelines", "This is not a duplicate"],
        ),
    ],
)

EPIC_TEMPLATE = IssueTemplate(
    name="Epic",
    filename="epic.yml",
    title_prefix="[Epic]: ",
    labels=["epic"],
    kind="form",
    fields=[
        # markdown field has no `id` in epic.yml -> label falls back to the
        # literal string "Notes".
        _field(
            "Notes", field_id="", type="markdown", required=False,
            description="Please fill out the sections below to define this epic.\n",
        ),
        _field(
            "Goal", field_id="goal", type="textarea", required=True,
            description="What outcome does this epic deliver?",
            placeholder="Describe the goal",
        ),
        _field(
            "Scope", field_id="scope", type="textarea", required=False,
            description="What's explicitly out of scope (optional)?",
            placeholder="Describe the scope",
        ),
        _field(
            "Risks acknowledged", field_id="risks", type="checkboxes", required=False,
            options=["Reviewed by architecture"],
        ),
        _field(
            "Target Quarter", field_id="quarter", type="dropdown", required=False,
            options=["Q1", "Q2", "Q3", "Q4"],
        ),
    ],
)


# ---------- requirement 3: validate_ticket_body per-reason correctness -----


def test_missing_reason_when_required_field_has_no_heading_at_all():
    tmpl = IssueTemplate(name="t", filename="t.yml", fields=[_field("Acceptance", required=True)])
    violations = templates.validate_ticket_body("no headings here at all", tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "missing")]
    _assert_expected_sentences(violations)


def test_empty_reason_when_heading_present_but_content_blank():
    tmpl = IssueTemplate(name="t", filename="t.yml", fields=[_field("Acceptance", required=True)])
    body = "### Acceptance\n\n"
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "empty")]
    _assert_expected_sentences(violations)


def test_empty_reason_for_no_response_sentinel():
    tmpl = IssueTemplate(name="t", filename="t.yml", fields=[_field("Acceptance", required=True)])
    body = "### Acceptance\n_No response_\n"
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "empty")]
    _assert_expected_sentences(violations)


def test_empty_reason_when_only_html_comment_remains_after_stripping():
    """An unfilled skeleton section containing only an HTML comment must
    report `empty`, not pass as if it had real content (plan: HTML comments
    are stripped *before* the emptiness check)."""
    tmpl = IssueTemplate(name="t", filename="t.yml", fields=[_field("Acceptance", required=True)])
    body = "### Acceptance\n<!-- Describe the acceptance criteria -->\n"
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "empty")]


def test_placeholder_reason_when_content_equals_placeholder_text():
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field("Acceptance", required=True, placeholder="Describe the acceptance criteria")],
    )
    body = "### Acceptance\nDescribe the acceptance criteria\n"
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "placeholder")]
    _assert_expected_sentences(violations)


def test_invalid_option_reason_for_dropdown_not_in_options():
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field("Priority", type="dropdown", required=True, options=["Low", "Medium", "High"])],
    )
    body = "### Priority\nExtreme\n"
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Priority", "invalid-option")]
    _assert_expected_sentences(violations)


def test_optional_dropdown_left_at_no_response_produces_no_violation():
    """Regression for a bug caught in plan review: an *optional* dropdown
    left at GitHub's `_No response_` sentinel must NOT be reported as
    `invalid-option` -- the absent/empty/sentinel check must run before the
    membership check, and for an optional field that check ends the story."""
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field("Priority", type="dropdown", required=False, options=["Low", "Medium", "High"])],
    )
    body = "### Priority\n_No response_\n"
    violations = templates.validate_ticket_body(body, tmpl)
    assert violations == []


def test_no_option_checked_reason_for_required_checkboxes_all_unchecked():
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field(
            "Confirmation", type="checkboxes", required=True,
            options=["I have read the contributing guidelines", "This is not a duplicate"],
        )],
    )
    body = (
        "### Confirmation\n"
        "- [ ] I have read the contributing guidelines\n"
        "- [ ] This is not a duplicate\n"
    )
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Confirmation", "no-option-checked")]
    _assert_expected_sentences(violations)


def test_checkboxes_at_least_one_checked_satisfies_required_field():
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field(
            "Confirmation", type="checkboxes", required=True,
            options=["I have read the contributing guidelines", "This is not a duplicate"],
        )],
    )
    body = (
        "### Confirmation\n"
        "- [x] I have read the contributing guidelines\n"
        "- [ ] This is not a duplicate\n"
    )
    assert templates.validate_ticket_body(body, tmpl) == []


def test_no_option_checked_reason_when_only_unrelated_line_is_checked():
    """Regression (review round 1): a checked line whose label isn't one of
    the field's real options must not satisfy the required-checkbox rule --
    the old implementation matched *any* `- [x]` line anywhere in the
    section, regardless of its label."""
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field("Confirmation", type="checkboxes", required=True, options=["A", "B"])],
    )
    body = (
        "### Confirmation\n"
        "- [ ] A\n"
        "- [ ] B\n"
        "- [x] Not a real option\n"
    )
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Confirmation", "no-option-checked")]


def test_optional_checkboxes_all_unchecked_produces_no_violation():
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field("Risks acknowledged", type="checkboxes", required=False, options=["Reviewed by architecture"])],
    )
    body = "### Risks acknowledged\n- [ ] Reviewed by architecture\n"
    assert templates.validate_ticket_body(body, tmpl) == []


def test_duplicate_heading_keeps_first_occurrence_not_last():
    """Regression (review round 5): two `### <label>` headings for the same
    label in a submitted body (only reachable via hand-editing / direct API
    -- GitHub's own form rendering never produces a duplicate heading) must
    not let a later, filled-in duplicate mask an earlier, empty,
    form-rendered section. The first occurrence's content wins; a validator
    reading only the last one would wrongly conclude the field is
    satisfied."""
    tmpl = IssueTemplate(name="t", filename="t.yml", fields=[_field("Acceptance", required=True)])
    body = (
        "### Acceptance\n\n"
        "### Acceptance\n"
        "Real content here.\n"
    )
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "empty")]


def test_markdown_type_fields_are_never_checked():
    tmpl = IssueTemplate(
        name="t", filename="t.yml",
        fields=[_field("Notes", type="markdown", required=True)],
    )
    # No "### Notes" heading at all -- would be `missing` for any other type.
    assert templates.validate_ticket_body("nothing relevant here", tmpl) == []


def test_heading_shaped_line_inside_fenced_code_block_does_not_satisfy_form_section():
    """Regression (review round 1): a `### <label>`-shaped line inside a
    fenced code block must not be mistaken for a real section -- the
    genuinely missing, non-fenced "Acceptance" section must still be
    reported as `missing`. Real ticket bodies contain code fences
    (confirmed via ticket #20's fetched body)."""
    tmpl = IssueTemplate(name="t", filename="t.yml", fields=[_field("Acceptance", required=True)])
    body = (
        "### Problem\n"
        "Something broke.\n\n"
        "```\n"
        "### Acceptance\n"
        "this is inside a code fence, not a real heading\n"
        "```\n"
    )
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "missing")]


def test_heading_shaped_line_inside_fenced_code_block_does_not_satisfy_markdown_heading():
    """Same regression as above, for `kind="markdown"` heading-presence
    checking: a `## Usage`-shaped line fenced inside a code block in the
    submitted body must not count as the real "Usage" heading being
    present."""
    tmpl = IssueTemplate(
        name="Doc Template",
        filename="doc.md",
        kind="markdown",
        raw_body="## Setup\n\nDo X.\n\n## Usage\n\nDo Y.\n",
    )
    body = (
        "## Setup\n\nDid X.\n\n"
        "```\n"
        "## Usage\n"
        "```\n"
    )
    violations = templates.validate_ticket_body(body, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Usage", "heading-missing")]


def test_clean_body_produces_no_violations():
    body = (
        "### Problem\nSomething broke.\n\n"
        "### Acceptance\nIt no longer breaks.\n\n"
        "### Prior attempts\nWe tried a workaround.\n"
    )
    assert templates.validate_ticket_body(body, BUG_TEMPLATE) == []


def test_heading_missing_reason_for_markdown_kind_template():
    tmpl = IssueTemplate(
        name="Doc Template",
        filename="doc.md",
        kind="markdown",
        raw_body="## Setup\n\nDo X.\n\n## Usage\n\nDo Y.\n\n## Notes\n\nDo Z.\n",
    )
    # Two headings missing (not one) so the `all(...)` check below is
    # genuinely over a multi-element list -- an implementation that only
    # tags the first violation in the list would fail this.
    body_missing_usage = "## Setup\n\nDid X.\n"
    violations = templates.validate_ticket_body(body_missing_usage, tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [
        ("Usage", "heading-missing"),
        ("Notes", "heading-missing"),
    ]
    _assert_expected_sentences(violations)
    # Acceptance criterion 2 (last bullet): a markdown-kind heading-missing
    # violation must say "only heading presence was enforced" -- spelled out
    # as both `reason: heading-missing` (asserted above) AND
    # `enforced_by: heading-presence` on every such violation.
    assert all(v.enforced_by == "heading-presence" for v in violations)

    body_both_present = "## Setup\n\nDid X.\n\n## Usage\n\nDid Y.\n\n## Notes\n\nDid Z.\n"
    assert templates.validate_ticket_body(body_both_present, tmpl) == []


def test_form_path_violation_has_default_enforced_by():
    """`enforced_by` is specific to the markdown heading-presence path (see
    test_heading_missing_reason_for_markdown_kind_template above) -- a
    form-path violation (any of the five reasons `_validate_form` can
    produce; `heading-missing`/`enforced_by` never arise on this path) must
    carry the dataclass's default, unset value, never inherit
    heading-presence's remedy label."""
    tmpl = IssueTemplate(name="t", filename="t.yml", fields=[_field("Acceptance", required=True)])
    violations = templates.validate_ticket_body("no headings here at all", tmpl)
    assert [(v.field_label, v.reason) for v in violations] == [("Acceptance", "missing")]
    assert violations[0].enforced_by == ""


def test_workitem_kind_always_returns_no_violations():
    """Regression for a test-critic tautology: the one field here is
    `required=True` with no matching heading in either body -- under
    ordinary form-validation rules this would report a `missing` violation
    (see test_missing_reason_when_required_field_has_no_heading_at_all
    above, same shape). The only way both calls can legitimately return
    `[]` is a genuine `kind == "workitem"` short-circuit that skips
    field-by-field validation entirely; an implementation with no
    workitem-specific branch would fail this test."""
    tmpl = IssueTemplate(
        name="Bug (Azure)", filename="", kind="workitem",
        fields=[_field("Repro Steps", required=True, type="textarea")],
    )
    assert templates.validate_ticket_body("", tmpl) == []
    assert templates.validate_ticket_body("anything at all, no headings", tmpl) == []


# ---------- requirement 4: render_skeleton round-trip -----------------------


_SECTION_RE = re.compile(r"^### (.+)$", re.MULTILINE)


def _split_sections(text: str) -> list[tuple[str, int, int]]:
    """Return `(label, content_start, content_end)` for each `### <label>`
    section in `text` -- a test-local helper (not production logic) used
    only to locate where to inject concrete filled-in content for the
    round-trip test below."""
    matches = list(_SECTION_RE.finditer(text))
    result: list[tuple[str, int, int]] = []
    for m in matches:
        label = m.group(1).strip()
        content_start = min(m.end() + 1, len(text))
        rest = text[content_start:]
        next_heading = re.search(r"^#{2,3} ", rest, re.MULTILINE)
        content_end = content_start + next_heading.start() if next_heading else len(text)
        result.append((label, content_start, content_end))
    return result


def _fill_skeleton(skeleton: str, tmpl: IssueTemplate) -> str:
    """Replace every non-markdown field's section content with concrete,
    valid, non-placeholder filled-in text -- exercising the real
    `render_skeleton` output's heading structure rather than hand-writing a
    filled body from scratch."""
    sections = _split_sections(skeleton)
    out = skeleton
    for label, start, end in reversed(sections):
        field = next((f for f in tmpl.fields if f.label == label), None)
        if field is None or field.type == "markdown":
            continue
        if field.type == "dropdown":
            fill = (field.options or [""])[0]
        elif field.type == "checkboxes":
            fill = "\n".join(f"- [x] {opt}" for opt in (field.options or [])[:1])
        else:
            fill = f"Real, concrete, non-placeholder content for {label}."
        out = out[:start] + fill + "\n" + out[end:]
    return out


def _required_reason_for(field: TemplateField) -> str:
    """The violation reason an unfilled required field of this type reports
    (plan: checkboxes get `no-option-checked`, every other type gets
    `empty`)."""
    return "no-option-checked" if field.type == "checkboxes" else "empty"


def _assert_round_trips_clean(tmpl: IssueTemplate) -> None:
    skeleton = templates.render_skeleton(tmpl)

    # Unfilled: every required, non-markdown field must be flagged; no
    # optional field and no markdown field may be.
    unfilled_violations = templates.validate_ticket_body(skeleton, tmpl)
    by_label = {v.field_label: v.reason for v in unfilled_violations}
    for field in tmpl.fields:
        if field.type == "markdown":
            assert field.label not in by_label, f"{field.label} (markdown) must never be flagged"
            continue
        if field.required:
            assert by_label.get(field.label) == _required_reason_for(field), (
                f"expected {field.label!r} unfilled-required violation "
                f"{_required_reason_for(field)!r}, got {by_label.get(field.label)!r}"
            )
        else:
            assert field.label not in by_label, f"optional field {field.label} must not be flagged when absent"

    # Filled: a concretely filled-in skeleton must validate clean.
    filled = _fill_skeleton(skeleton, tmpl)
    filled_violations = templates.validate_ticket_body(filled, tmpl)
    assert filled_violations == [], f"expected clean, got {filled_violations!r}"


def test_render_skeleton_round_trip_bug_template():
    _assert_round_trips_clean(BUG_TEMPLATE)


def test_render_skeleton_round_trip_task_template():
    _assert_round_trips_clean(TASK_TEMPLATE)


def test_render_skeleton_round_trip_epic_template():
    _assert_round_trips_clean(EPIC_TEMPLATE)


def _strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def test_render_skeleton_output_shape_bug_template():
    """Regression for a test-critic tautology: the round-trip tests above
    only ever feed the skeleton back through validate_ticket_body, so a
    skeleton emitting nothing but bare '### <label>' headings for required
    fields (no comments, no optional-field section, no placeholder text
    anywhere) would still pass every one of them. These assertions inspect
    the actual `render_skeleton(BUG_TEMPLATE)` string directly."""
    skeleton = templates.render_skeleton(BUG_TEMPLATE)

    # a heading for every field, including the optional one.
    assert "### Problem" in skeleton
    assert "### Acceptance" in skeleton
    assert "### Prior attempts" in skeleton

    # each field's placeholder text appears somewhere in the skeleton (as
    # guidance for the person filling it in) but only inside an HTML
    # comment, never as literal, bare section content -- otherwise it would
    # itself trip the `placeholder` violation reason once filed verbatim.
    placeholders = [
        "What's the background?",
        "Describe the acceptance criteria",
        "Describe prior attempts",
    ]
    without_comments = _strip_html_comments(skeleton)
    for placeholder in placeholders:
        assert placeholder in skeleton, f"expected placeholder {placeholder!r} to appear in the skeleton"
        assert placeholder not in without_comments, (
            f"placeholder {placeholder!r} must only appear inside an HTML comment, "
            f"not as literal section content"
        )


def test_render_skeleton_output_shape_task_template_markdown_dropdown_checkboxes():
    """Companion to the bug-template shape test above, covering the field
    types bug.yml doesn't have: markdown, dropdown, checkboxes."""
    skeleton = templates.render_skeleton(TASK_TEMPLATE)

    # a heading for every field, including the markdown field (label falls
    # back to its id "intro" per the fixture comment above).
    assert "### intro" in skeleton
    assert "### Summary" in skeleton
    assert "### Details" in skeleton
    assert "### Priority" in skeleton
    assert "### Confirmation" in skeleton

    # the markdown field's own description text appears only inside an
    # HTML comment, never as bare text a reader (or the validator) would
    # treat as real section content.
    markdown_snippet = "Thanks for filing a task"
    without_comments = _strip_html_comments(skeleton)
    assert markdown_snippet in skeleton
    assert markdown_snippet not in without_comments, (
        "markdown field content must only appear inside an HTML comment"
    )

    # the dropdown field's options appear in the skeleton in some form
    # (e.g. inside a comment listing the choices).
    for option in ("Low", "Medium", "High"):
        assert option in skeleton

    # the checkboxes field's options are rendered as literal, one-per-line
    # unchecked checkbox items.
    assert "- [ ] I have read the contributing guidelines" in skeleton
    assert "- [ ] This is not a duplicate" in skeleton


SANDWICH_TEMPLATE = IssueTemplate(
    name="Sandwich",
    filename="sandwich.yml",
    kind="form",
    fields=[
        _field("Before", field_id="before", type="textarea", required=True, placeholder="fill before"),
        _field("Middle", field_id="middle", type="markdown", required=False, description="Just some notes."),
        _field("After", field_id="after", type="textarea", required=True, placeholder="fill after"),
    ],
)


def test_markdown_field_does_not_suppress_neighbouring_required_violations():
    skeleton = templates.render_skeleton(SANDWICH_TEMPLATE)
    violations = templates.validate_ticket_body(skeleton, SANDWICH_TEMPLATE)
    by_label = {v.field_label: v.reason for v in violations}
    assert by_label.get("Before") == "empty"
    assert by_label.get("After") == "empty"
    assert "Middle" not in by_label


# ---------- requirement 5: helper measurement against real tickets ---------
#
# Heading -> field-label rename map (+ level normalisation `##` -> `###`)
# needed to make ticket #25's real body validate clean against BUG_TEMPLATE.
# Derived by inspecting the actual fetched ticket text (2026-09-13): #25
# uses "##" (level-2) headings with wording that doesn't match bug.yml's
# field labels; GitHub's own "### <label>" form-rendering convention
# requires an exact, level-3 heading match.
_TICKET_25_RENAME_MAP = {
    "## Observed problem (user-facing behavior)": "### Problem",
    "## Testable acceptance criterion": "### Acceptance",
    "## Prior failed attempt — read before implementing": "### Prior attempts",
}


def test_helper_measurement_ticket_20_missing_acceptance_and_prior_attempts():
    """Ticket #20's real body (fetched verbatim; it has NO markdown headings
    at all, of any level) is missing exactly the "Acceptance" and "Prior
    attempts" sections against BUG_TEMPLATE -- this is the ticket's own
    stated claim, confirmed here by inspection rather than assumed. "Problem"
    is deliberately optional in BUG_TEMPLATE (see tests/test_templates.py
    fixture comment / change report): #20 has no heading matching it either,
    but since it's optional that absence produces no violation, which is
    what keeps the violation set to exactly these two entries."""
    body = _read_fixture("web_tester_20_body.md")
    violations = templates.validate_ticket_body(body, BUG_TEMPLATE)
    assert {(v.field_label, v.reason) for v in violations} == {
        ("Acceptance", "missing"),
        ("Prior attempts", "missing"),
    }


def test_helper_measurement_ticket_25_clean_after_heading_rename():
    """Ticket #25's real body, after renaming/normalising its '##' headings
    to bug.yml's exact '### <label>' fields, validates clean."""
    body = _read_fixture("web_tester_25_body.md")
    for old, new in _TICKET_25_RENAME_MAP.items():
        assert old in body, f"expected heading {old!r} not found in ticket #25 fixture -- re-inspect real text"
        body = body.replace(old, new)
    violations = templates.validate_ticket_body(body, BUG_TEMPLATE)
    assert violations == []
