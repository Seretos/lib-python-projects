"""Scaffold tests for the Azure DevOps provider.

Covers things that exist independently of any single endpoint:
- the provider class is registered in `_PROVIDERS`
- `_safe` translates `AzureDevOpsError` to the standard envelope
- `_check` handles the documented error payload shapes
- `_client` sets Basic auth correctly
- scope helpers honour the three-segment path
- mappers translate canonical ADO payloads into the common dataclasses
- the minimal MD↔HTML converter preserves the `#ai-generated` marker
- `list_statuses` returns a self-consistent StatusSpec
"""
from __future__ import annotations

import base64
import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsError,
    AzureDevOpsProvider,
    SUPPORTED_RELATION_KINDS,
    _basic_auth_header,
    _build_pr_url,
    _build_work_item_url,
    _cache_clear_all,
    _check,
    _client,
    _html_to_markdown,
    _map_pr,
    _map_work_item,
    _map_work_item_comment,
    _markdown_to_html,
    _org_scope,
    _project_scope,
)
from lib_python_projects.providers.base import Comment, FieldSpec, StatusSpec, Ticket


def _project(
    path: str = "seredos/azure-tests/azure-tests",
    base_url: str | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path=path,
        base_url=base_url,
        token_env="AZURE_TOKEN",
    )


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "test-agent"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """ADO provider has module-level caches — wipe between tests so
    state from one test doesn't leak into another."""
    _cache_clear_all()


# ---------- registry wiring --------------------------------------------------


def test_provider_is_registered() -> None:
    # TODO(ports-adapters): re-enable nach API-Stabilisierung
    # The tool-layer `_PROVIDERS` registry lives in agent-project-issues,
    # not in this lib. Provider registration is now a plugin concern.
    import pytest as _pytest
    _pytest.skip("tool-layer registry test — belongs in agent-project-issues")


def test_safe_translates_azuredevops_error() -> None:
    # TODO(ports-adapters): re-enable nach API-Stabilisierung
    # `_safe` wrapper lives in the plugin's tools/_providers.py.
    import pytest as _pytest
    _pytest.skip("tool-layer error-translation test — belongs in agent-project-issues")


def test_supported_relation_kinds_matches_writable_subset() -> None:
    from lib_python_projects.providers.base import WRITABLE_RELATION_KINDS

    # ADO supports every writable kind defined in the base interface.
    for kind in WRITABLE_RELATION_KINDS:
        assert kind in SUPPORTED_RELATION_KINDS


# ---------- _client + auth ---------------------------------------------------


def test_basic_auth_header_format() -> None:
    header = _basic_auth_header("PAT123")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
    # Empty username, PAT as password (canonical ADO PAT auth scheme).
    assert decoded == ":PAT123"


def test_client_sets_authorization_with_pat() -> None:
    client = _client(_project(), token="abc")
    try:
        assert client.headers["Authorization"].startswith("Basic ")
    finally:
        client.close()


def test_client_omits_auth_when_token_missing() -> None:
    client = _client(_project(), token=None)
    try:
        assert "Authorization" not in client.headers
    finally:
        client.close()


def test_client_base_url_default() -> None:
    client = _client(_project(), token=None)
    try:
        assert str(client.base_url).rstrip("/") == "https://dev.azure.com"
    finally:
        client.close()


def test_client_base_url_overridden_for_self_hosted() -> None:
    client = _client(_project(base_url="https://devops.example.com/"), token=None)
    try:
        # Trailing slash gets stripped.
        assert str(client.base_url).rstrip("/") == "https://devops.example.com"
    finally:
        client.close()


def test_client_base_url_kwarg_overrides_project() -> None:
    """The ``base_url`` keyword argument takes priority over project.base_url."""
    project = _project(base_url="https://devops.example.com")
    client = _client(project, token=None, base_url="https://app.vssps.visualstudio.com")
    try:
        assert str(client.base_url).rstrip("/") == "https://app.vssps.visualstudio.com"
    finally:
        client.close()


def test_client_base_url_kwarg_none_falls_back_to_project() -> None:
    """When ``base_url`` kwarg is None, project.base_url is used."""
    project = _project(base_url="https://devops.example.com")
    client = _client(project, token=None, base_url=None)
    try:
        assert str(client.base_url).rstrip("/") == "https://devops.example.com"
    finally:
        client.close()


# ---------- scope helpers ----------------------------------------------------


def test_project_scope_uses_org_and_project() -> None:
    assert _project_scope(_project()) == "/seredos/azure-tests"


def test_project_scope_quotes_special_chars() -> None:
    p = _project(path="My Org/My Project/repo")
    assert _project_scope(p) == "/My%20Org/My%20Project"


def test_org_scope_returns_just_org() -> None:
    assert _org_scope(_project()) == "/seredos"


# ---------- _check error translation -----------------------------------------


def _resp(payload, status: int = 400) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("GET", "https://dev.azure.com/x"),
    )


def test_check_success_is_no_op() -> None:
    r = httpx.Response(
        status_code=200, request=httpx.Request("GET", "https://dev.azure.com/x")
    )
    _check(r)


def test_check_simple_message_payload() -> None:
    with pytest.raises(AzureDevOpsError) as exc:
        _check(_resp({"message": "TF401019: Work item not found"}, status=404))
    assert exc.value.status == 404
    assert "TF401019" in str(exc.value)


def test_check_inner_exception_is_appended() -> None:
    with pytest.raises(AzureDevOpsError) as exc:
        _check(
            _resp(
                {
                    "message": "VS403335: validation failed",
                    "innerException": {"message": "field 'title' is required"},
                },
                status=400,
            )
        )
    assert "VS403335" in str(exc.value)
    assert "title" in str(exc.value)


def test_check_non_json_falls_back_to_reason() -> None:
    r = httpx.Response(
        status_code=500,
        content=b"<html>oops</html>",
        request=httpx.Request("GET", "https://dev.azure.com/x"),
    )
    with pytest.raises(AzureDevOpsError) as exc:
        _check(r)
    assert exc.value.status == 500


# ---------- MD ↔ HTML round-trip --------------------------------------------


def test_markdown_to_html_handles_basic_inlines() -> None:
    html = _markdown_to_html("Hello **world** and *kitty*.")
    assert "<strong>world</strong>" in html
    assert "<em>kitty</em>" in html


def test_markdown_to_html_preserves_marker_line() -> None:
    # The leading `#ai-generated` line must NOT be rendered as <h1> — it
    # has to round-trip back to itself so markers.has_ai_generated_marker
    # keeps working.
    html = _markdown_to_html("#ai-generated\n\nBody text")
    assert "<h1>" not in html
    assert "#ai-generated" in html


def test_html_to_markdown_inverse_of_basic_html() -> None:
    html = "<p>Hello <strong>world</strong></p>"
    md = _html_to_markdown(html)
    assert md == "Hello **world**"


def test_marker_survives_round_trip() -> None:
    from lib_python_projects.markers import (
        ensure_body_prefix,
        has_ai_generated_marker,
    )

    md_in = ensure_body_prefix("**Bold** body with `code` and a [link](https://x.io).")
    html = _markdown_to_html(md_in)
    md_back = _html_to_markdown(html)
    assert has_ai_generated_marker(md_back)
    assert "**Bold**" in md_back
    assert "[link](https://x.io)" in md_back
    assert "`code`" in md_back


def test_html_to_markdown_strips_unknown_tags_keeping_text() -> None:
    html = "<div><span class='x'>kept</span></div>"
    assert _html_to_markdown(html) == "kept"


def test_markdown_to_html_handles_fenced_code() -> None:
    html = _markdown_to_html("```python\nprint('hi')\n```")
    assert "<pre><code" in html
    assert "language-python" in html
    assert "print(&#x27;hi&#x27;)" in html or "print('hi')" in html


def test_markdown_to_html_handles_unordered_list() -> None:
    html = _markdown_to_html("- one\n- two\n- three")
    assert "<ul>" in html and "</ul>" in html
    assert html.count("<li>") == 3


def test_markdown_to_html_handles_headings_other_than_marker() -> None:
    # A heading mid-body still gets the <h2> treatment — only the very
    # first line, if it matches the AI marker pattern, is special-cased.
    html = _markdown_to_html("Body\n\n## Section")
    assert "<h2>Section</h2>" in html


def test_unordered_list_round_trip_keeps_bullets_adjacent() -> None:
    """The HTMLParser feeds the literal `\\n` between `</li>` and `<li>`
    into `handle_data`; without the in-list whitespace guard that
    compounds into a blank line between every bullet. Round-trip must
    preserve adjacency."""
    md_in = "- one\n- two\n- three"
    html = _markdown_to_html(md_in)
    md_back = _html_to_markdown(html)
    # No blank line between bullets.
    assert "- one\n- two\n- three" in md_back


def test_ordered_list_round_trip_keeps_items_adjacent() -> None:
    md_in = "1. one\n2. two\n3. three"
    html = _markdown_to_html(md_in)
    md_back = _html_to_markdown(html)
    assert "1. one\n2. two\n3. three" in md_back


def test_fenced_code_language_tag_survives_round_trip() -> None:
    """Defect 2a: the language tag on a fenced code block must survive the
    markdown→HTML→markdown round-trip unchanged.

    Without the fix the `pre` handler emits ``\\n```\\n`` before seeing the
    inner ``<code class="language-X">`` tag, losing the language.
    """
    md_in = "```python\nprint('hi')\n```"
    md_back = _html_to_markdown(_markdown_to_html(md_in))
    assert md_back == md_in, repr(md_back)


def test_fenced_code_no_language_round_trips_cleanly() -> None:
    """Defect 2a (regression): a fence without a language tag must still
    round-trip without gaining a trailing space or extra newline."""
    md_in = "```\ncode\n```"
    md_back = _html_to_markdown(_markdown_to_html(md_in))
    assert md_back == md_in, repr(md_back)


def test_multiline_paragraph_round_trip_no_extra_blank_line() -> None:
    """Defect 2b: two lines of plain text in the same paragraph must
    round-trip to exactly ``Line1\\nLine2``, not ``Line1\\n\\nLine2``.

    The extra blank line appeared because the join separator was
    ``"<br>\\n"``; the trailing ``\\n`` was delivered as a separate data
    event by HTMLParser, producing a spurious extra newline on readback.
    """
    md_in = "Line1\nLine2"
    md_back = _html_to_markdown(_markdown_to_html(md_in))
    assert md_back == md_in, repr(md_back)


def test_html_to_markdown_strips_trailing_per_line_whitespace() -> None:
    """ADO's HTML editor can leak trailing spaces (notably after the AI
    marker line). `_html_to_markdown` strips them per line so the agent-
    visible body stays clean."""
    # Trailing space inside the marker paragraph + inside a body line.
    html = "<p>#ai-generated </p><p>Hello world  </p>"
    md = _html_to_markdown(html)
    # No line ends with a space.
    for line in md.splitlines():
        assert line == line.rstrip(), repr(line)
    assert "#ai-generated" in md


# ---------- mappers ----------------------------------------------------------


def _work_item_payload(work_item_id: int = 1, **overrides) -> dict:
    base = {
        "id": work_item_id,
        "fields": {
            "System.Title": f"Item {work_item_id}",
            "System.Description": "<p>Body</p>",
            "System.State": "To Do",
            "System.WorkItemType": "Issue",
            "System.Tags": "bug; p1",
            "System.AssignedTo": {
                "displayName": "Alice",
                "uniqueName": "alice@example.com",
            },
            "System.CreatedBy": {"displayName": "Bob"},
            "System.CreatedDate": "2026-05-18T10:00:00Z",
            "System.ChangedDate": "2026-05-18T11:00:00Z",
        },
    }
    if "fields" in overrides:
        base["fields"].update(overrides.pop("fields"))
    base.update(overrides)
    return base


def test_map_work_item_basic() -> None:
    raw = _work_item_payload(5)
    p = _project()
    t = _map_work_item(raw, p)
    assert isinstance(t, Ticket)
    assert t.id == "5"
    assert t.title == "Item 5"
    assert t.status == "To Do"
    assert t.author == "Bob"
    assert t.assignees == ["Alice"]
    assert t.labels == ["bug", "p1"]
    assert t.body == "Body"
    assert t.url == "https://dev.azure.com/seredos/azure-tests/_workitems/edit/5"


def test_map_work_item_missing_optional_fields() -> None:
    raw = {"id": 9, "fields": {"System.State": "Done"}}
    t = _map_work_item(raw, _project())
    assert t.title == ""
    assert t.body == ""
    assert t.assignees == []
    assert t.labels == []
    assert t.status == "Done"


def test_map_work_item_comment_basic() -> None:
    raw = {
        "id": 42,
        "createdBy": {"displayName": "Alice"},
        "text": "<p>Looks good!</p>",
        "createdDate": "2026-05-18T12:00:00Z",
    }
    c = _map_work_item_comment(raw, _project(), "5")
    assert isinstance(c, Comment)
    assert c.id == "42"
    assert c.body == "Looks good!"
    assert c.author == "Alice"
    assert "/_workitems/edit/5" in c.url


def _pr_payload(pr_id: int = 7, **overrides) -> dict:
    base = {
        "pullRequestId": pr_id,
        "title": f"PR {pr_id}",
        "description": "<p>impl</p>",
        "status": "active",
        "isDraft": False,
        "createdBy": {"displayName": "Alice"},
        "reviewers": [],
        "labels": [],
        "sourceRefName": "refs/heads/feat/x",
        "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "lastMergeTargetCommit": {"commitId": "def456"},
        "creationDate": "2026-05-18T10:00:00Z",
        "repository": {"name": "azure-tests"},
    }
    base.update(overrides)
    return base


def test_map_pr_open() -> None:
    pr = _map_pr(_pr_payload(7), _project())
    assert pr.id == "7"
    assert pr.number == 7
    assert pr.title == "PR 7"
    assert pr.status == "open"
    assert pr.draft is False
    assert pr.author == "Alice"
    assert pr.head["ref"] == "feat/x"
    assert pr.head["sha"] == "abc123"
    assert pr.base["ref"] == "main"


def test_map_pr_merged() -> None:
    pr = _map_pr(
        _pr_payload(7, status="completed", mergeStatus="succeeded", lastMergeCommit={"commitId": "merged123"}),
        _project(),
    )
    assert pr.status == "merged"
    assert pr.merged is True
    assert pr.merge_commit_sha == "merged123"


def test_map_pr_abandoned() -> None:
    pr = _map_pr(_pr_payload(7, status="abandoned"), _project())
    assert pr.status == "closed"
    assert pr.merged is False


def test_map_pr_reviewer_vote_classification() -> None:
    pr = _map_pr(
        _pr_payload(
            7,
            reviewers=[
                {"displayName": "Reviewed", "vote": 10},
                {"displayName": "Pending", "vote": 0},
                {"displayName": "Rejected", "vote": -10},
            ],
        ),
        _project(),
    )
    assert pr.reviewers == ["Reviewed", "Rejected"]
    assert pr.requested_reviewers == ["Pending"]


# ---------- list_statuses ----------------------------------------------------


def test_list_statuses_basic_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Basic process template has 3 states (To Do / Doing / Done).
    We expect `default_open` = "To Do" and `terminal_completed` = "Done".
    """

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({
                "value": [
                    {"name": "Issue", "referenceName": "Microsoft.VSTS.WorkItemTypes.Issue"},
                    {"name": "Task"},
                ]
            })
        if path.endswith("/_apis/wit/workitemtypes/Issue/states"):
            return _json({
                "value": [
                    {"name": "To Do", "category": "Proposed"},
                    {"name": "Doing", "category": "InProgress"},
                    {"name": "Done", "category": "Completed"},
                ]
            })
        raise AssertionError(f"unexpected path {path}")

    _install_mock(monkeypatch, handler)
    spec = AzureDevOpsProvider().list_statuses(_project(), token="t")
    assert isinstance(spec, StatusSpec)
    assert spec.values == ["To Do", "Doing", "Done"]
    assert spec.hints["default_open"] == "To Do"
    assert spec.hints["terminal_completed"] == "Done"
    # Basic has no Removed state — surface that honestly as None rather
    # than collapsing onto terminal_completed, which would mislead
    # agents into thinking they had two terminal states to pick from.
    assert spec.hints["terminal_declined"] is None
    # transitions: all states can reach every other state (we don't
    # restrict because ADO doesn't expose the legal-transitions graph).
    assert set(spec.transitions["To Do"]) == {"Doing", "Done"}


def test_list_statuses_agile_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Agile-template Issue type has a Removed state — `terminal_declined`
    must point at it rather than collapsing onto `terminal_completed`."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Bug"}]})
        if path.endswith("/_apis/wit/workitemtypes/Bug/states"):
            return _json({
                "value": [
                    {"name": "New", "category": "Proposed"},
                    {"name": "Active", "category": "InProgress"},
                    {"name": "Resolved", "category": "Resolved"},
                    {"name": "Closed", "category": "Completed"},
                    {"name": "Removed", "category": "Removed"},
                ]
            })
        raise AssertionError(f"unexpected path {path}")

    _install_mock(monkeypatch, handler)
    p = _project()
    p.default_work_item_type = "Bug"  # type: ignore[misc]
    spec = AzureDevOpsProvider().list_statuses(p, token="t")
    assert "Closed" in spec.values
    assert "Removed" in spec.values
    assert spec.hints["terminal_completed"] == "Closed"
    assert spec.hints["terminal_declined"] == "Removed"
    assert spec.hints["default_open"] == "New"
    assert set(spec.hints["terminal"]) == {"Closed", "Removed"}


# ---------- list_fields ------------------------------------------------------


def _fields_handler_with_type(
    work_item_type: str,
    fields_payload: list[dict],
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a mock handler that returns the given fields for a specific type."""
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        fields_path = f"/_apis/wit/workitemtypes/{work_item_type}/fields"
        if path.endswith(fields_path):
            return _json({"value": fields_payload})
        raise AssertionError(f"unexpected path {path}")
    return handler


def test_list_fields_picklist_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """A field with allowedValues maps to FieldSpec.allowed_values list."""
    fields = [
        {
            "referenceName": "Custom.Status",
            "name": "Status",
            "type": "picklistString",
            "allowedValues": ["Open", "Closed"],
            "isReadOnly": False,
            "alwaysRequired": False,
        }
    ]
    p = _project()
    p.default_work_item_type = "Issue"  # type: ignore[misc]
    _install_mock(monkeypatch, _fields_handler_with_type("Issue", fields))
    result = AzureDevOpsProvider().list_fields(p, token="t")
    assert len(result) == 1
    spec = result[0]
    assert isinstance(spec, FieldSpec)
    assert spec.reference_name == "Custom.Status"
    assert spec.display_name == "Status"
    assert spec.type == "picklistString"
    assert spec.allowed_values == ["Open", "Closed"]
    assert spec.read_only is False


def test_list_fields_plain_string_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """A field with no allowedValues key maps to allowed_values=None."""
    fields = [
        {
            "referenceName": "System.Title",
            "name": "Title",
            "type": "string",
            "isReadOnly": False,
            "alwaysRequired": True,
        }
    ]
    p = _project()
    p.default_work_item_type = "Issue"  # type: ignore[misc]
    _install_mock(monkeypatch, _fields_handler_with_type("Issue", fields))
    result = AzureDevOpsProvider().list_fields(p, token="t")
    assert len(result) == 1
    assert result[0].allowed_values is None


def test_list_fields_default_work_item_type_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """When work_item_type is None the provider resolves via /workitemtypes first."""
    fields = [
        {
            "referenceName": "System.State",
            "name": "State",
            "type": "string",
            "isReadOnly": False,
            "alwaysRequired": False,
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes"):
            return _json({"value": [{"name": "Issue"}]})
        if path.endswith("/_apis/wit/workitemtypes/Issue/fields"):
            return _json({"value": fields})
        raise AssertionError(f"unexpected path {path}")

    _install_mock(monkeypatch, handler)
    # project has no default_work_item_type — forces the two-step resolution
    result = AzureDevOpsProvider().list_fields(_project(), token="t")
    assert len(result) == 1
    assert result[0].reference_name == "System.State"


def test_list_fields_explicit_work_item_type_skips_type_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing work_item_type='Task' must not hit the /workitemtypes endpoint."""
    fields = [
        {
            "referenceName": "System.Title",
            "name": "Title",
            "type": "string",
            "isReadOnly": False,
            "alwaysRequired": True,
        }
    ]
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        seen.append(path)
        if path.endswith("/_apis/wit/workitemtypes/Task/fields"):
            return _json({"value": fields})
        raise AssertionError(f"unexpected path {path}")

    _install_mock(monkeypatch, handler)
    result = AzureDevOpsProvider().list_fields(_project(), token="t", work_item_type="Task")
    assert len(result) == 1
    # /workitemtypes (no trailing type segment) must never appear
    assert not any(p.endswith("/_apis/wit/workitemtypes") for p in seen), (
        "list_fields called /workitemtypes discovery even though work_item_type was explicit"
    )


def test_list_fields_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call with the same type must not hit the HTTP endpoint again."""
    fields = [
        {
            "referenceName": "System.Title",
            "name": "Title",
            "type": "string",
            "isReadOnly": False,
            "alwaysRequired": False,
        }
    ]
    hit_count: list[int] = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/_apis/wit/workitemtypes/Issue/fields"):
            hit_count[0] += 1
            return _json({"value": fields})
        raise AssertionError(f"unexpected path {path}")

    p = _project()
    p.default_work_item_type = "Issue"  # type: ignore[misc]
    _install_mock(monkeypatch, handler)
    provider = AzureDevOpsProvider()
    provider.list_fields(p, token="t")
    provider.list_fields(p, token="t")
    assert hit_count[0] == 1, (
        f"Expected the fields endpoint to be called once, got {hit_count[0]}"
    )


def test_list_fields_read_only_and_always_required_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """isReadOnly and alwaysRequired flags are faithfully mapped."""
    fields = [
        {
            "referenceName": "System.Id",
            "name": "ID",
            "type": "integer",
            "isReadOnly": True,
            "alwaysRequired": True,
        }
    ]
    p = _project()
    p.default_work_item_type = "Issue"  # type: ignore[misc]
    _install_mock(monkeypatch, _fields_handler_with_type("Issue", fields))
    result = AzureDevOpsProvider().list_fields(p, token="t")
    assert len(result) == 1
    spec = result[0]
    assert spec.read_only is True
    assert spec.always_required is True
