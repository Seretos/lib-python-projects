"""Provider-level regression tests for ticket #272.

`ETagTransport` used to replay the pagination headers (`Link`,
`X-Total-Pages`, `X-Next-Page`) captured on the FIRST response whenever a
later request for the same URL was answered 304 -- even though those
headers are not a function of the page body, so the replay was stale as
soon as the thread grew.  Every other provider test injects a bare
`MockTransport`, which bypasses `ETagTransport` entirely; these tests wire
the REAL `ETagTransport` around a mock server that derives its ETag from the
page body, answers `If-None-Match` with 304 when that page's body is
unchanged, and computes the pagination headers from the *current* thread.

`clear_etag_cache()` is never called between the two calls of a test -- the
autouse fixture only drains the store before each test starts.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers._http_cache import (
    ETagTransport,
    clear_etag_cache,
)
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_etag_cache()


# ---------- shared mock server -------------------------------------------------


def _paged(
    req: httpx.Request, items: list[dict]
) -> tuple[list[dict], int, int, int]:
    per_page = int(req.url.params.get("per_page", "30"))
    page = int(req.url.params.get("page", "1"))
    last = max(1, math.ceil(len(items) / per_page))
    chunk = items[(page - 1) * per_page : page * per_page]
    return chunk, page, per_page, last


def _respond(req: httpx.Request, chunk: list[dict], headers: dict) -> httpx.Response:
    body = json.dumps(chunk).encode("utf-8")
    etag = '"' + hashlib.md5(body).hexdigest() + '"'  # noqa: S324 - test fixture
    if req.headers.get("If-None-Match") == etag:
        # Unchanged BODY -> 304, whatever happened to the thread's page count.
        return httpx.Response(304, headers={"ETag": etag})
    return httpx.Response(
        200,
        content=body,
        headers={"Content-Type": "application/json", "ETag": etag, **headers},
    )


class _GitHubThread:
    """Mutable GitHub issue-comment thread behind a real ETagTransport."""

    base = "https://api.github.com/repos/acme/backend/issues/42/comments"

    def __init__(self, count: int) -> None:
        self.n = 0
        self.items: list[dict] = []
        self.grow_to(count)

    def grow_to(self, count: int) -> None:
        while self.n < count:
            self.n += 1
            self.items.append({
                "id": self.n,
                "user": {"login": "alice"},
                "body": f"comment {self.n}",
                "html_url": f"https://github.com/acme/backend/issues/42#c{self.n}",
                "created_at": "2024-01-01T00:00:00Z",
            })

    def handler(self, req: httpx.Request) -> httpx.Response:
        chunk, page, per_page, last = _paged(req, self.items)
        parts = []
        if last > 1:
            if page < last:
                parts.append(
                    f'<{self.base}?per_page={per_page}&page={page + 1}>; rel="next"'
                )
            parts.append(f'<{self.base}?per_page={per_page}&page={last}>; rel="last"')
        headers = {"Link": ", ".join(parts)} if parts else {}
        return _respond(req, chunk, headers)


class _GitLabThread:
    """Mutable GitLab issue-notes thread behind a real ETagTransport."""

    def __init__(self, user_notes: int) -> None:
        self.n = 0
        self.items: list[dict] = []
        self.add_user(user_notes)

    def add_user(self, count: int) -> None:
        for _ in range(count):
            self.n += 1
            self.items.append({
                "id": self.n,
                "body": f"note {self.n}",
                "system": False,
                "author": {"username": "alice"},
                "created_at": "2024-01-01T00:00:00Z",
            })

    def add_system(self) -> None:
        self.items.append({
            "id": 1000 + len(self.items),
            "body": "changed the description",
            "system": True,
            "author": {"username": "alice"},
            "created_at": "2024-01-01T00:00:00Z",
        })

    def handler(self, req: httpx.Request) -> httpx.Response:
        chunk, page, _per_page, last = _paged(req, self.items)
        headers = {
            "X-Total-Pages": str(last),
            "X-Next-Page": str(page + 1) if page < last else "",
        }
        return _respond(req, chunk, headers)


def _install_github(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def fake_client(token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=github_mod.API_BASE,
            headers={"Accept": github_mod.ACCEPT},
            timeout=30.0,
            transport=ETagTransport(httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(github_mod, "_client", fake_client)


def _install_gitlab(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=gitlab_mod._base_url(project),
            timeout=30.0,
            transport=ETagTransport(httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)


def _gh_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="github", path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _gl_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="gitlab", path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
    )


def _gh_list(limit: int, order: str = "desc", page: int = 1):
    rows, has_more = GitHubProvider().list_comments(
        _gh_project(), "t", "42", limit=limit, order=order, page=page,
    )
    return [int(c.id) for c in rows], has_more


def _gl_list(limit: int, order: str = "desc"):
    rows, has_more = GitLabProvider().list_comments(
        _gl_project(), "t", "42", limit=limit, order=order,
    )
    return [int(c.id) for c in rows], has_more


# ---------- R1: GitHub desc tail is current on a later call --------------------


def test_github_desc_tail_current_after_thread_grows_9_to_14(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRIVING (R1): the ticket's reported symptom -- 9 -> 14 comments."""
    thread = _GitHubThread(9)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(3)[0] == [9, 8, 7]
    thread.grow_to(14)
    ids, _ = _gh_list(3)
    assert ids == [14, 13, 12]


def test_github_desc_tail_current_after_thread_grows_12_to_13(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limit divides the first count (4 full pages of 3)."""
    thread = _GitHubThread(12)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(3)[0] == [12, 11, 10]
    thread.grow_to(13)
    assert _gh_list(3)[0] == [13, 12, 11]


def test_github_desc_tail_current_after_thread_grows_14_to_15(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance bullet 1's literal numbers; no page boundary is crossed,
    so this already passes on the unfixed code (guard)."""
    thread = _GitHubThread(14)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(3)[0] == [14, 13, 12]
    thread.grow_to(15)
    assert _gh_list(3)[0] == [15, 14, 13]


def test_github_desc_limit_larger_than_thread_two_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limit=30 over a 14-comment thread, then a comment is appended between
    calls: all rows newest-first, has_more False both times (guard)."""
    thread = _GitHubThread(14)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(30) == (list(range(14, 0, -1)), False)
    thread.grow_to(15)
    assert _gh_list(30) == (list(range(15, 0, -1)), False)


# ---------- R2: GitHub desc has_more exactly when older comments exist --------


def test_github_desc_has_more_flips_true_when_thread_outgrows_one_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRIVING (R2): 3 comments (single full page, no Link) -> has_more False;
    grown to 9 the same call must report older comments exist.  On the stale
    replay page 1 is 304 (body unchanged) with no Link, so the walk is
    skipped and (rows, has_more) stays ([3, 2, 1], False)."""
    thread = _GitHubThread(3)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(3) == ([3, 2, 1], False)
    thread.grow_to(9)
    assert _gh_list(3) == ([9, 8, 7], True)


def test_github_desc_has_more_exact_values_across_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhaustive (rows, has_more) at every step of a growing thread."""
    thread = _GitHubThread(9)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(3) == ([9, 8, 7], True)
    thread.grow_to(14)
    assert _gh_list(3) == ([14, 13, 12], True)
    thread.grow_to(15)
    assert _gh_list(3) == ([15, 14, 13], True)


def test_github_desc_has_more_when_slice_trims_one_older_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """12 -> 13: the fresh walk collects 4 comments for limit=3, trimming one."""
    thread = _GitHubThread(12)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(3) == ([12, 11, 10], True)
    thread.grow_to(13)
    assert _gh_list(3) == ([13, 12, 11], True)


# ---------- R3: GitHub asc has_more sees comments added after a full page 1 ----


def test_github_asc_has_more_true_after_comment_added_beyond_full_page_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRIVING (R3): page 1 full (3 of 3, no Link, has_more False); a 4th
    comment appears; page 1's body is unchanged so the server 304s, and the
    replayed headers carry no rel="next"."""
    thread = _GitHubThread(3)
    _install_github(monkeypatch, thread.handler)
    assert _gh_list(3, order="asc", page=1) == ([1, 2, 3], False)
    thread.grow_to(4)
    assert _gh_list(3, order="asc", page=1) == ([1, 2, 3], True)


# ---------- R4 / R5: GitLab ---------------------------------------------------


def test_gitlab_desc_tail_current_after_thread_grows_9_to_14(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRIVING (R4): replayed X-Total-Pages says 3, the fresh value is 5."""
    thread = _GitLabThread(9)
    _install_gitlab(monkeypatch, thread.handler)
    assert _gl_list(3)[0] == [9, 8, 7]
    thread.add_user(5)
    assert _gl_list(3)[0] == [14, 13, 12]


def test_gitlab_desc_tail_filters_system_notes_after_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system note sits on a fetched page; it must stay filtered out."""
    thread = _GitLabThread(9)
    _install_gitlab(monkeypatch, thread.handler)
    assert _gl_list(3)[0] == [9, 8, 7]
    thread.add_user(4)      # notes 10..13
    thread.add_system()     # raw item 14
    thread.add_user(1)      # note 14 -> 15 raw items, 5 pages
    assert _gl_list(3)[0] == [14, 13, 12]


def test_gitlab_desc_has_more_true_when_slice_trims_older_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRIVING (R5): 4 notes, per_page=3, limit=3.  The walk reaches page 1
    (cur == 0) yet trims note 1 from the slice, so has_more must be True."""
    thread = _GitLabThread(4)
    _install_gitlab(monkeypatch, thread.handler)
    assert _gl_list(3) == ([4, 3, 2], True)


def test_gitlab_desc_has_more_false_when_exactly_limit_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _GitLabThread(3)
    _install_gitlab(monkeypatch, thread.handler)
    assert _gl_list(3) == ([3, 2, 1], False)
