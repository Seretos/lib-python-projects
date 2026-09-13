"""Driving tests for `GitHubProvider.list_issue_templates` (ticket #259,
`IssueTemplateProvider` mixin).

Mocking follows `tests/test_gitlab_issues.py`'s `httpx.MockTransport`
convention, adapted to GitHub's `_client(token)` signature (see
`test_provider_parity.py`'s `_install_github_mock`).

Neither `providers/base.py`'s `IssueTemplateProvider` mixin nor
`GitHubProvider.list_issue_templates` exist yet (phase=implement writes
them) -- every test here is expected to fail with `AttributeError`:
`'GitHubProvider' object has no attribute 'list_issue_templates'`.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers.github import GitHubError, GitHubProvider


def _project(path: str = "acme/widgets") -> ProjectConfig:
    return ProjectConfig(
        id="acme-widgets",
        provider="github",
        path=path,
        token_env="GITHUB_TOKEN_ACME",
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_mod.API_BASE, headers=headers, transport=transport,
        )

    monkeypatch.setattr(github_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _dir_entry(name: str, path: str) -> dict:
    return {"name": name, "path": path, "type": "file"}


def _content_response(text: str) -> httpx.Response:
    return _json({"content": _b64(text), "encoding": "base64"})


FIXTURES = Path(__file__).parent / "fixtures" / "issue_templates"
BUG_YML_TEXT = (FIXTURES / "bug.yml").read_text(encoding="utf-8")
TASK_YML_TEXT = (FIXTURES / "task.yml").read_text(encoding="utf-8")
EPIC_YML_TEXT = (FIXTURES / "epic.yml").read_text(encoding="utf-8")

NOTE_MD_TEXT = (
    "---\n"
    "name: Question\n"
    'about: "Ask a general question"\n'
    'title: "[Question]: "\n'
    "labels: [\"question\"]\n"
    "---\n"
    "Please describe your question in detail.\n"
)

CONFIG_YML_TEXT = (
    "blank_issues_enabled: false\n"
    "contact_links:\n"
    "  - name: Community Support\n"
    "    url: https://example.com/discussions\n"
    "    about: Please ask questions here.\n"
)

_DIR_PATH = ".github/ISSUE_TEMPLATE"


def _standard_dir_listing() -> list[dict]:
    return [
        _dir_entry("bug.yml", f"{_DIR_PATH}/bug.yml"),
        _dir_entry("task.yml", f"{_DIR_PATH}/task.yml"),
        _dir_entry("epic.yml", f"{_DIR_PATH}/epic.yml"),
        _dir_entry("note.md", f"{_DIR_PATH}/note.md"),
        _dir_entry("config.yml", f"{_DIR_PATH}/config.yml"),
    ]


def _standard_handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path.endswith(f"/contents/{_DIR_PATH}"):
        return _json(_standard_dir_listing())
    if path.endswith(f"{_DIR_PATH}/bug.yml"):
        return _content_response(BUG_YML_TEXT)
    if path.endswith(f"{_DIR_PATH}/task.yml"):
        return _content_response(TASK_YML_TEXT)
    if path.endswith(f"{_DIR_PATH}/epic.yml"):
        return _content_response(EPIC_YML_TEXT)
    if path.endswith(f"{_DIR_PATH}/note.md"):
        return _content_response(NOTE_MD_TEXT)
    if path.endswith(f"{_DIR_PATH}/config.yml"):
        raise AssertionError("config.yml content must never be fetched -- excluded by filename")
    raise AssertionError(f"unexpected request: {req.method} {path}")


# ---------- requirement 1: parses form + markdown templates, excludes config.yml ----


def test_list_issue_templates_returns_correctly_shaped_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(monkeypatch, _standard_handler)
    result = GitHubProvider().list_issue_templates(_project(), "tok")

    by_filename = {t.filename: t for t in result}
    assert set(by_filename) == {"bug.yml", "task.yml", "epic.yml", "note.md"}, (
        "config.yml must be excluded from results"
    )

    bug = by_filename["bug.yml"]
    assert bug.name == "Bug Report"
    assert bug.title_prefix == "[Bug]: "
    assert bug.labels == ["bug"]
    assert bug.kind == "form"
    field_by_label = {f.label: f for f in bug.fields}
    assert set(field_by_label) == {"Problem", "Acceptance", "Prior attempts"}
    assert field_by_label["Problem"].required is False
    assert field_by_label["Acceptance"].required is True
    assert field_by_label["Prior attempts"].required is True
    assert all(f.type == "textarea" for f in bug.fields)

    task = by_filename["task.yml"]
    assert task.name == "Task"
    assert task.title_prefix == "[Task]: "
    assert task.labels == ["task"]
    task_fields = {f.label: f for f in task.fields}
    # markdown field has an explicit id -> label falls back to the id.
    assert "intro" in task_fields
    assert task_fields["intro"].type == "markdown"
    assert task_fields["Summary"].type == "input"
    assert task_fields["Summary"].required is True
    assert task_fields["Priority"].type == "dropdown"
    assert task_fields["Priority"].options == ["Low", "Medium", "High"]
    assert task_fields["Priority"].required is True
    # checkboxes: field-level `required` derives from *any* option's own
    # `required: true` -- task.yml's "Confirmation" has exactly one such
    # option among two.
    confirmation = task_fields["Confirmation"]
    assert confirmation.type == "checkboxes"
    assert confirmation.required is True
    assert confirmation.options == [
        "I have read the contributing guidelines",
        "This is not a duplicate",
    ]

    epic = by_filename["epic.yml"]
    epic_fields = {f.label: f for f in epic.fields}
    # epic.yml's markdown field has NO id -> label falls back to "Notes".
    assert "Notes" in epic_fields
    assert epic_fields["Notes"].type == "markdown"
    # epic.yml's "Risks acknowledged" checkboxes field has no option marked
    # required -> the field itself must not be required.
    assert epic_fields["Risks acknowledged"].required is False

    note = by_filename["note.md"]
    assert note.kind == "markdown"
    assert note.name == "Question"
    assert note.title_prefix == "[Question]: "
    assert note.labels == ["question"]
    assert note.fields == []
    assert "Please describe your question in detail." in note.raw_body
    # Regression for a test-critic tautology: a substring-only check would
    # also pass an implementation that assigns the *entire* decoded file --
    # front matter included -- to raw_body. Pin down that the front matter
    # was actually stripped, not merely that the real content is present
    # somewhere inside a larger string.
    assert not note.raw_body.startswith("---")
    assert "name: Question" not in note.raw_body


def test_list_issue_templates_title_prefix_and_labels_default_to_empty_not_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A form YAML with no top-level `title`/`labels` keys must surface
    `""`/`[]`, never `None`."""
    minimal_form = (
        "name: Minimal\n"
        "body:\n"
        "  - type: textarea\n"
        "    id: notes\n"
        "    attributes:\n"
        "      label: Notes\n"
        "    validations:\n"
        "      required: false\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith(f"/contents/{_DIR_PATH}"):
            return _json([_dir_entry("minimal.yml", f"{_DIR_PATH}/minimal.yml")])
        if path.endswith(f"{_DIR_PATH}/minimal.yml"):
            return _content_response(minimal_form)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_issue_templates(_project(), "tok")
    assert len(result) == 1
    tmpl = result[0]
    assert tmpl.title_prefix == ""
    assert tmpl.labels == []
    assert tmpl.filename == "minimal.yml"


def test_md_template_scalar_labels_parsed_as_comma_separated_not_char_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (review round 1): GitHub's own default-generated `.md`
    legacy templates write `labels` as a bare scalar string (`labels: bug`),
    not a YAML sequence. `list("bug")` would silently produce
    `['b', 'u', 'g']` instead of `["bug"]`."""
    scalar_labels_md = (
        "---\n"
        "name: Scalar Labels\n"
        'title: "[Scalar]: "\n'
        "labels: bug\n"
        "---\n"
        "Body text.\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith(f"/contents/{_DIR_PATH}"):
            return _json([_dir_entry("scalar.md", f"{_DIR_PATH}/scalar.md")])
        if path.endswith(f"{_DIR_PATH}/scalar.md"):
            return _content_response(scalar_labels_md)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_issue_templates(_project(), "tok")
    assert len(result) == 1
    assert result[0].labels == ["bug"]


def test_md_template_scalar_comma_separated_labels_split_and_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scalar `labels: bug, needs-triage` (GitHub's own comma-separated
    front-matter convention) must split into distinct, stripped labels."""
    scalar_labels_md = (
        "---\n"
        "name: Scalar Labels Multi\n"
        "labels: bug, needs-triage\n"
        "---\n"
        "Body text.\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith(f"/contents/{_DIR_PATH}"):
            return _json([_dir_entry("scalar2.md", f"{_DIR_PATH}/scalar2.md")])
        if path.endswith(f"{_DIR_PATH}/scalar2.md"):
            return _content_response(scalar_labels_md)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_issue_templates(_project(), "tok")
    assert len(result) == 1
    assert result[0].labels == ["bug", "needs-triage"]


def test_list_issue_templates_quotes_path_with_special_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (review round 4): a template `path` containing a `#` --
    a character GitHub filenames permit -- must be percent-quoted before
    being interpolated into the contents-API URL. Unquoted, `#` is parsed
    client-side as the start of a URL *fragment* and everything after it is
    stripped before the request is ever sent (RFC 3986 fragments are never
    put on the wire), so the server would see a request for
    `.../ISSUE_TEMPLATE/bug` instead of `.../ISSUE_TEMPLATE/bug#1.yml` --
    landing on the wrong resource (here, an unexpected/missing one) rather
    than the real template.
    """
    weird_name = "bug#1.yml"
    weird_path = f"{_DIR_PATH}/{weird_name}"
    expected_encoded_suffix = f"{_DIR_PATH}/bug%231.yml"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        raw_path = req.url.raw_path.decode("ascii")
        if path.endswith(f"/contents/{_DIR_PATH}"):
            return _json([_dir_entry(weird_name, weird_path)])
        if raw_path.endswith(expected_encoded_suffix):
            return _content_response(BUG_YML_TEXT)
        raise AssertionError(
            f"unexpected request (path not correctly quoted): {req.method} {path!r} "
            f"raw_path={raw_path!r}"
        )

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_issue_templates(_project(), "tok")

    assert len(result) == 1
    tmpl = result[0]
    assert tmpl.filename == weird_name
    assert tmpl.name == "Bug Report"
    assert tmpl.kind == "form"


# ---------- requirement 2: error contract -----------------------------------


def test_md_template_with_crlf_line_endings_parses_front_matter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (review round 5): a `.md` issue template committed with
    CRLF (`\\r\\n`) line endings -- perfectly valid, just not LF -- must
    still have its front matter parsed. The old `_FRONT_MATTER_RE` assumed a
    bare `\\n` immediately follows the opening `---` and surrounds the
    closing `---`, so a CRLF file never matched and the whole template was
    silently dropped (treated the same as a genuinely unparseable file)."""
    crlf_md = (
        "---\r\n"
        "name: CRLF Template\r\n"
        'title: "[CRLF]: "\r\n'
        'labels: ["bug"]\r\n'
        "---\r\n"
        "Body text for the CRLF template.\r\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith(f"/contents/{_DIR_PATH}"):
            return _json([_dir_entry("crlf.md", f"{_DIR_PATH}/crlf.md")])
        if path.endswith(f"{_DIR_PATH}/crlf.md"):
            return _content_response(crlf_md)
        raise AssertionError(f"unexpected request: {req.method} {path}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_issue_templates(_project(), "tok")

    assert len(result) == 1, "CRLF front matter must parse, not be silently dropped"
    tmpl = result[0]
    assert tmpl.kind == "markdown"
    assert tmpl.name == "CRLF Template"
    assert tmpl.title_prefix == "[CRLF]: "
    assert tmpl.labels == ["bug"]
    assert "Body text for the CRLF template." in tmpl.raw_body
    # front matter must be excluded from raw_body, not merely present
    # somewhere inside a larger string (same tautology guard as the LF case
    # above).
    assert not tmpl.raw_body.strip().startswith("---")
    assert "name: CRLF Template" not in tmpl.raw_body


def test_list_issue_templates_404_dir_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    assert GitHubProvider().list_issue_templates(_project(), "tok") == []


def test_list_issue_templates_empty_dir_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    assert GitHubProvider().list_issue_templates(_project(), "tok") == []


@pytest.mark.parametrize("status", [401, 403, 500])
def test_list_issue_templates_auth_and_server_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "boom"}, status_code=status)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        GitHubProvider().list_issue_templates(_project(), "tok")
    assert exc.value.status == status
