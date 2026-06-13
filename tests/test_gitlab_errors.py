"""Tests for GitLab provider rate-limit and ProviderError hierarchy (ticket #96)."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from lib_python_projects.providers.base import ProviderError, RateLimitError
from lib_python_projects.providers.gitlab import GitLabError, _check


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


def test_gitlab_429_retry_after_header_seconds() -> None:
    """429 + Retry-After (seconds) header raises RateLimitError with correct retry_after."""
    resp = _resp(429, {"message": "429 Too Many Requests"}, {"Retry-After": "30"})
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.status == 429
    assert exc.value.retry_after == 30


def test_gitlab_429_ratelimit_reset_epoch_header() -> None:
    """429 + RateLimit-Reset (unix epoch) header raises RateLimitError with retry_after ≈ 45."""
    reset_ts = int(time.time()) + 45
    resp = _resp(
        429,
        {"message": "429 Too Many Requests"},
        {"RateLimit-Reset": str(reset_ts)},
    )
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.status == 429
    assert exc.value.retry_after is not None
    assert abs(exc.value.retry_after - 45) <= 2


def test_gitlab_429_no_header_retry_after_none() -> None:
    """429 with no rate-limit headers raises RateLimitError with retry_after=None."""
    resp = _resp(429, {"message": "Too Many Requests"})
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.status == 429
    assert exc.value.retry_after is None


def test_gitlab_retry_after_takes_priority_over_reset() -> None:
    """When both Retry-After and RateLimit-Reset are present, Retry-After wins."""
    reset_ts = int(time.time()) + 999
    resp = _resp(
        429,
        {"message": "Too Many Requests"},
        {"Retry-After": "30", "RateLimit-Reset": str(reset_ts)},
    )
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.retry_after == 30


# ---------- Non-429 uses GitLabError -----------------------------------------


def test_gitlab_403_raises_gitlab_error() -> None:
    """Non-429 error (403) raises GitLabError, not RateLimitError."""
    resp = _resp(403, {"message": "Forbidden"})
    with pytest.raises(GitLabError) as exc:
        _check(resp)
    assert exc.value.status == 403


def test_gitlab_404_raises_gitlab_error() -> None:
    """404 raises GitLabError."""
    resp = _resp(404, {"message": "Not Found"})
    with pytest.raises(GitLabError) as exc:
        _check(resp)
    assert exc.value.status == 404
    assert not isinstance(exc.value, RateLimitError)


# ---------- ProviderError hierarchy ------------------------------------------


def test_gitlab_error_is_instance_of_provider_error() -> None:
    """GitLabError must be an instance of ProviderError and RuntimeError."""
    err = GitLabError(404, "not found")
    assert isinstance(err, ProviderError)
    assert isinstance(err, RuntimeError)


def test_gitlab_error_str_contract() -> None:
    """str(GitLabError) must keep the 'GitLab NNN: <message>' format."""
    err = GitLabError(404, "Not Found")
    assert str(err) == "GitLab 404: Not Found"
    assert err.status == 404
    assert err.message == "Not Found"


def test_rate_limit_error_is_instance_of_provider_error() -> None:
    """RateLimitError raised from _check is also a ProviderError."""
    resp = _resp(429, {"message": "Too Many Requests"})
    with pytest.raises(ProviderError) as exc:
        _check(resp)
    assert isinstance(exc.value, RateLimitError)


# ---------- Ticket #100: RateLimit-ResetTime fallback ------------------------


def test_gitlab_429_ratelimit_resettime_fallback() -> None:
    """429 + RateLimit-ResetTime (epoch) fallback when Retry-After and
    RateLimit-Reset are both absent."""
    reset_ts = int(time.time()) + 120
    resp = _resp(
        429,
        {"message": "Too Many Requests"},
        {"RateLimit-ResetTime": str(reset_ts)},
    )
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.status == 429
    assert exc.value.retry_after is not None
    assert abs(exc.value.retry_after - 120) <= 2


def test_gitlab_429_retry_after_priority_over_resettime() -> None:
    """When Retry-After is present, it takes priority over RateLimit-ResetTime."""
    reset_ts = int(time.time()) + 999
    resp = _resp(
        429,
        {"message": "Too Many Requests"},
        {"Retry-After": "25", "RateLimit-ResetTime": str(reset_ts)},
    )
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.retry_after == 25


def test_gitlab_429_ratelimit_reset_priority_over_resettime() -> None:
    """When RateLimit-Reset is present (no Retry-After), it takes priority over
    RateLimit-ResetTime."""
    reset_ts = int(time.time()) + 60
    reset_time_ts = int(time.time()) + 999
    resp = _resp(
        429,
        {"message": "Too Many Requests"},
        {"RateLimit-Reset": str(reset_ts), "RateLimit-ResetTime": str(reset_time_ts)},
    )
    with pytest.raises(RateLimitError) as exc:
        _check(resp)
    assert exc.value.retry_after is not None
    assert abs(exc.value.retry_after - 60) <= 2
