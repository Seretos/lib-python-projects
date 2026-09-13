"""Driving tests for `AzureDevOpsProvider.list_issue_templates` (ticket #259,
`IssueTemplateProvider` mixin).

Mocking follows the `tests/test_azuredevops_misc.py` convention
(`_install_mock`/`_json`, `azure_mod._client(project, token)` signature).

Round-3 review (findings 1+2): the real Azure DevOps work-item-templates
API is **team-scoped**, not project-scoped
(`GET .../{team}/_apis/wit/templates`), and its list response is
**shallow** -- no `fields` mapping -- so a full template body (with
`fields`) requires a second, per-template
`GET .../{team}/_apis/wit/templates/{templateId}` call. Both shapes were
confirmed against Microsoft's official REST API docs ("Work Item
Templates - List" / "- Get", `azure-devops-rest-7.1`). The mocking below
was updated from the original single-call, project-scoped shape to match.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import AzureDevOpsError, AzureDevOpsProvider


def _project(
    path: str = "seredos/azure-tests/azure-tests",
    default_work_item_type: str = "Bug",
    default_team: str | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path=path,
        token_env="AZURE_TOKEN",
        default_work_item_type=default_work_item_type,
        default_team=default_team,
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
        headers = {"Accept": "application/json"}
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen


_TEMPLATES_LIST_SUFFIX = "/_apis/wit/templates"


def _is_list_path(path: str) -> bool:
    """True for the shallow list endpoint, false for a per-template detail
    path (which ends with an extra `/{templateId}` segment)."""
    return path.endswith(_TEMPLATES_LIST_SUFFIX)


def _is_detail_path(path: str, template_id: str) -> bool:
    return path.endswith(f"{_TEMPLATES_LIST_SUFFIX}/{template_id}")


def _standard_handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if _is_detail_path(path, "tmpl-1"):
        return _json({
            "id": "tmpl-1",
            "name": "Standard Bug",
            "workItemTypeName": "Bug",
            "fields": {
                "System.Title": "",
                "Microsoft.VSTS.TCM.ReproSteps": "",
                "Microsoft.VSTS.Common.Priority": "2",
            },
        })
    if _is_list_path(path):
        assert req.url.params.get("workitemtypename") == "Bug"
        return _json({
            "count": 1,
            "value": [
                {
                    "id": "tmpl-1",
                    "name": "Standard Bug",
                    "workItemTypeName": "Bug",
                    "url": f"https://dev.azure.com{path}/tmpl-1",
                },
            ],
        })
    raise AssertionError(f"unexpected request: {req.method} {path}")


# ---------- requirement 1: parses work-item templates -----------------------


def test_list_issue_templates_returns_workitem_kind_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(monkeypatch, _standard_handler)
    result = AzureDevOpsProvider().list_issue_templates(_project(), "tok")

    assert len(result) == 1
    tmpl = result[0]
    assert tmpl.name == "Standard Bug"
    assert tmpl.filename == ""
    assert tmpl.title_prefix == ""
    assert tmpl.labels == []
    assert tmpl.kind == "workitem"

    field_ids = {f.field_id for f in tmpl.fields}
    assert field_ids == {"System.Title", "Microsoft.VSTS.TCM.ReproSteps", "Microsoft.VSTS.Common.Priority"}
    for f in tmpl.fields:
        assert f.label == f.field_id
        assert f.type == "textarea"
        assert f.required is False
        assert f.description is None


# ---------- round-3 finding 1: team-scoped URL -------------------------------


def test_list_issue_templates_uses_team_scoped_url_defaulting_to_project_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `default_team` configured -> falls back to the project name
    itself (Azure DevOps's own convention for an unconfigured default
    team)."""
    seen = _install_mock(monkeypatch, _standard_handler)
    AzureDevOpsProvider().list_issue_templates(_project(), "tok")

    list_requests = [r for r in seen if _is_list_path(r.url.path)]
    assert len(list_requests) == 1
    assert list_requests[0].url.path == "/seredos/azure-tests/azure-tests/_apis/wit/templates"


def test_list_issue_templates_uses_configured_default_team(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_mock(monkeypatch, _standard_handler)
    AzureDevOpsProvider().list_issue_templates(
        _project(default_team="Custom Team"), "tok",
    )

    list_requests = [r for r in seen if _is_list_path(r.url.path)]
    assert len(list_requests) == 1
    # `httpx.URL.path` percent-decodes; assert on the raw wire path instead
    # so the space-in-team-name encoding is actually exercised.
    assert list_requests[0].url.raw_path.split(b"?")[0].decode() == (
        "/seredos/azure-tests/Custom%20Team/_apis/wit/templates"
    )


# ---------- round-3 finding 2: per-template detail fetch ---------------------


def test_list_issue_templates_fetches_full_template_detail_per_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list response alone carries no `fields` -- proves the fields on
    the returned `IssueTemplate` were sourced from the per-template detail
    fetch, not (impossibly) from the shallow list entry."""
    seen = _install_mock(monkeypatch, _standard_handler)
    result = AzureDevOpsProvider().list_issue_templates(_project(), "tok")

    detail_requests = [r for r in seen if _is_detail_path(r.url.path, "tmpl-1")]
    assert len(detail_requests) == 1
    assert result[0].fields  # sourced from the detail call, list carries no "fields"


# ---------- requirement 2: error contract -----------------------------------


def test_list_issue_templates_404_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if _is_list_path(req.url.path):
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    assert AzureDevOpsProvider().list_issue_templates(_project(), "tok") == []


def test_list_issue_templates_empty_listing_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if _is_list_path(req.url.path):
            return _json({"count": 0, "value": []})
        raise AssertionError(f"unexpected request: {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    assert AzureDevOpsProvider().list_issue_templates(_project(), "tok") == []


@pytest.mark.parametrize("status", [401, 403, 500])
def test_list_issue_templates_auth_and_server_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if _is_list_path(req.url.path):
            return _json({"message": "boom"}, status_code=status)
        raise AssertionError(f"unexpected request: {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().list_issue_templates(_project(), "tok")
    assert exc.value.status == status


@pytest.mark.parametrize("status", [401, 403, 500])
def test_list_issue_templates_detail_call_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    """Same error contract, but the failure comes from the per-template
    detail fetch rather than the list call."""
    def handler(req: httpx.Request) -> httpx.Response:
        if _is_detail_path(req.url.path, "tmpl-1"):
            return _json({"message": "boom"}, status_code=status)
        if _is_list_path(req.url.path):
            return _json({
                "count": 1,
                "value": [{"id": "tmpl-1", "name": "Standard Bug", "workItemTypeName": "Bug"}],
            })
        raise AssertionError(f"unexpected request: {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().list_issue_templates(_project(), "tok")
    assert exc.value.status == status
