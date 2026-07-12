"""Tests for `bulk_update_tickets` (ticket #149) on all three providers.

`bulk_update_tickets` loops over an explicit `ticket_ids` list, calling the
provider's own `update_ticket` once per id and capturing per-item
success/error instead of aborting the whole batch on the first failure.
These tests exercise that partial-failure contract end to end via
`httpx.MockTransport`, mirroring the fixture/helper style used in
`tests/test_provider_parity.py`.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers import gitlab as gitlab_provider
from lib_python_projects.providers import azuredevops as azuredevops_provider
from lib_python_projects.providers.base import BulkTicketResult
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider, _cache_clear_all


def _resp(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


@pytest.fixture(autouse=True)
def _clear_ado_caches() -> None:
    _cache_clear_all()


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def _gh_project() -> ProjectConfig:
    return ProjectConfig(id="acme", provider="github", path="acme/backend")


def _gh_issue_payload(number: int, labels: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [{"name": n} for n in (labels or [])],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _install_github_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers={"Accept": "application/vnd.github+json"},
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


def _github_bulk_handler(
    *, missing_ids: set[int] = frozenset(), patch_error_ids: dict[int, int] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    patch_error_ids = patch_error_ids or {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        # Label-existence validation for a non-AI label in labels_add.
        if method == "GET" and "/labels/" in path and not path.endswith(
            ("/labels/ai-modified", "/labels/ai-generated")
        ):
            return _resp({"name": path.rsplit("/", 1)[-1]})
        # Best-effort ai-modified label ensure (POST /repos/.../labels).
        if method == "POST" and path.endswith("/labels"):
            return _resp({"name": "ai-modified", "color": "ededed"}, status_code=201)
        if method == "GET" and "/issues/" in path:
            num = int(path.rsplit("/", 1)[-1])
            if num in missing_ids:
                return _resp({"message": "Not Found"}, status_code=404)
            return _resp(_gh_issue_payload(num))
        if method == "PATCH" and "/issues/" in path:
            num = int(path.rsplit("/", 1)[-1])
            if num in patch_error_ids:
                return _resp(
                    {"message": "server error"}, status_code=patch_error_ids[num]
                )
            payload = json.loads(req.content.decode())
            labels = [lbl for lbl in payload.get("labels") or []]
            return _resp(_gh_issue_payload(num, labels=labels))
        raise AssertionError(f"unexpected {method} {path}")

    return handler


def test_github_bulk_update_success_all_items(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _github_bulk_handler()
    _install_github_mock(monkeypatch, handler)
    results = GitHubProvider().bulk_update_tickets(
        _gh_project(), "t", ["1", "2", "3"], labels_add=["X"],
    )
    assert [r.ticket_id for r in results] == ["1", "2", "3"]
    for r in results:
        assert r.error is None
        assert r.ticket is not None
        assert "X" in r.ticket.labels


def test_github_bulk_update_partial_failure_404_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_github_mock(
        monkeypatch, _github_bulk_handler(missing_ids={3}),
    )
    results = GitHubProvider().bulk_update_tickets(
        _gh_project(), "t", ["1", "2", "3", "4", "5"], labels_add=["X"],
    )
    assert len(results) == 5
    assert results[2].ticket_id == "3"
    assert results[2].ticket is None
    assert results[2].error
    assert "#3" in results[2].error
    assert "not found" in results[2].error
    for i in (0, 1, 3, 4):
        assert results[i].ticket is not None
        assert results[i].error is None
    # The loop must not have aborted: ids 4 and 5 still got PATCHed.
    patch_ids = {
        int(r.url.path.rsplit("/", 1)[-1]) for r in seen if r.method == "PATCH"
    }
    assert patch_ids == {1, 2, 4, 5}


def test_github_bulk_update_empty_list_returns_empty_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty ticket_ids list")

    seen = _install_github_mock(monkeypatch, handler)
    results = GitHubProvider().bulk_update_tickets(
        _gh_project(), "t", [], labels_add=["X"],
    )
    assert results == []
    assert seen == []


def test_github_bulk_update_non_404_error_mid_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_github_mock(
        monkeypatch, _github_bulk_handler(patch_error_ids={3: 500}),
    )
    results = GitHubProvider().bulk_update_tickets(
        _gh_project(), "t", ["1", "2", "3", "4"], labels_add=["X"],
    )
    assert results[2].ticket is None
    assert results[2].error
    assert "500" in results[2].error
    for i in (0, 1, 3):
        assert results[i].ticket is not None
        assert results[i].error is None


def test_github_bulk_update_value_error_per_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """`status='closed'` (bare, unsupported) raises ValueError inside
    `update_ticket` — must be captured per-item, never propagated out of
    `bulk_update_tickets`."""
    _install_github_mock(monkeypatch, _github_bulk_handler())
    results = GitHubProvider().bulk_update_tickets(
        _gh_project(), "t", ["1", "2", "3"], status="closed",
    )
    assert len(results) == 3
    for r in results:
        assert r.ticket is None
        assert r.error
        assert "closed" in r.error


def test_github_bulk_update_preserves_order_with_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_github_mock(monkeypatch, _github_bulk_handler())
    ids = ["3", "1", "3", "2"]
    results = GitHubProvider().bulk_update_tickets(
        _gh_project(), "t", ids, labels_add=["X"],
    )
    assert [r.ticket_id for r in results] == ids


def test_github_bulk_update_accepts_custom_fields_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub's `update_ticket` accepts `custom_fields`, so its
    `bulk_update_tickets` must too — `None` is a silent no-op."""
    _install_github_mock(monkeypatch, _github_bulk_handler())
    results = GitHubProvider().bulk_update_tickets(
        _gh_project(), "t", ["1"], labels_add=["X"], custom_fields=None,
    )
    assert results[0].error is None


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


def _gl_project() -> ProjectConfig:
    return ProjectConfig(id="gl", provider="gitlab", path="Seredos/gitlab-tests")


def _gl_issue_payload(iid: int, labels: list[str] | None = None) -> dict:
    return {
        "iid": iid,
        "title": f"Issue {iid}",
        "description": "",
        "state": "opened",
        "author": {"username": "a"},
        "assignees": [],
        "labels": labels or [],
        "web_url": f"https://gitlab.com/seredos/gitlab-tests/-/issues/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _install_gitlab_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=f"{(project.base_url or 'https://gitlab.com').rstrip('/')}/api/v4",
            headers={"Accept": "application/json"},
            transport=transport,
        )

    monkeypatch.setattr(gitlab_provider, "_client", fake_client)
    return seen


def _gitlab_bulk_handler(
    *, missing_iids: set[int] = frozenset(), put_error_iids: dict[int, int] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    put_error_iids = put_error_iids or {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        if method == "GET" and "/issues/" in path:
            iid = int(path.rsplit("/", 1)[-1])
            if iid in missing_iids:
                return _resp({"message": "404 Not Found"}, status_code=404)
            return _resp(_gl_issue_payload(iid))
        if method == "PUT" and "/issues/" in path:
            iid = int(path.rsplit("/", 1)[-1])
            if iid in put_error_iids:
                return _resp(
                    {"message": "server error"}, status_code=put_error_iids[iid]
                )
            payload = json.loads(req.content.decode())
            add_labels = [
                lbl for lbl in (payload.get("add_labels") or "").split(",") if lbl
            ]
            return _resp(_gl_issue_payload(iid, labels=add_labels))
        raise AssertionError(f"unexpected {method} {path}")

    return handler


def test_gitlab_bulk_update_success_all_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_gitlab_mock(monkeypatch, _gitlab_bulk_handler())
    results = GitLabProvider().bulk_update_tickets(
        _gl_project(), "t", ["1", "2", "3"], labels_add=["X"],
    )
    assert [r.ticket_id for r in results] == ["1", "2", "3"]
    for r in results:
        assert r.error is None
        assert r.ticket is not None
        assert "X" in r.ticket.labels


def test_gitlab_bulk_update_partial_failure_404_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_gitlab_mock(
        monkeypatch, _gitlab_bulk_handler(missing_iids={3}),
    )
    results = GitLabProvider().bulk_update_tickets(
        _gl_project(), "t", ["1", "2", "3", "4", "5"], labels_add=["X"],
    )
    assert len(results) == 5
    assert results[2].ticket_id == "3"
    assert results[2].ticket is None
    assert results[2].error
    assert "#3" in results[2].error
    assert "not found" in results[2].error
    for i in (0, 1, 3, 4):
        assert results[i].ticket is not None
        assert results[i].error is None
    put_iids = {int(r.url.path.rsplit("/", 1)[-1]) for r in seen if r.method == "PUT"}
    assert put_iids == {1, 2, 4, 5}


def test_gitlab_bulk_update_empty_list_returns_empty_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty ticket_ids list")

    seen = _install_gitlab_mock(monkeypatch, handler)
    results = GitLabProvider().bulk_update_tickets(
        _gl_project(), "t", [], labels_add=["X"],
    )
    assert results == []
    assert seen == []


def test_gitlab_bulk_update_non_404_error_mid_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gitlab_mock(
        monkeypatch, _gitlab_bulk_handler(put_error_iids={3: 500}),
    )
    results = GitLabProvider().bulk_update_tickets(
        _gl_project(), "t", ["1", "2", "3", "4"], labels_add=["X"],
    )
    assert results[2].ticket is None
    assert results[2].error
    assert "500" in results[2].error
    for i in (0, 1, 3):
        assert results[i].ticket is not None
        assert results[i].error is None


def test_gitlab_bulk_update_value_error_per_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised status value raises ValueError inside `update_ticket`
    (client-side, before any write) — must be captured per-item."""
    _install_gitlab_mock(monkeypatch, _gitlab_bulk_handler())
    results = GitLabProvider().bulk_update_tickets(
        _gl_project(), "t", ["1", "2", "3"], status="bogus",
    )
    assert len(results) == 3
    for r in results:
        assert r.ticket is None
        assert r.error
        assert "Accepted: open, closed." in r.error


def test_gitlab_bulk_update_preserves_order_with_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gitlab_mock(monkeypatch, _gitlab_bulk_handler())
    ids = ["3", "1", "3", "2"]
    results = GitLabProvider().bulk_update_tickets(
        _gl_project(), "t", ids, labels_add=["X"],
    )
    assert [r.ticket_id for r in results] == ids


def test_gitlab_bulk_update_custom_fields_kwarg_raises_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab's `update_ticket` has no `custom_fields` parameter, so
    `bulk_update_tickets` must not accept one either — the signature
    divergence from GitHub/Azure DevOps is intentional."""
    _install_gitlab_mock(monkeypatch, _gitlab_bulk_handler())
    with pytest.raises(TypeError):
        GitLabProvider().bulk_update_tickets(
            _gl_project(), "t", ["1"], custom_fields={"foo": "bar"},
        )


# ---------------------------------------------------------------------------
# Azure DevOps
# ---------------------------------------------------------------------------


def _ado_project() -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path="seredos/azure-tests/azure-tests",
        token_env="AZURE_TOKEN",
        default_work_item_type="Task",
    )


def _ado_wi_payload(work_item_id: int, **fields_override: object) -> dict:
    fields = {
        "System.Title": f"Item {work_item_id}",
        "System.Description": "<p>Body</p>",
        "System.State": "To Do",
        "System.WorkItemType": "Task",
        "System.Tags": "",
        "System.CreatedDate": "2026-05-18T10:00:00Z",
        "System.ChangedDate": "2026-05-18T11:00:00Z",
    }
    fields.update(fields_override)
    return {"id": work_item_id, "fields": fields}


def _install_azuredevops_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(
            base_url=base, headers={"Accept": "application/json"}, transport=transport,
        )

    monkeypatch.setattr(azuredevops_provider, "_client", fake_client)
    return seen


def _ado_bulk_handler(
    *, missing_ids: set[int] = frozenset(), patch_error_ids: dict[int, int] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    patch_error_ids = patch_error_ids or {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        if method == "GET" and "/workitems/" in path:
            wid = int(path.rsplit("/", 1)[-1])
            if wid in missing_ids:
                return _resp({"message": "TF401232: not found"}, status_code=404)
            return _resp(_ado_wi_payload(wid))
        if method == "PATCH" and "/workitems/" in path:
            wid = int(path.rsplit("/", 1)[-1])
            if wid in patch_error_ids:
                return _resp(
                    {"message": "server error"}, status_code=patch_error_ids[wid]
                )
            patch = json.loads(req.content.decode())
            tags_op = next(
                (op for op in patch if op["path"] == "/fields/System.Tags"), None
            )
            overrides = {"System.Tags": tags_op["value"]} if tags_op else {}
            return _resp(_ado_wi_payload(wid, **overrides))
        raise AssertionError(f"unexpected {method} {path}")

    return handler


def test_azuredevops_bulk_update_success_all_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_azuredevops_mock(monkeypatch, _ado_bulk_handler())
    results = AzureDevOpsProvider().bulk_update_tickets(
        _ado_project(), "t", ["1", "2", "3"], labels_add=["X"],
    )
    assert [r.ticket_id for r in results] == ["1", "2", "3"]
    for r in results:
        assert r.error is None
        assert r.ticket is not None
        assert "X" in r.ticket.labels


def test_azuredevops_bulk_update_partial_failure_404_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_azuredevops_mock(
        monkeypatch, _ado_bulk_handler(missing_ids={3}),
    )
    results = AzureDevOpsProvider().bulk_update_tickets(
        _ado_project(), "t", ["1", "2", "3", "4", "5"], labels_add=["X"],
    )
    assert len(results) == 5
    assert results[2].ticket_id == "3"
    assert results[2].ticket is None
    assert results[2].error
    assert "#3" in results[2].error
    assert "not found" in results[2].error
    for i in (0, 1, 3, 4):
        assert results[i].ticket is not None
        assert results[i].error is None
    patch_ids = {
        int(r.url.path.rsplit("/", 1)[-1]) for r in seen if r.method == "PATCH"
    }
    assert patch_ids == {1, 2, 4, 5}


def test_azuredevops_bulk_update_empty_list_returns_empty_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an empty ticket_ids list")

    seen = _install_azuredevops_mock(monkeypatch, handler)
    results = AzureDevOpsProvider().bulk_update_tickets(
        _ado_project(), "t", [], labels_add=["X"],
    )
    assert results == []
    assert seen == []


def test_azuredevops_bulk_update_non_404_error_mid_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_azuredevops_mock(
        monkeypatch, _ado_bulk_handler(patch_error_ids={3: 500}),
    )
    results = AzureDevOpsProvider().bulk_update_tickets(
        _ado_project(), "t", ["1", "2", "3", "4"], labels_add=["X"],
    )
    assert results[2].ticket is None
    assert results[2].error
    assert "500" in results[2].error
    for i in (0, 1, 3):
        assert results[i].ticket is not None
        assert results[i].error is None


def test_azuredevops_bulk_update_value_error_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid `status` value is rejected by ADO's PATCH (400 +
    RuleValidationException), re-wrapped by `update_ticket` into a curated
    `ValueError` — must be captured per-item, not propagated."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        if method == "GET" and "/workitems/" in path:
            wid = int(path.rsplit("/", 1)[-1])
            return _resp(_ado_wi_payload(wid))
        if method == "PATCH" and "/workitems/" in path:
            return _resp(
                {
                    "message": (
                        "The field 'State' contains the value 'Bogus' which "
                        "is not in the list of supported values"
                    ),
                    "typeKey": "RuleValidationException",
                },
                status_code=400,
            )
        if path.endswith("/_apis/wit/workitemtypes/Task/states"):
            return _resp(
                {"value": [{"name": "To Do"}, {"name": "Done"}]}
            )
        raise AssertionError(f"unexpected {method} {path}")

    _install_azuredevops_mock(monkeypatch, handler)
    results = AzureDevOpsProvider().bulk_update_tickets(
        _ado_project(), "t", ["1", "2", "3"], status="Bogus",
    )
    assert len(results) == 3
    for r in results:
        assert r.ticket is None
        assert r.error
        assert "unsupported status" in r.error
        assert "Azure DevOps" in r.error


def test_azuredevops_bulk_update_preserves_order_with_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_azuredevops_mock(monkeypatch, _ado_bulk_handler())
    ids = ["3", "1", "3", "2"]
    results = AzureDevOpsProvider().bulk_update_tickets(
        _ado_project(), "t", ids, labels_add=["X"],
    )
    assert [r.ticket_id for r in results] == ids


def test_azuredevops_bulk_update_accepts_custom_fields_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure DevOps's `update_ticket` accepts `custom_fields`, like
    GitHub's — its `bulk_update_tickets` must too."""
    _install_azuredevops_mock(monkeypatch, _ado_bulk_handler())
    results = AzureDevOpsProvider().bulk_update_tickets(
        _ado_project(), "t", ["1"], labels_add=["X"], custom_fields=None,
    )
    assert results[0].error is None


# ---------------------------------------------------------------------------
# Cross-provider structural checks (light — the full versions live in
# tests/test_provider_parity.py alongside the other *_expose_* assertions).
# ---------------------------------------------------------------------------


def test_bulk_ticket_result_is_exported_from_providers_package() -> None:
    from lib_python_projects.providers import BulkTicketResult as PublicBulkTicketResult

    assert PublicBulkTicketResult is BulkTicketResult
