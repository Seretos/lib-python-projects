"""Tests for AzureDevOpsProvider label management methods (ticket #35, #172).

Covers:
- list_labels — in-use tags derived from the project's work items (WIQL +
  batch fetch), empty project, WIQL failure (ticket #172 bug 2)
- _tags_string_from_labels — validate-and-reject on invalid input
  (ticket #172 bug 1)
- create_label / update_label / delete_label → LabelOperationUnsupported
- LabelOperationUnsupported is a NotImplementedError subclass
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
    _tags_string_from_labels,
)
from lib_python_projects.providers.base import Label, LabelOperationUnsupported


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
    """ADO provider has module-level caches — wipe between tests."""
    _cache_clear_all()


# ---------- list_labels (ticket #172 bug 2: derive from in-use work items) ---


def _wiql_payload(ids: list[int]) -> dict:
    return {"workItems": [{"id": i} for i in ids]}


def _batch_payload(tag_strings: list[str | None]) -> dict:
    return {
        "value": [
            {"id": idx + 1, "fields": ({"System.Tags": tags} if tags is not None else {})}
            for idx, tags in enumerate(tag_strings)
        ]
    }


def test_list_labels_derives_from_work_item_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (ticket #172 bug 2): list_labels must derive the tag list
    from tags actually applied to work items (WIQL + batch fetch), and must
    NEVER call the org/project tag-catalog endpoint (`_apis/wit/tags`),
    which leaks catalog-only tags that aren't on any work item."""
    requested_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requested_paths.append(req.url.path)
        assert "_apis/wit/tags" not in req.url.path, (
            "list_labels must not call the tag-catalog endpoint"
        )
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/wiql"):
            assert "/seredos/azure-tests" in req.url.path
            return _json(_wiql_payload([1]))
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/workitemsbatch"):
            return _json(_batch_payload(["bug"]))
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert labels == [Label(name="bug", color="", description="")]
    assert any(p.endswith("/_apis/wit/wiql") for p in requested_paths)


def test_list_labels_no_work_items_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """WIQL returns no work items → empty list, and no batch call is issued."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/wiql"):
            return _json(_wiql_payload([]))
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert labels == []


def test_list_labels_wiql_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """WIQL endpoint returns a non-success status → empty list (best-effort),
    no exception raised."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert labels == []


def test_list_labels_batch_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """WIQL succeeds but the follow-up batch fetch fails → empty list
    (best-effort), no exception raised."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/wiql"):
            return _json(_wiql_payload([1]))
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/workitemsbatch"):
            return _json({"message": "Server Error"}, status_code=500)
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert labels == []


def test_list_labels_empty_tags_contribute_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Work items with no/empty System.Tags don't produce spurious labels,
    and tags shared across items are deduped + sorted in the union."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/wiql"):
            return _json(_wiql_payload([1, 2, 3]))
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/workitemsbatch"):
            return _json(_batch_payload(["bug; urgent", "", None]))
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert labels == [
        Label(name="bug", color="", description=""),
        Label(name="urgent", color="", description=""),
    ]


# ---------- _tags_string_from_labels (ticket #172 bug 1: validate-and-reject) -


def test_tags_string_from_labels_rejects_semicolon() -> None:
    """A label containing ';' (ADO's tag separator) must raise ValueError
    instead of silently corrupting the joined tag string."""
    with pytest.raises(ValueError):
        _tags_string_from_labels(["a;b"])


def test_tags_string_from_labels_rejects_empty_string() -> None:
    """An empty-string label must raise ValueError instead of being
    silently dropped."""
    with pytest.raises(ValueError):
        _tags_string_from_labels(["", "x"])


def test_tags_string_from_labels_rejects_whitespace_only() -> None:
    """A whitespace-only label must raise ValueError instead of being
    silently dropped."""
    with pytest.raises(ValueError):
        _tags_string_from_labels(["  "])


def test_tags_string_from_labels_rejects_none() -> None:
    """A None entry in the labels list must raise ValueError."""
    with pytest.raises(ValueError):
        _tags_string_from_labels([None])  # type: ignore[list-item]


def test_tags_string_from_labels_happy_path_unchanged() -> None:
    """Valid input still dedupes, sorts, and joins with '; ' as before."""
    assert _tags_string_from_labels(["b", "a", "a"]) == "a; b"


# ---------- mutating operations raise LabelOperationUnsupported ---------------


def test_create_label_raises_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_label raises LabelOperationUnsupported without any HTTP call."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(LabelOperationUnsupported) as exc:
        AzureDevOpsProvider().create_label(_project(), token="t", name="new-tag")
    assert seen == [], "no HTTP call expected"
    assert exc.value.operation == "create_label"
    assert exc.value.provider == "azuredevops"


def test_update_label_raises_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_label raises LabelOperationUnsupported without any HTTP call."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(LabelOperationUnsupported) as exc:
        AzureDevOpsProvider().update_label(
            _project(), token="t", name="old-tag", new_name="new-tag"
        )
    assert seen == [], "no HTTP call expected"
    assert exc.value.operation == "update_label"
    assert exc.value.provider == "azuredevops"


def test_delete_label_raises_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_label raises LabelOperationUnsupported without any HTTP call."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _json({})

    _install_mock(monkeypatch, handler)
    with pytest.raises(LabelOperationUnsupported) as exc:
        AzureDevOpsProvider().delete_label(_project(), token="t", name="some-tag")
    assert seen == [], "no HTTP call expected"
    assert exc.value.operation == "delete_label"
    assert exc.value.provider == "azuredevops"


# ---------- LabelOperationUnsupported class invariants -----------------------


def test_label_operation_unsupported_is_not_implemented_error() -> None:
    """LabelOperationUnsupported must subclass NotImplementedError."""
    assert issubclass(LabelOperationUnsupported, NotImplementedError)


def test_label_operation_unsupported_carries_operation_and_provider() -> None:
    """Exception attributes survive construction."""
    exc = LabelOperationUnsupported("create_label", "azuredevops")
    assert exc.operation == "create_label"
    assert exc.provider == "azuredevops"
    assert "create_label" in str(exc)
    assert "azuredevops" in str(exc)
