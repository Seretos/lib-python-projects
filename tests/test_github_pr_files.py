"""Tests for ticket #240: `GitHubProvider.list_pr_files` — a standalone
provider capability that discovers a PR's changed files, raw diff patch
text, and parsed per-side (LEFT/RIGHT) commentable line ranges *before*
calling `add_pr_review_comment`, instead of learning about a bad
`path`/`line` only via the 422 re-raise in that method.

Covers:
  R1: `GET /repos/{o}/{r}/pulls/{n}/files` mapping — path, change_type,
      patch (verbatim), line_ranges (via the shared `parse_diff_hunk_ranges`
      helper), additions/deletions.
      Edge cases: multi-page fetch via the `Link` header; the 30-page
      pagination cap (truncate-and-warn, never raise); renamed file ->
      `previous_path`; non-renamed entry with a stray `previous_filename`
      -> `previous_path` stays `None`; binary file (no `patch` key) ->
      `patch is None` and `line_ranges == []`; 404 -> named PR error;
      empty file list -> `[]`.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_mod
from lib_python_projects.providers.github import GitHubProvider


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
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
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_mod.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _file_entry(
    filename: str,
    status: str = "modified",
    patch: str | None = None,
    previous_filename: str | None = None,
    additions: int = 1,
    deletions: int = 1,
) -> dict:
    entry: dict = {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "changes": additions + deletions,
    }
    if patch is not None:
        entry["patch"] = patch
    if previous_filename is not None:
        entry["previous_filename"] = previous_filename
    return entry


# ---------- R1: mapping (path / change_type / patch / line_ranges) ----------


def test_list_pr_files_returns_paths_patch_and_line_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core mapping: `GET /pulls/{n}/files` with `per_page=100`, one
    modified file whose `patch` carries a `@@ -10,5 +12,7 @@` header ->
    `path`/`change_type`/`patch` mapped verbatim, `line_ranges` parsed via
    the shared `parse_diff_hunk_ranges` helper. `additions`/`deletions` use
    different values so the assertions can only pass if the mapper reads
    the correct key for each (a key-swap bug would flip them)."""
    patch = "@@ -10,5 +12,7 @@ def foo():\n context\n-old\n+new"
    entry = _file_entry("src/app.py", status="modified", patch=patch, additions=3, deletions=1)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.path == "/repos/acme/backend/pulls/55/files"
        assert req.url.params.get("per_page") == "100"
        return _json([entry])

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")

    from lib_python_projects.providers.base import DiffHunkRange

    assert len(result) == 1
    f = result[0]
    assert f.path == "src/app.py"
    assert f.change_type == "modified"
    assert f.patch == patch
    assert f.additions == 3
    assert f.deletions == 1
    assert f.previous_path is None
    assert f.line_ranges == [
        DiffHunkRange("LEFT", 10, 14),
        DiffHunkRange("RIGHT", 12, 18),
    ]


def test_list_pr_files_paginates_via_link_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-page result (`Link: rel="next"`) is fully concatenated,
    not truncated to the first page."""
    page1_entry = _file_entry("src/a.py")
    page2_entry = _file_entry("src/b.py")

    def handler(req: httpx.Request) -> httpx.Response:
        page = req.url.params.get("page", "1")
        if page == "1":
            return _json(
                [page1_entry],
                headers={
                    "Link": (
                        '<https://api.github.com/repos/acme/backend'
                        '/pulls/55/files?page=2>; rel="next"'
                    )
                },
            )
        if page == "2":
            return _json([page2_entry])
        raise AssertionError(f"unexpected page: {page}")

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")
    assert [f.path for f in result] == ["src/a.py", "src/b.py"]


def test_list_pr_files_page_cap_returns_partial_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hitting the 30-page pagination cap truncates rather than raises —
    this codebase's existing convention (`_find_pending_review`): return
    the files fetched so far and emit one `log.warning` on
    `project-issues.github` mentioning the PR id and 'incomplete'. Page 31
    must never be requested."""

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        if page > 30:
            raise AssertionError(f"page cap must stop before page {page}")
        entries = [_file_entry(f"src/file_{page}_{i}.py") for i in range(100)]
        return _json(
            entries,
            headers={
                "Link": (
                    f'<https://api.github.com/repos/acme/backend/pulls/55'
                    f'/files?page={page + 1}>; rel="next"'
                )
            },
        )

    _install_mock(monkeypatch, handler)
    with caplog.at_level(logging.WARNING, logger="project-issues.github"):
        result = GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")

    assert len(result) == 3000
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"Expected exactly one warning, got {warnings}"
    record = warnings[0]
    assert record.name == "project-issues.github"
    assert "55" in record.message
    assert "incomplete" in record.message.lower()


# ---------- previous_path contract -------------------------------------------


def test_list_pr_files_renamed_file_sets_previous_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _file_entry(
        "new/path.py", status="renamed", previous_filename="old/path.py",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([entry])

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")
    assert result[0].change_type == "renamed"
    assert result[0].previous_path == "old/path.py"


def test_list_pr_files_non_renamed_with_previous_filename_present_leaves_previous_path_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`previous_path` is populated ONLY when `change_type == 'renamed'` —
    a stray `previous_filename` on a non-renamed entry must not leak
    through."""
    entry = _file_entry(
        "src/app.py", status="modified", previous_filename="should/be/ignored.py",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([entry])

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")
    assert result[0].change_type == "modified"
    assert result[0].previous_path is None


# ---------- binary / oversized files ------------------------------------------


def test_list_pr_files_binary_file_has_none_patch_and_empty_line_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binary file carries no `patch` key at all — `patch` stays `None`
    and `line_ranges` is the supported-but-empty `[]`, not `None` (GitHub
    supports positions in general; this file just has none)."""
    entry = _file_entry("assets/logo.png", status="modified", patch=None)

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([entry])

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")
    assert result[0].patch is None
    assert result[0].line_ranges == []


# ---------- errors + empty -----------------------------------------------------


def test_list_pr_files_404_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(github_mod.GitHubError) as exc:
        GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")
    assert exc.value.status == 404
    assert "PR 'acme#55' not found" in exc.value.message


def test_list_pr_files_empty_file_list_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    result = GitHubProvider().list_pr_files(_project(), token="t", pr_id="55")
    assert result == []
