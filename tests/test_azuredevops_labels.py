"""Tests for AzureDevOpsProvider label management methods (ticket #35).

Covers:
- list_labels — tags endpoint happy path, empty tags
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


# ---------- list_labels -------------------------------------------------------


def test_list_labels_returns_label_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tags endpoint returns items → list[Label] with color='' and description=''."""
    tags_payload = {
        "value": [
            {"id": "1", "name": "bug"},
            {"id": "2", "name": "enhancement"},
        ],
        "count": 2,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert "_apis/wit/tags" in req.url.path
        return _json(tags_payload)

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert isinstance(labels, list)
    assert len(labels) == 2
    assert all(isinstance(lbl, Label) for lbl in labels)
    assert labels[0].name == "bug"
    assert labels[0].color == ""        # ADO has no color concept
    assert labels[0].description == ""  # ADO has no description concept
    assert labels[1].name == "enhancement"


def test_list_labels_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tags endpoint returns empty value array → empty list."""
    tags_payload = {"value": [], "count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(tags_payload)

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert labels == []


def test_list_labels_endpoint_unavailable_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the tags endpoint returns a non-success status → empty list (best-effort)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    labels = AzureDevOpsProvider().list_labels(_project(), token="t")
    assert labels == []


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
