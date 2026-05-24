"""Tests for the Azure DevOps provider's pull-request surface.

Covers:
- `list_prs` status / branch filter translation + repo-id resolution cache
- `get_pr` + top-level thread comments (with thread-without-context shape)
- `create_pr` body marker + draft toggle + reviewers
- `update_pr` status/title/body/reviewers
- `merge_pr` merge-method mapping
- `add_pr_comment` thread-without-context creation
- `add_pr_review_comment` diff-anchored thread + reply path
- `submit_pr_review` reviewer-vote mapping
- `list_pr_review_comments` thread-with-context filtering
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops as azure_mod
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsError,
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)
from lib_python_projects.providers.base import PRFilters


REPO_ID = "da0d7da0-6a8c-4958-aad3-be17cbf806eb"


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path="seredos/azure-tests/azure-tests",
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
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    _cache_clear_all()


def _pr_payload(pr_id: int, **overrides) -> dict:
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
        "lastMergeSourceCommit": {"commitId": "abc"},
        "lastMergeTargetCommit": {"commitId": "def"},
        "creationDate": "2026-05-18T10:00:00Z",
        "repository": {"name": "azure-tests"},
    }
    base.update(overrides)
    return base


def _repos_response() -> httpx.Response:
    return _json({
        "value": [
            {
                "id": REPO_ID,
                "name": "azure-tests",
                "defaultBranch": "refs/heads/main",
            },
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "name": "azure-tests2",
                "defaultBranch": "refs/heads/main",
            },
        ]
    })


def _repos_handler(req: httpx.Request) -> httpx.Response | None:
    """Shared handler shard for repository listing — the call PRs depend on."""
    if req.url.path.endswith("/_apis/git/repositories"):
        return _repos_response()
    return None


def _labels_handler(
    req: httpx.Request, labels: list[str] | None = None
) -> httpx.Response | None:
    """Shared shard for the PR-labels endpoint that `get_pr`/`merge_pr`
    now fetch separately. Returns `[]` by default so tests that don't
    care about labels don't need to bother with the payload."""
    if (
        "/_apis/git/repositories/" in req.url.path
        and req.url.path.endswith("/labels")
        and req.method == "GET"
    ):
        names = labels or []
        return _json({
            "value": [{"name": n, "active": True} for n in names]
        })
    return None


# ---------- list_prs ---------------------------------------------------------


def test_list_prs_open_translates_status(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            captured.append(req.url.params.get("searchCriteria.status", ""))
            return _json({"value": [_pr_payload(1), _pr_payload(2)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    prs, _ = AzureDevOpsProvider().list_prs(
        _project(), token="t", filters=PRFilters(status="open", limit=30)
    )
    assert [p.id for p in prs] == ["1", "2"]
    assert captured == ["active"]


def test_list_prs_translates_head_and_base_to_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            captured["source"] = req.url.params.get("searchCriteria.sourceRefName")
            captured["target"] = req.url.params.get("searchCriteria.targetRefName")
            return _json({"value": []})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().list_prs(
        _project(),
        token="t",
        filters=PRFilters(head="feat/x", base="develop"),
    )
    assert captured["source"] == "refs/heads/feat/x"
    assert captured["target"] == "refs/heads/develop"


def test_list_prs_filters_by_label_client_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            return _json({
                "value": [
                    _pr_payload(1, labels=[{"name": "bug"}]),
                    _pr_payload(2, labels=[{"name": "other"}]),
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    prs, _ = AzureDevOpsProvider().list_prs(
        _project(),
        token="t",
        filters=PRFilters(labels=["bug"]),
    )
    assert [p.id for p in prs] == ["1"]


def test_repo_id_cache_hits_after_first_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_list_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal repo_list_calls
        if req.url.path.endswith("/_apis/git/repositories"):
            repo_list_calls += 1
            return _repos_response()
        if "/pullrequests" in req.url.path:
            return _json({"value": []})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    p = AzureDevOpsProvider()
    for _ in range(3):
        p.list_prs(_project(), token="t", filters=PRFilters(limit=1))
    assert repo_list_calls == 1


# ---------- get_pr -----------------------------------------------------------


def test_get_pr_lists_top_level_thread_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        path = req.url.path
        if path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7))
        if path.endswith("/pullrequests/7/threads"):
            return _json({
                "value": [
                    # Top-level discussion thread (no threadContext).
                    {
                        "id": 1,
                        "threadContext": None,
                        "comments": [
                            {
                                "id": 1,
                                "parentCommentId": 0,
                                "author": {"displayName": "Alice"},
                                "content": "<p>looks good</p>",
                                "commentType": "text",
                                "publishedDate": "2026-05-18T10:00:00Z",
                            }
                        ],
                    },
                    # Diff-anchored thread — excluded from top-level list.
                    {
                        "id": 2,
                        "threadContext": {
                            "filePath": "/a.py",
                            "rightFileStart": {"line": 5},
                        },
                        "comments": [
                            {
                                "id": 1,
                                "parentCommentId": 0,
                                "content": "<p>inline</p>",
                                "commentType": "text",
                            }
                        ],
                    },
                ]
            })
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    pr, comments = AzureDevOpsProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.id == "7"
    assert [c.body for c in comments] == ["looks good"]


def test_list_pr_review_comments_only_anchored_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/pullrequests/7/threads"):
            return _json({
                "value": [
                    {
                        "id": 1, "threadContext": None,
                        "comments": [
                            {"id": 1, "content": "<p>ignored</p>", "commentType": "text"}
                        ],
                    },
                    {
                        "id": 2,
                        "threadContext": {
                            "filePath": "/a.py",
                            "rightFileStart": {"line": 5},
                        },
                        "comments": [
                            {
                                "id": 1,
                                "parentCommentId": 0,
                                "author": {"displayName": "Reviewer"},
                                "content": "<p>fix here</p>",
                                "commentType": "text",
                            }
                        ],
                    },
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    rcs = AzureDevOpsProvider().list_pr_review_comments(
        _project(), token="t", pr_id="7"
    )
    assert len(rcs) == 1
    rc = rcs[0]
    assert rc.path == "/a.py"
    assert rc.line == 5
    assert rc.side == "RIGHT"


# ---------- create_pr -------------------------------------------------------


def test_create_pr_emits_refs_and_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    applied_labels: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if req.method == "POST" and path.endswith("/pullrequests"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json(_pr_payload(11))
        # Auto-applied ai-generated label
        if req.method == "POST" and path.endswith("/pullrequests/11/labels"):
            applied_labels.append(json.loads(req.content.decode("utf-8"))["name"])
            return _json({})
        # Labels endpoint now fetched separately in get_pr.
        labels = _labels_handler(req, labels=applied_labels)
        if labels is not None:
            return labels
        # The follow-up get_pr that read labels back
        if req.method == "GET" and path.endswith("/pullrequests/11"):
            return _json(_pr_payload(11))
        if req.method == "GET" and path.endswith("/pullrequests/11/threads"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    pr = AzureDevOpsProvider().create_pr(
        _project(),
        token="t",
        title="hello",
        body="Body",
        head="feat/x",
        base="main",
        draft=True,
    )
    assert pr.id == "11"
    body = captured["body"]
    assert body["sourceRefName"] == "refs/heads/feat/x"
    assert body["targetRefName"] == "refs/heads/main"
    assert body["isDraft"] is True
    assert "#ai-generated" in body["description"]
    assert "ai-generated" in applied_labels
    assert "ai-generated" in pr.labels


# ---------- update_pr -------------------------------------------------------


def test_update_pr_status_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing a PR via our generic `status='closed'` maps to ADO 'abandoned'."""
    captured: dict = {}
    applied_labels: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        # Auto-applied ai-modified label when the existing PR lacks
        # the ai-generated marker.
        if req.method == "POST" and path.endswith("/pullrequests/7/labels"):
            applied_labels.append(json.loads(req.content.decode("utf-8"))["name"])
            return _json({})
        labels = _labels_handler(req, labels=applied_labels)
        if labels is not None:
            return labels
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json(_pr_payload(7, status="abandoned"))
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7, status="abandoned"))
        if path.endswith("/threads"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_pr(_project(), token="t", pr_id="7", status="closed")
    assert captured["body"]["status"] == "abandoned"
    assert "ai-modified" in applied_labels


# ---------- merge_pr --------------------------------------------------------


@pytest.mark.parametrize("ours,theirs", [
    ("merge", "noFastForward"),
    ("squash", "squash"),
    ("rebase", "rebase"),
])
def test_merge_pr_method_mapping(
    monkeypatch: pytest.MonkeyPatch, ours: str, theirs: str,
    fast_merge_settle: None,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            # First GET (before PATCH) returns the open PR; subsequent
            # GETs in the settle-loop return the merged state.
            if "patched" in captured:
                return _json(
                    _pr_payload(7, status="completed", mergeStatus="succeeded")
                )
            return _json(_pr_payload(7, mergeStatus="notSet"))
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            captured["patched"] = True
            # ADO returns the pre-merge snapshot synchronously.
            return _json(
                _pr_payload(7, status="active", mergeStatus="queued")
            )
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().merge_pr(
        _project(), token="t", pr_id="7", merge_method=ours
    )
    assert captured["body"]["status"] == "completed"
    assert captured["body"]["completionOptions"]["mergeStrategy"] == theirs


@pytest.fixture
def fast_merge_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the settle-loop's sleeps so tests run in ~milliseconds."""
    monkeypatch.setattr(
        azure_mod.AzureDevOpsProvider,
        "_MERGE_SETTLE_DELAYS_MS",
        (0, 0, 0, 0, 0, 0),
    )


def test_merge_pr_squash_policy_override_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
    fast_merge_settle: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defect 4: when ADO branch policy overrides a squash merge strategy,
    merge_pr must emit a log.warning identifying both the requested and
    the actual strategy.
    """
    import logging

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            if "patched" in captured:
                # Settle-loop response: strategy was overridden by branch policy
                # to "noFastForward" even though we requested "squash".
                return _json(_pr_payload(
                    7,
                    status="completed",
                    mergeStatus="succeeded",
                    completionOptions={"mergeStrategy": "noFastForward"},
                ))
            return _json(_pr_payload(7, mergeStatus="notSet"))
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            captured["patched"] = True
            return _json(_pr_payload(7, status="active", mergeStatus="queued"))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    with caplog.at_level(logging.WARNING, logger="project-issues.azuredevops"):
        AzureDevOpsProvider().merge_pr(
            _project(), token="t", pr_id="7", merge_method="squash"
        )

    # A warning must have been emitted mentioning both strategies.
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_texts, "Expected a warning about strategy override, got none"
    combined = " ".join(warning_texts)
    assert "squash" in combined, f"Expected 'squash' in warning: {combined!r}"
    assert "noFastForward" in combined or "branch policy" in combined, (
        f"Expected override context in warning: {combined!r}"
    )


# ---------- add_pr_comment + review comments -------------------------------


def test_add_pr_comment_creates_thread_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.method == "POST" and req.url.path.endswith("/pullrequests/7/threads"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({
                "id": 42,
                "threadContext": None,
                "comments": [
                    {
                        "id": 1,
                        "author": {"displayName": "AI"},
                        "content": captured["body"]["comments"][0]["content"],
                        "commentType": "text",
                        "publishedDate": "2026-05-18T10:00:00Z",
                    }
                ],
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    comment = AzureDevOpsProvider().add_pr_comment(
        _project(), token="t", pr_id="7", body="LGTM"
    )
    body = captured["body"]
    assert "threadContext" not in body
    assert body["comments"][0]["parentCommentId"] == 0
    assert "#ai-generated" in body["comments"][0]["content"]
    # Top-of-thread comments expose the bare thread id, matching the
    # GitHub `id == discussion_id` invariant.
    assert comment.id == "42"


def _items_handler_for_file(line_count: int = 50):
    """Return an items-endpoint stub that resolves the file and reports
    a `line_count`-line file content. Used by review-comment tests so
    `_validate_pr_diff_line` doesn't reject the line as out-of-range.
    """
    body = "\n".join([f"line{i}" for i in range(1, line_count + 1)])

    def stub(req: httpx.Request) -> httpx.Response | None:
        if "/_apis/git/repositories/" in req.url.path and req.url.path.endswith(
            "/items"
        ):
            return _json({"content": body})
        return None

    return stub


def test_add_pr_review_comment_anchored_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    item_stub = _items_handler_for_file(line_count=50)

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        items_resp = item_stub(req)
        if items_resp is not None:
            return items_resp
        if req.method == "POST" and req.url.path.endswith("/pullrequests/7/threads"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({
                "id": 99,
                "threadContext": captured["body"]["threadContext"],
                "comments": [
                    {
                        "id": 1,
                        "parentCommentId": 0,
                        "author": {"displayName": "AI"},
                        "content": captured["body"]["comments"][0]["content"],
                        "commentType": "text",
                        "publishedDate": "2026-05-18T10:00:00Z",
                    }
                ],
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    rc = AzureDevOpsProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7",
        body="please fix", path="/file.py", line=12, side="RIGHT",
        commit_sha="abc",
    )
    ctx = captured["body"]["threadContext"]
    assert ctx["filePath"] == "/file.py"
    assert ctx["rightFileStart"]["line"] == 12
    assert "leftFileStart" not in ctx
    assert rc.path == "/file.py"
    assert rc.line == 12
    assert rc.side == "RIGHT"


def test_add_pr_review_comment_left_side(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    item_stub = _items_handler_for_file(line_count=50)

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        items_resp = item_stub(req)
        if items_resp is not None:
            return items_resp
        if req.method == "POST" and req.url.path.endswith("/pullrequests/7/threads"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({
                "id": 99,
                "threadContext": captured["body"]["threadContext"],
                "comments": [
                    {"id": 1, "parentCommentId": 0, "content": "<p>x</p>", "commentType": "text"}
                ],
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7",
        body="x", path="/file.py", line=12, side="LEFT",
        commit_sha="def",
    )
    ctx = captured["body"]["threadContext"]
    assert "leftFileStart" in ctx
    assert "rightFileStart" not in ctx


def test_add_pr_review_comment_rejects_without_anchor_or_reply() -> None:
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="x"
        )
    assert "in_reply_to" in str(exc.value) or "path" in str(exc.value)


def test_submit_pr_review_vote_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/connectionData"):
            return _json({
                "authenticatedUser": {
                    "id": "user-guid",
                    "displayName": "Me",
                }
            })
        if req.method == "PUT" and "/reviewers/user-guid" in path:
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({"id": "user-guid", "vote": captured["body"]["vote"]})
        if req.method == "POST" and path.endswith("/threads"):
            return _json({
                "id": 1,
                "comments": [
                    {"id": 1, "content": "<p>x</p>", "commentType": "text"}
                ],
            })
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="approve", body="lgtm"
    )
    assert captured["body"]["vote"] == 10


# ---------- post-#40 bug-fix coverage ---------------------------------------


def test_merge_pr_conflicts_raises_classified_error(
    monkeypatch: pytest.MonkeyPatch, fast_merge_settle: None
) -> None:
    """A PR with conflicts after the PATCH must surface as 409, not
    leak through as the snapshot's `merged=false`."""
    state: dict = {"patched": False}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            if state["patched"]:
                return _json(_pr_payload(7, mergeStatus="conflicts"))
            return _json(_pr_payload(7, mergeStatus="notSet"))
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            state["patched"] = True
            return _json(_pr_payload(7, mergeStatus="queued", status="completed"))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().merge_pr(
            _project(), token="t", pr_id="7", merge_method="merge"
        )
    assert exc.value.status == 409
    assert "conflicts" in str(exc.value).lower()


def test_merge_pr_stuck_in_progress_raises_202(
    monkeypatch: pytest.MonkeyPatch, fast_merge_settle: None
) -> None:
    """If the settle-loop expires with mergeStatus still non-terminal,
    raise 202 so the agent re-fetches rather than treating the
    snapshot as merged=false."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7, mergeStatus="queued"))
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7, mergeStatus="queued", status="completed"))
        raise AssertionError

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().merge_pr(
            _project(), token="t", pr_id="7", merge_method="merge"
        )
    assert exc.value.status == 202
    assert "in progress" in str(exc.value).lower()


def test_merge_pr_already_merged_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial GET shows status=completed + mergeStatus=succeeded →
    raise AzureDevOpsError(405, '... already merged') before the PATCH."""
    patch_called = []

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(
                7, status="completed", mergeStatus="succeeded"
            ))
        if req.method == "PATCH":
            patch_called.append(True)
            return _json(_pr_payload(7))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().merge_pr(
            _project(), token="t", pr_id="7", merge_method="merge"
        )
    assert exc.value.status == 405
    assert "already merged" in exc.value.message
    # PATCH must never be issued.
    assert not patch_called


def test_add_pr_review_comment_rejects_line_outside_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line beyond the file's last line is rejected with 422, matching
    GitHub's behavior. ADO would otherwise silently anchor the thread
    to a non-existent position."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if "/_apis/git/repositories/" in path and path.endswith("/items"):
            # 10-line file
            return _json({"content": "\n".join(f"l{i}" for i in range(1, 11))})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7",
            body="oops", path="/short.py", line=9999, side="RIGHT",
            commit_sha="abc",
        )
    assert exc.value.status == 422
    assert "outside" in str(exc.value).lower()


def test_add_pr_review_comment_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the path isn't present at the PR head commit, reject."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if "/_apis/git/repositories/" in path and path.endswith("/items"):
            return _json({"message": "not found"}, status_code=404)
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    from lib_python_projects.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7",
            body="oops", path="/missing.py", line=1, side="RIGHT",
            commit_sha="abc",
        )
    assert exc.value.status == 422
    assert "missing.py" in str(exc.value)


def test_review_comment_id_is_bare_thread_for_top_of_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-of-thread review comments use the bare thread id, matching
    GitHub's `id == discussion_id` invariant. Replies keep the
    composite form for cross-comment uniqueness."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/pullrequests/7/threads"):
            return _json({
                "value": [
                    {
                        "id": 50,
                        "threadContext": {
                            "filePath": "/a.py",
                            "rightFileStart": {"line": 1},
                        },
                        "comments": [
                            {
                                "id": 1, "parentCommentId": 0,
                                "author": {"displayName": "X"},
                                "content": "<p>top</p>",
                                "commentType": "text",
                            },
                            {
                                "id": 2, "parentCommentId": 1,
                                "author": {"displayName": "Y"},
                                "content": "<p>reply</p>",
                                "commentType": "text",
                            },
                        ],
                    },
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    rcs = AzureDevOpsProvider().list_pr_review_comments(
        _project(), token="t", pr_id="7"
    )
    assert len(rcs) == 2
    top = next(c for c in rcs if c.body.strip() == "top")
    reply = next(c for c in rcs if c.body.strip() == "reply")
    assert top.id == "50"
    assert top.discussion_id == "50"
    assert reply.id == "50.2"
    assert reply.discussion_id == "50"
    assert reply.in_reply_to == "50"


def test_in_reply_to_accepts_composite_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`add_pr_review_comment(in_reply_to="50.1")` must address thread 50,
    not crash. Older callers may round-trip the legacy composite id."""
    addressed_thread: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if req.method == "POST" and "/threads/50/comments" in path:
            addressed_thread["thread"] = 50
            return _json({"id": 2, "parentCommentId": 1, "content": "<p>r</p>"})
        if req.method == "GET" and path.endswith("/threads/50"):
            return _json({
                "id": 50,
                "threadContext": {
                    "filePath": "/a.py", "rightFileStart": {"line": 1},
                },
                "comments": [],
            })
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7", body="r", in_reply_to="50.1",
    )
    assert addressed_thread["thread"] == 50


def test_pr_head_repo_full_name_is_three_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`head.repo_full_name` matches the project.path 3-segment shape so
    cross-provider consumers don't see a bare repo name on Azure."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        if req.url.path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7))
        if req.url.path.endswith("/pullrequests/7/threads"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    pr, _ = AzureDevOpsProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.head["repo_full_name"] == "seredos/azure-tests/azure-tests"


def test_submit_pr_review_tags_body_thread_with_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body posts as a thread with the `projectIssues.kind=review_body`
    property so it doesn't leak into `get_pr().comments[]`."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/connectionData"):
            return _json({
                "authenticatedUser": {
                    "id": "user-guid",
                    "displayName": "Alice Builder",
                    "uniqueName": "alice@example.com",
                }
            })
        if req.method == "PUT" and "/reviewers/user-guid" in path:
            return _json({"id": "user-guid", "vote": 10})
        if req.method == "POST" and path.endswith("/threads"):
            captured["thread"] = json.loads(req.content.decode("utf-8"))
            return _json({
                "id": 77,
                "comments": [
                    {"id": 1, "content": "<p>x</p>", "commentType": "text"}
                ],
            })
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    review = AzureDevOpsProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="approve", body="lgtm",
    )
    thread = captured["thread"]
    # Property must carry the review-body marker so downstream
    # filtering hides it from get_pr().comments[].
    props = thread.get("properties") or {}
    assert "projectIssues.kind" in props
    val = props["projectIssues.kind"]
    assert (val.get("$value") if isinstance(val, dict) else val) == "review_body"
    # Body got the #ai-generated prefix.
    assert "#ai-generated" in thread["comments"][0]["content"]
    # Review surface fields.
    # author uses displayName to match _map_pr / _map_thread_comment on
    # Azure (consistent identity shape across all PR surfaces).
    assert review.author == "Alice Builder"
    assert review.author != "user-guid"  # never the bare GUID
    assert review.submitted_at  # not empty
    assert review.id != "user-guid"  # synthesized, not the reviewer GUID
    assert ":10:" in review.id  # vote-encoded


def test_get_pr_filters_review_body_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threads tagged as review-bodies must not appear in get_pr().comments[]."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7))
        if req.method == "GET" and path.endswith("/pullrequests/7/threads"):
            return _json({
                "value": [
                    {
                        "id": 1,
                        "threadContext": None,
                        "comments": [{"id": 1, "content": "<p>plain</p>", "commentType": "text"}],
                    },
                    {
                        "id": 2,
                        "threadContext": None,
                        "properties": {
                            "projectIssues.kind": {
                                "$type": "System.String",
                                "$value": "review_body",
                            },
                        },
                        "comments": [{"id": 1, "content": "<p>review-body</p>", "commentType": "text"}],
                    },
                    {
                        # Flat-property shape (ADO returns either form).
                        "id": 3,
                        "threadContext": None,
                        "properties": {"projectIssues.kind": "review_body"},
                        "comments": [{"id": 1, "content": "<p>review-body-flat</p>", "commentType": "text"}],
                    },
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    _, comments = AzureDevOpsProvider().get_pr(_project(), token="t", pr_id="7")
    bodies = [c.body for c in comments]
    assert any("plain" in b for b in bodies)
    assert not any("review-body" in b for b in bodies)


def test_update_pr_synthesizes_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO's PR resource has no real `updated_at`; after a write we
    overlay the current UTC so downstream tooling can sort PRs by
    last touch."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        path = req.url.path
        if req.method == "POST" and path.endswith("/pullrequests/7/labels"):
            return _json({})
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7, title="new"))
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7, title="new"))
        if path.endswith("/threads"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    pr = AzureDevOpsProvider().update_pr(
        _project(), token="t", pr_id="7", title="new"
    )
    # creationDate was 2026-05-18; updated_at must be newer.
    assert pr.updated_at > pr.created_at


# ---------- Round 2 bug-fix coverage ----------------------------------------


def test_merge_pr_waits_for_both_status_and_merge_to_settle(
    monkeypatch: pytest.MonkeyPatch, fast_merge_settle: None
) -> None:
    """The PATCH response shows `status=active, mergeStatus=queued`; the
    settle-loop must wait until BOTH transition (succeeded + completed)
    before returning, otherwise `_map_pr` derives merged=false from
    the half-finished snapshot."""
    state: dict = {"poll": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req)
        if labels is not None:
            return labels
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            state["poll"] += 1
            # First poll: mergeStatus succeeded but status still active
            # (mid-async). Second poll: both settled.
            if state["poll"] <= 1:
                return _json(
                    _pr_payload(7, status="active", mergeStatus="succeeded")
                )
            return _json(
                _pr_payload(7, status="completed", mergeStatus="succeeded")
            )
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7, status="active", mergeStatus="queued"))
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    pr = AzureDevOpsProvider().merge_pr(
        _project(), token="t", pr_id="7", merge_method="merge"
    )
    assert pr.merged is True
    assert pr.status == "merged"


def test_get_pr_fetches_labels_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO's single-PR GET doesn't include labels. Provider must call
    the labels endpoint so the PR returned by get_pr/create_pr/update_pr/
    merge_pr advertises labels consistently with list_prs."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        labels = _labels_handler(req, labels=["ai-generated", "shipit"])
        if labels is not None:
            return labels
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            # Note: NO labels field — single GET doesn't include them.
            return _json(_pr_payload(7))
        if path.endswith("/threads"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    pr, _ = AzureDevOpsProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.labels == ["ai-generated", "shipit"]


def test_get_pr_labels_endpoint_403_does_not_kill_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 on the labels endpoint is best-effort: surface empty
    labels rather than killing the legitimate get_pr."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if "/labels" in path and req.method == "GET":
            return _json({"message": "forbidden"}, status_code=403)
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7))
        if path.endswith("/threads"):
            return _json({"value": []})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    pr, _ = AzureDevOpsProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.labels == []


def test_submit_pr_review_author_never_falls_through_to_guid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connectionData payload carries the GUID + displayName. The
    reviewer PUT response is the second identity source. Whatever
    happens, the author must never fall through to the bare GUID."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/connectionData"):
            return _json({
                "authenticatedUser": {
                    "id": "guid-only",
                    "displayName": "Arne von Appen",
                }
            })
        if req.method == "PUT" and "/reviewers/guid-only" in path:
            return _json({"id": "guid-only", "vote": 10})
        if req.method == "POST" and path.endswith("/threads"):
            return _json({"id": 1, "comments": [{"id": 1, "content": "<p>x</p>"}]})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    review = AzureDevOpsProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="approve", body="lgtm"
    )
    # author uses displayName, matching the _map_pr / _map_thread_comment
    # convention so the same user looks the same across all Azure
    # PR-related surfaces.
    assert review.author == "Arne von Appen"
    assert review.author != "guid-only"


def test_submit_pr_review_body_includes_ai_marker_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned Review.body carries the #ai-generated prefix that
    the docs promise — matching the GitHub provider."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/connectionData"):
            return _json({"authenticatedUser": {"id": "u1", "mailAddress": "u1@x"}})
        if req.method == "PUT" and "/reviewers/" in path:
            return _json({"id": "u1", "vote": 10})
        if req.method == "POST" and path.endswith("/threads"):
            return _json({"id": 1, "comments": [{"id": 1, "content": "<p>x</p>"}]})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    review = AzureDevOpsProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="approve", body="looks good",
    )
    assert review.body.startswith("#ai-generated")
    assert "looks good" in review.body


def test_submit_pr_review_author_via_reviewer_put_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When connectionData returns only the GUID, the reviewer PUT
    response carries the human-readable identity. The merged identity
    must surface the displayName so the author field is consistent
    with the PR-level / comment-level author shape on Azure."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/connectionData"):
            # Slim payload: ADO sometimes returns just `id` + descriptor.
            return _json({"authenticatedUser": {"id": "guid-only"}})
        if req.method == "PUT" and "/reviewers/guid-only" in path:
            # The PUT response carries the rich identity.
            return _json({
                "id": "guid-only",
                "displayName": "Arne von Appen",
                "uniqueName": "arne@example.com",
                "vote": 10,
            })
        if req.method == "POST" and path.endswith("/threads"):
            return _json({"id": 1, "comments": [{"id": 1, "content": "<p>x</p>"}]})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    review = AzureDevOpsProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="approve", body="lgtm",
    )
    # The merged identity surfaces displayName from the PUT response;
    # the bare GUID from connectionData never bleeds through.
    assert review.author == "Arne von Appen"
    assert "guid-only" not in review.author


def test_add_pr_review_comment_echoes_caller_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub echoes the `commit_id` the caller supplied; Azure must
    mirror that on the ReviewComment return rather than the bogus
    None we got when the thread payload didn't carry a SHA."""
    item_stub = _items_handler_for_file(line_count=50)

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        items_resp = item_stub(req)
        if items_resp is not None:
            return items_resp
        if req.method == "POST" and req.url.path.endswith("/pullrequests/7/threads"):
            return _json({
                "id": 100,
                "threadContext": json.loads(req.content.decode("utf-8"))["threadContext"],
                "comments": [
                    {"id": 1, "parentCommentId": 0, "content": "<p>r</p>", "commentType": "text"}
                ],
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    rc = AzureDevOpsProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7",
        body="check this", path="/file.py", line=5, side="RIGHT",
        commit_sha="abc1234",
    )
    assert rc.commit_sha == "abc1234"


def test_review_comment_original_line_and_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_map_thread_comment_for_review` was returning original_line=None
    and a junk offset for commit_sha. Now original_line falls back to
    the current line for fresh threads, and commit_sha is None when
    the thread doesn't carry a SHA marker rather than fabricated."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/pullrequests/7/threads"):
            return _json({
                "value": [
                    {
                        "id": 99,
                        "threadContext": {
                            "filePath": "/a.py",
                            "rightFileStart": {"line": 5},
                            "rightFileEnd": {"line": 5},
                        },
                        "pullRequestThreadContext": {
                            "trackingCriteria": {
                                "origRightFileStart": {"line": 3},
                            },
                        },
                        "comments": [
                            {
                                "id": 1,
                                "parentCommentId": 0,
                                "author": {"displayName": "Alice"},
                                "content": "<p>fix this</p>",
                                "commentType": "text",
                            }
                        ],
                    },
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    rcs = AzureDevOpsProvider().list_pr_review_comments(
        _project(), token="t", pr_id="7"
    )
    assert len(rcs) == 1
    rc = rcs[0]
    # original_line comes from the tracking origRightFileStart.line.
    assert rc.original_line == 3
    # commit_sha is None when no SHA-shaped value is available — not
    # the bogus character-offset string the old code returned.
    assert rc.commit_sha is None or isinstance(rc.commit_sha, str)
    # And critically, never an integer offset.
    assert rc.commit_sha != 1


# ---------- list_prs closed status -------------------------------------------


def test_list_prs_closed_includes_abandoned_and_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status='closed' must return abandoned PRs (status='closed') and
    completed+mergeStatus=succeeded PRs (status='merged')."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            return _json({
                "value": [
                    _pr_payload(10, status="abandoned"),
                    _pr_payload(11, status="completed", mergeStatus="succeeded"),
                ]
            })
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    prs, _ = AzureDevOpsProvider().list_prs(
        _project(), token="t", filters=PRFilters(status="closed", limit=30)
    )
    assert {p.id for p in prs} == {"10", "11"}
    by_id = {p.id: p for p in prs}
    assert by_id["10"].status == "closed"
    assert by_id["11"].status == "merged"


def test_list_prs_closed_excludes_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status='closed' must not include active PRs even though ADO returns
    them when we request searchCriteria.status=all."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            return _json({
                "value": [
                    _pr_payload(20, status="active"),
                    _pr_payload(21, status="abandoned"),
                ]
            })
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    prs, _ = AzureDevOpsProvider().list_prs(
        _project(), token="t", filters=PRFilters(status="closed", limit=30)
    )
    assert [p.id for p in prs] == ["21"]


# ---------- has_more boundary regression (ticket #39) -------------------------


def test_list_prs_has_more_true_when_raw_api_count_equals_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression #39: has_more is True when the raw ADO API response
    contains at least `limit` items (measured before client-side filtering)."""
    limit = 3
    payloads = [_pr_payload(i) for i in range(1, limit + 1)]

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            return _json({"value": payloads})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    prs, has_more = AzureDevOpsProvider().list_prs(
        _project(), token="t", filters=PRFilters(status="open", limit=limit)
    )
    assert len(prs) == limit
    assert has_more is True, "has_more must be True when API returns exactly limit items"


def test_list_prs_has_more_false_when_raw_api_count_below_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression #39: has_more is False when the raw ADO API response
    contains fewer than `limit` items."""
    limit = 10

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            # Return only 2 items when limit is 10 — partial page.
            return _json({"value": [_pr_payload(1), _pr_payload(2)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    prs, has_more = AzureDevOpsProvider().list_prs(
        _project(), token="t", filters=PRFilters(status="open", limit=limit)
    )
    assert len(prs) == 2
    assert has_more is False, "has_more must be False when API returns fewer than limit items"


# ---------- Case 1: submit_pr_review on merged PR → human-readable error -----


def test_submit_pr_review_on_merged_pr_raises_human_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submit_pr_review on a merged/completed PR returns TF401181 from ADO.
    The provider must surface a human-readable AzureDevOpsError, not the raw
    Microsoft error code."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        # connectionData — identity resolution.
        if "/_apis/connectionData" in req.url.path:
            return _json({"authenticatedUser": {"id": "user-guid-123"}})
        # Reviewer PUT → TF401181 (merged PR cannot be edited).
        if "/reviewers/" in req.url.path and req.method == "PUT":
            return _json(
                {"message": "TF401181: The pull request cannot be edited because its status is not 'Active'."},
                status_code=400,
            )
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().submit_pr_review(
            _project(), token="t", pr_id="7", state="approve",
        )
    assert exc.value.status == 400
    assert "merged" in exc.value.message or "completed" in exc.value.message
    # Must NOT expose the raw TF401181 code in the user-facing message.
    assert "TF401181" not in exc.value.message


def test_submit_pr_review_409_tf401181_raises_human_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submit_pr_review TF401181 delivered as 409 also gets normalized."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/_apis/connectionData" in req.url.path:
            return _json({"authenticatedUser": {"id": "user-guid-123"}})
        if "/reviewers/" in req.url.path and req.method == "PUT":
            return _json(
                {"message": "TF401181: cannot be edited"},
                status_code=409,
            )
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().submit_pr_review(
            _project(), token="t", pr_id="7", state="approve",
        )
    assert exc.value.status == 409
    assert "TF401181" not in exc.value.message
    assert "merged" in exc.value.message or "completed" in exc.value.message


# ---------- Case 2: pre-flight validation errors have status 400 not 0 -------


def test_get_comment_missing_ticket_id_raises_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_comment without ticket_id must raise AzureDevOpsError with status 400."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().get_comment(
            _project(), token="t", comment_id="1", ticket_id=None,
        )
    assert exc.value.status == 400
    assert exc.value.status != 0


def test_update_comment_missing_ticket_id_raises_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_comment without ticket_id must raise AzureDevOpsError with status 400."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().update_comment(
            _project(), token="t", comment_id="1", body="test", ticket_id=None,
        )
    assert exc.value.status == 400
    assert exc.value.status != 0


# ---------- ticket #30: return-shape None vs "" fixes -------------------------


def test_map_thread_comment_populates_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_map_thread_comment must populate updated_at from lastUpdatedDate."""

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/_apis/git/repositories" in req.url.path and "/pullrequests/" not in req.url.path:
            return _repos_response()
        if "/pullrequests/1" in req.url.path and "/threads" not in req.url.path:
            return _json({"value": [_pr_payload(1)]}) if "pullrequests?" in req.url.path else _json(_pr_payload(1))
        if "/threads" in req.url.path:
            # Single top-level-discussion thread (no threadContext).
            return _json({"value": [{
                "id": 100,
                "threadContext": None,
                "isDeleted": False,
                "comments": [{
                    "id": 1,
                    "content": "<p>nice work</p>",
                    "author": {"displayName": "Alice"},
                    "publishedDate": "2026-05-20T10:00:00Z",
                    "lastUpdatedDate": "2026-05-21T14:00:00Z",
                }],
                "status": "active",
            }]})
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    _pr, comments = AzureDevOpsProvider().get_pr(_project(), token="t", pr_id="1")
    # The comment from the non-review thread should carry updated_at.
    assert len(comments) >= 1
    pr_comments = [c for c in comments if hasattr(c, "updated_at")]
    assert pr_comments[0].updated_at == "2026-05-21T14:00:00Z"
