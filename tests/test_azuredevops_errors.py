"""Tests for Azure DevOps provider rate-limit and ProviderError hierarchy (ticket #96)."""
from __future__ import annotations

import json

import httpx
import pytest

from lib_python_projects.providers.azuredevops import AzureDevOpsError, _check
from lib_python_projects.providers.base import ProviderError, RateLimitError


def _resp(
    status_code: int,
    payload: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    body = json.dumps(payload or {}).encode("utf-8")
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


# ---------- RateLimitError on 429 --------------------------------------------


def test_azuredevops_429_retry_after_header() -> None:
    """429 + Retry-After header raises RateLimitError with correct retry_after."""
    resp = _resp(
        429,
        {"message": "Too many requests"},
        {"Retry-After": "60"},
    )
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.status == 429
    assert exc.value.retry_after == 60


def test_azuredevops_429_no_header_retry_after_none() -> None:
    """429 with no Retry-After header raises RateLimitError with retry_after=None."""
    resp = _resp(429, {"message": "Too many requests"})
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.status == 429
    assert exc.value.retry_after is None


def test_azuredevops_429_unparseable_retry_after_none() -> None:
    """429 with a non-integer Retry-After header yields retry_after=None."""
    resp = _resp(
        429,
        {"message": "Too many requests"},
        {"Retry-After": "not-a-number"},
    )
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.retry_after is None


# ---------- Non-429 uses AzureDevOpsError ------------------------------------


def test_azuredevops_400_raises_azure_error() -> None:
    """A regular 400 raises AzureDevOpsError, not RateLimitError."""
    resp = _resp(400, {"message": "Bad request", "typeKey": "SomeOtherError"})
    with pytest.raises(AzureDevOpsError) as exc:
        _check(resp)
    assert not isinstance(exc.value, RateLimitError)


def test_azuredevops_500_raises_azure_error() -> None:
    """A genuine 500 raises AzureDevOpsError (status preserved as 500)."""
    resp = _resp(500, {"message": "Internal Server Error"})
    with pytest.raises(AzureDevOpsError) as exc:
        _check(resp)
    assert exc.value.status == 500
    assert not isinstance(exc.value, RateLimitError)


# ---------- ProviderError hierarchy ------------------------------------------


def test_azuredevops_error_is_instance_of_provider_error() -> None:
    """AzureDevOpsError must be an instance of ProviderError and RuntimeError."""
    err = AzureDevOpsError(404, "not found")
    assert isinstance(err, ProviderError)
    assert isinstance(err, RuntimeError)


def test_azuredevops_error_str_contract() -> None:
    """str(AzureDevOpsError) must keep the 'Azure DevOps NNN: <message>' format."""
    err = AzureDevOpsError(404, "Work item not found")
    assert str(err) == "Azure DevOps 404: Work item not found"
    assert err.status == 404
    assert err.message == "Work item not found"


def test_rate_limit_error_is_provider_error() -> None:
    """RateLimitError raised from _check is also a ProviderError."""
    resp = _resp(429, {"message": "Too many requests"})
    with pytest.raises(ProviderError) as exc:
        _check(resp)
    assert isinstance(exc.value, RateLimitError)
