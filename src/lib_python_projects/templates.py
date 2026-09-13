"""Provider-free issue-template validation & rendering (ticket #259).

This module holds the shapes and (in a later phase) the logic that mirrors
GitHub's own rendering of a submitted issue form into an issue body: each
form field becomes a `### <label>` markdown section. Confirmed by fetching
GitHub's own docs (`https://docs.github.com/en/communities/using-templates-to-
encourage-useful-issues-and-pull-requests/syntax-for-issue-forms`, 2026-09-13):

    "When a contributor fills out an issue form, their responses for each
    input are converted to markdown and added to the body of an issue."

That fetch did **not** turn up a verbatim sentence for the `_No response_`
sentinel GitHub inserts for a blank optional field on that page as currently
published — phase=implement should re-verify wording for that specific
behaviour (well-known GitHub behaviour, just not confirmed verbatim from
this page) before relying on the literal string elsewhere.

Deliberately **no** provider imports here — `providers/base.py` imports
*from* this module, never the other way around.

`validate_ticket_body` mirrors GitHub's own rendering of a submitted issue
form: it parses `### <label>` sections out of a ticket body and checks each
template field against its rendered section. `render_skeleton` produces a
fillable `### <label>` skeleton for a template (or the template's raw body
for `kind="markdown"`). `_expected_sentence` generates the per-violation
human-readable remedy text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TemplateField:
    """A single field of an issue template.

    `type` is the provider-native form-element type for a `kind="form"`
    template (`"markdown"`, `"input"`, `"textarea"`, `"dropdown"`,
    `"checkboxes"`), or `"textarea"` uniformly for a synthesised
    `kind="workitem"` field (Azure DevOps).

    `options` is the list of selectable option labels for `"dropdown"`/
    `"checkboxes"` fields, `None` for every other type.

    `label` is the human-readable heading text a `### <label>` section is
    matched against. `field_id` is the provider-native field identifier
    (GitHub form `id`, Azure DevOps field reference name), `""` when the
    provider doesn't supply one.
    """

    label: str
    field_id: str
    type: str
    required: bool
    options: list[str] | None = None
    description: str | None = None
    placeholder: str | None = None


@dataclass
class IssueTemplate:
    """A single issue template as read from a project's web-UI template store.

    `kind` is `"form"` (GitHub YAML issue form), `"markdown"` (GitHub `.md`
    template or a GitLab issue template), or `"workitem"` (Azure DevOps work
    item template).

    `title_prefix` and `labels` default to `""`/`[]` (never `None`) when the
    provider/template doesn't set them, so callers don't need conditional
    guards. `raw_body` is populated for `kind="markdown"` (the template's
    literal body text) and empty for `kind="form"`/`kind="workitem"`, which
    carry their content in `fields` instead.
    """

    name: str
    filename: str
    title_prefix: str = ""
    labels: list[str] = field(default_factory=list)
    kind: str = "form"
    fields: list[TemplateField] = field(default_factory=list)
    raw_body: str = ""


@dataclass
class TemplateViolation:
    """A single way a submitted ticket body fails to satisfy a template.

    `reason` is one of `"missing"`, `"empty"`, `"placeholder"`,
    `"invalid-option"`, `"no-option-checked"`, `"heading-missing"`.

    `expected` is a generated, single-line, non-empty human-readable
    sentence naming the field's label and the remedy (see
    `_expected_sentence`, added in the implementation phase) — never pinned
    to exact wording by tests, only asserted non-empty/single-line.

    `enforced_by` names the specific mechanism that produced the violation,
    when that mechanism is narrower than the general per-field validation
    the `reason` already implies. It is `"heading-presence"` for every
    `kind="markdown"` heading-missing violation (only heading presence was
    enforced there -- no content/placeholder/option checks run against a
    markdown template) and defaults to `""` for every form-path violation.
    """

    field_label: str
    reason: str
    expected: str
    enforced_by: str = ""


# ---------- section parsing ---------------------------------------------

# GitHub's own issue-form rendering: each field becomes a `### <label>`
# section. Exact, case-sensitive, exactly three `#` (plan requirement) --
# a `## ` or `#### ` line is never mistaken for a form-field heading.
_FORM_SECTION_RE = re.compile(r"^### (.+)$", re.MULTILINE)

# A section's content runs up to (but not including) the next `##`/`###`
# heading line -- whichever comes first.
_NEXT_HEADING_RE = re.compile(r"^#{2,3} ", re.MULTILINE)

# Markdown-kind templates: headings at level 2 or 3, line-anchored so a
# heading word merely mentioned in prose never counts as "present".
_MARKDOWN_HEADING_RE = re.compile(r"^#{2,3}[ \t]+(.+?)\s*$", re.MULTILINE)

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# GitHub's sentinel for a blank optional form field (well-known GitHub
# behaviour -- see module docstring for the citation caveat).
_NO_RESPONSE = "_No response_"

# Captures the checked state and the label text of each checkbox line, so
# the checkboxes validator can check *which* option was checked, not merely
# that *some* line somewhere was checked (a made-up/unrelated checked line
# must never satisfy a required-checkbox field).
_CHECKBOX_LINE_RE = re.compile(r"^-\s*\[([ xX])\]\s*(.*)$", re.MULTILINE)


def _mask_fenced_code_blocks(text: str) -> str:
    """Blank out the interior of every fenced code-block span (paired
    ` ``` ` lines) in `text`, preserving text length and line structure, so
    a `### <label>`/`##`/`###` heading-shaped line inside a code fence is
    never mistaken for a real section/heading by the regexes below. Real
    ticket bodies contain code fences (confirmed via ticket #20's fetched
    body), so this masking happens once here, shared by both the
    form-section splitter and the markdown-heading extractor -- not
    duplicated in each.

    Fence delimiter lines themselves are left untouched (they never start
    with `#`, so they can't be mistaken for a heading); only the lines
    *between* a pair of fence lines are blanked, character-for-character,
    so byte offsets computed against the masked text stay valid against the
    original `text` too."""
    lines = text.split("\n")
    masked: list[str] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            masked.append(line)
            in_fence = not in_fence
        elif in_fence:
            masked.append(" " * len(line))
        else:
            masked.append(line)
    return "\n".join(masked)


def _parse_form_sections(body: str) -> dict[str, str]:
    """Return `{label: raw_content}` for every `### <label>` section in
    `body`. `raw_content` is the untouched text between the heading and
    the next `##`/`###` heading (or end of string) -- HTML-comment
    stripping and emptiness checks happen later, in the per-field
    validators, not here.

    Heading detection runs against a fenced-code-block-masked copy of
    `body` (see `_mask_fenced_code_blocks`) so a heading-shaped line inside
    a code fence is never treated as a real section or as the boundary of
    one; the masked copy has identical length/line structure to `body`, so
    the offsets found against it are used to slice the *real* content out
    of the original, unmasked `body`.

    A duplicate `### <label>` heading for the same label keeps its FIRST
    occurrence's content, never a later one -- GitHub's own form rendering
    never produces two headings for the same label, so a duplicate means
    the body was hand-edited/posted directly via the API. Letting a later
    duplicate overwrite the first could mask an empty, genuinely
    form-rendered section behind a same-labeled section a submitter added
    afterwards; honoring only the first (real) occurrence is the safe
    reading for a validator."""
    masked = _mask_fenced_code_blocks(body)
    sections: dict[str, str] = {}
    for m in _FORM_SECTION_RE.finditer(masked):
        label = m.group(1).strip()
        start = m.end()
        rest = masked[start:]
        next_heading = _NEXT_HEADING_RE.search(rest)
        end = start + next_heading.start() if next_heading else len(body)
        sections.setdefault(label, body[start:end])
    return sections


def _clean_content(raw_content: str | None) -> str | None:
    """Strip HTML comments (before the emptiness check, per plan) and
    surrounding whitespace. `None` (heading absent) passes through
    unchanged so callers can distinguish "absent" from "present but
    empty"."""
    if raw_content is None:
        return None
    return _HTML_COMMENT_RE.sub("", raw_content).strip()


# ---------- expected-sentence generation ---------------------------------


def _expected_sentence(label: str, reason: str, options: list[str] | None = None) -> str:
    """Generate a single-line, non-empty human-readable remedy sentence
    for a `TemplateViolation`, naming the field's label and a
    reason-specific fix."""
    if reason == "missing":
        return f"a `### {label}` section with content"
    if reason == "empty":
        return f"content under the `### {label}` section"
    if reason == "placeholder":
        return f"real content under {label}, not the placeholder text"
    if reason == "invalid-option":
        return f"one of: {', '.join(options or [])}"
    if reason == "no-option-checked":
        return f"at least one checked box under {label}"
    if reason == "heading-missing":
        return f"a `{label}` heading"
    return f"valid content for {label}"  # pragma: no cover - defensive fallback


# ---------- per-field-type validators -------------------------------------


def _validate_text_field(f: TemplateField, raw_content: str | None) -> TemplateViolation | None:
    """`input`/`textarea` validation: missing -> empty -> placeholder,
    in that order."""
    content = _clean_content(raw_content)
    if content is None:
        if f.required:
            return TemplateViolation(f.label, "missing", _expected_sentence(f.label, "missing"))
        return None
    if content == "" or content == _NO_RESPONSE:
        if f.required:
            return TemplateViolation(f.label, "empty", _expected_sentence(f.label, "empty"))
        return None
    if f.placeholder is not None and content == f.placeholder.strip():
        return TemplateViolation(f.label, "placeholder", _expected_sentence(f.label, "placeholder"))
    return None


def _validate_dropdown(f: TemplateField, raw_content: str | None) -> TemplateViolation | None:
    """`dropdown` validation: the absent/empty/sentinel rules run FIRST
    (plan requirement) -- only once content is genuinely present does the
    options-membership check run."""
    content = _clean_content(raw_content)
    if content is None:
        if f.required:
            return TemplateViolation(f.label, "missing", _expected_sentence(f.label, "missing"))
        return None
    if content == "" or content == _NO_RESPONSE:
        if f.required:
            return TemplateViolation(f.label, "empty", _expected_sentence(f.label, "empty"))
        return None
    options = f.options or []
    if content not in options:
        return TemplateViolation(
            f.label, "invalid-option", _expected_sentence(f.label, "invalid-option", options)
        )
    return None


def _validate_checkboxes(f: TemplateField, raw_content: str | None) -> TemplateViolation | None:
    """`checkboxes` validation: required + no *real option's* box checked ->
    `no-option-checked`. A checked line whose label isn't one of `f.options`
    (case-sensitive exact match, consistent with the rest of this module's
    exactness conventions) does not count -- only a genuinely satisfied
    option does. Optional fields with absent/empty content never violate."""
    content = _clean_content(raw_content)
    if not f.required:
        return None
    if content is None or content == "" or content == _NO_RESPONSE:
        return TemplateViolation(
            f.label, "no-option-checked", _expected_sentence(f.label, "no-option-checked")
        )
    options = set(f.options or [])
    checked_labels = {
        m.group(2).strip()
        for m in _CHECKBOX_LINE_RE.finditer(content)
        if m.group(1) in ("x", "X")
    }
    if not checked_labels & options:
        return TemplateViolation(
            f.label, "no-option-checked", _expected_sentence(f.label, "no-option-checked")
        )
    return None


def _validate_form(body: str, template: IssueTemplate) -> list[TemplateViolation]:
    sections = _parse_form_sections(body)
    violations: list[TemplateViolation] = []
    for f in template.fields:
        if f.type == "markdown":
            continue  # markdown fields are never checked (plan requirement)
        raw_content = sections.get(f.label)
        if f.type == "checkboxes":
            v = _validate_checkboxes(f, raw_content)
        elif f.type == "dropdown":
            v = _validate_dropdown(f, raw_content)
        else:
            v = _validate_text_field(f, raw_content)
        if v is not None:
            violations.append(v)
    return violations


def _validate_markdown(body: str, template: IssueTemplate) -> list[TemplateViolation]:
    """`kind="markdown"` validation: every `##`/`###` heading present in
    the template's `raw_body` must also appear as a line-anchored heading
    line in `body` -- a heading word merely mentioned in prose does not
    count. Both sides are matched against a fenced-code-block-masked copy
    (see `_mask_fenced_code_blocks`) so a heading-shaped line inside a code
    fence in either the template or the submitted body is never mistaken
    for a real heading."""
    template_headings = [
        m.group(1).strip()
        for m in _MARKDOWN_HEADING_RE.finditer(_mask_fenced_code_blocks(template.raw_body))
    ]
    body_headings = {
        m.group(1).strip() for m in _MARKDOWN_HEADING_RE.finditer(_mask_fenced_code_blocks(body))
    }
    violations: list[TemplateViolation] = []
    for label in template_headings:
        if label not in body_headings:
            violations.append(
                TemplateViolation(
                    label,
                    "heading-missing",
                    _expected_sentence(label, "heading-missing"),
                    enforced_by="heading-presence",
                )
            )
    return violations


def validate_ticket_body(body: str, template: IssueTemplate) -> list[TemplateViolation]:
    """Validate `body` against `template`, mirroring how GitHub renders a
    submitted issue form into an issue body.

    `kind="workitem"` always returns `[]` -- Azure DevOps work-item-
    template fields are all `required=False` by design (no validation
    teeth exist yet for that provider).
    """
    if template.kind == "workitem":
        return []
    if template.kind == "markdown":
        return _validate_markdown(body, template)
    return _validate_form(body, template)


# ---------- skeleton rendering --------------------------------------------


def _render_form_field(f: TemplateField) -> list[str]:
    lines = [f"### {f.label}"]
    if f.type == "markdown":
        if f.description:
            lines.append(f"<!-- {f.description.strip()} -->")
    elif f.type == "checkboxes":
        for opt in f.options or []:
            lines.append(f"- [ ] {opt}")
    elif f.type == "dropdown":
        comment_bits = []
        if f.description:
            comment_bits.append(f.description.strip())
        options = f.options or []
        if options:
            comment_bits.append(f"choose one of: {', '.join(options)}")
        if comment_bits:
            lines.append(f"<!-- {' '.join(comment_bits)} -->")
    else:
        comment_bits = []
        if f.description:
            comment_bits.append(f.description.strip())
        if f.placeholder:
            comment_bits.append(f"e.g. {f.placeholder.strip()}")
        if comment_bits:
            lines.append(f"<!-- {' '.join(comment_bits)} -->")
    lines.append("")
    return lines


def render_skeleton(template: IssueTemplate) -> str:
    """Render a fillable skeleton for `template`.

    `kind="markdown"` returns `raw_body` unchanged -- there is no field
    structure to skeletonize. `kind="workitem"` emits one plain
    `### <label>` heading per field, no comments (Azure DevOps work-item
    templates carry no description/placeholder metadata). `kind="form"`
    emits `### <label>` for every field, including `type="markdown"`
    ones, with description/placeholder/dropdown-options rendered as HTML
    comments (so they never accidentally satisfy the validator) and
    checkboxes rendered as real, literal `- [ ] option` lines.
    """
    if template.kind == "markdown":
        return template.raw_body
    if template.kind == "workitem":
        lines = [f"### {f.label}" for f in template.fields]
        return "\n".join(lines) + ("\n" if lines else "")
    lines = []
    for f in template.fields:
        lines.extend(_render_form_field(f))
    return "\n".join(lines).rstrip() + "\n"
