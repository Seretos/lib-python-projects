"""Tests for `GitLabProvider.get_ticket` relation extraction.

Covers:
- issue_links → relates_to / blocks / blocked_by mapping
- closed_by MRs (auto-close after merge)
- outgoing body scans: closes / fixes / resolves → `closes`
- duplicate-of body scan → `duplicate_of`
- plain `#N` references → `mentions` (filtered against close/duplicate
  sets and self-reference)
- PROJECT_ISSUES_MENTIONS_SCAN_DEPTH controls comment-body scanning
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_mod
from lib_python_projects.providers.gitlab import GitLabError, GitLabProvider
from lib_python_projects.providers.base import (
    RelationAlreadyExists,
    RelationKindUnsupported,
    RelationNotFound,
)


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="gitlab", path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
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
        headers = {"Accept": "application/json", "User-Agent": "test"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(
            base_url=gitlab_mod._base_url(project),
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _issue_with_body(iid: int, body: str = "") -> dict:
    return {
        "iid": iid,
        "title": f"Issue {iid}",
        "description": body,
        "state": "opened",
        "author": {"username": "alice"},
        "assignees": [],
        "labels": [],
        "web_url": f"https://gitlab.com/acme/backend/-/issues/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }


# ---------- helpers ----------------------------------------------------------


def _kinds(relations: list) -> list[tuple[str, str]]:
    """(kind, ticket_id) pairs, sorted for deterministic comparison."""
    return sorted([(r.kind, r.ticket_id) for r in relations])


# ---------- issue links ------------------------------------------------------


def test_relations_from_issue_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each `link_type` → corresponding `RelationKind`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5))
        if "/issues/5/notes" in url:
            return _json([])
        if "/issues/5/links" in url:
            return _json([
                {
                    "iid": 10, "link_type": "blocks",
                    "title": "blocked issue",
                    "web_url": "https://gitlab.com/acme/backend/-/issues/10",
                    "state": "opened",
                    "references": {"relative": "#10"},
                },
                {
                    "iid": 11, "link_type": "is_blocked_by",
                    "title": "blocking issue",
                    "web_url": "https://gitlab.com/acme/backend/-/issues/11",
                    "state": "opened",
                    "references": {"relative": "#11"},
                },
                {
                    "iid": 12, "link_type": "relates_to",
                    "title": "related",
                    "web_url": "https://gitlab.com/acme/backend/-/issues/12",
                    "state": "closed",
                    "references": {"relative": "#12"},
                },
            ])
        if "/issues/5/closed_by" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    pairs = _kinds(relations)
    assert ("blocks", "#10") in pairs
    assert ("blocked_by", "#11") in pairs
    assert ("relates_to", "#12") in pairs


def test_relations_unknown_link_type_falls_back_to_relates_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5))
        if "/links" in url:
            return _json([{
                "iid": 99, "link_type": "weird_new_kind",
                "title": "", "web_url": "", "state": "opened",
                "references": {"relative": "#99"},
            }])
        return _json([], 200) if "/notes" in url or "/closed_by" in url else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    pairs = _kinds(relations)
    assert ("relates_to", "#99") in pairs


# ---------- closed_by --------------------------------------------------------


def test_relations_closed_by_from_mrs(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5))
        if "/closed_by" in url:
            return _json([{
                "iid": 50, "title": "fix",
                "web_url": "https://gitlab.com/acme/backend/-/merge_requests/50",
                "state": "merged",
            }])
        return _json([], 200) if ("/notes" in url or "/links" in url) else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    closed = [r for r in relations if r.kind == "closed_by"]
    assert len(closed) == 1
    assert closed[0].ticket_id == "#50"
    assert closed[0].is_pull_request is True
    assert closed[0].state == "merged"


# ---------- body scans -------------------------------------------------------


def _empty_aux_handler(req: httpx.Request) -> httpx.Response | None:
    """Helper: return _json([]) for /notes /links /closed_by; None otherwise."""
    url = str(req.url)
    if "/notes" in url or "/links" in url or "/closed_by" in url:
        return _json([])
    return None


def test_body_scan_closes_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "This implements x.\n\nCloses #42 and fixes #43."

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=body))
        aux = _empty_aux_handler(req)
        return aux if aux else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    pairs = _kinds(relations)
    assert ("closes", "#42") in pairs
    assert ("closes", "#43") in pairs
    # And NOT also as `mentions` — the filter de-duplicates.
    assert ("mentions", "#42") not in pairs


def test_body_scan_resolves_and_implements_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolves` and `implements` also count as closing keywords."""
    body = "resolves #100 / implements #101"

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=body))
        aux = _empty_aux_handler(req)
        return aux if aux else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    closes = sorted(r.ticket_id for r in relations if r.kind == "closes")
    assert closes == ["#100", "#101"]


def test_body_scan_duplicate_of(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Duplicate of #1.\n\nSee also #2."

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=body))
        aux = _empty_aux_handler(req)
        return aux if aux else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    pairs = _kinds(relations)
    assert ("duplicate_of", "#1") in pairs
    # `#2` should appear as a plain mention, not a duplicate.
    assert ("mentions", "#2") in pairs


def test_body_scan_mentions_excludes_self_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `#5` in issue 5's own body must not show up as a mention."""
    body = "This is #5 itself. Also see #6."

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=body))
        aux = _empty_aux_handler(req)
        return aux if aux else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    pairs = _kinds(relations)
    assert ("mentions", "#5") not in pairs
    assert ("mentions", "#6") in pairs


def test_body_scan_cross_project_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`group/proj#N` references are preserved verbatim."""
    body = "see other-group/other-project#42"

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=body))
        aux = _empty_aux_handler(req)
        return aux if aux else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    pairs = _kinds(relations)
    assert ("mentions", "other-group/other-project#42") in pairs


# ---------- comment-body scan depth -----------------------------------------


def test_scan_depth_zero_skips_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default depth=0 → only scan the body, not the comments."""
    monkeypatch.delenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", raising=False)

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=""))
        if "/issues/5/notes" in url:
            return _json([{
                "id": 1, "body": "Closes #99",  # ignored — not body
                "system": False,
                "author": {"username": "a"},
                "created_at": "2024-01-01T00:00:00Z",
            }])
        if "/links" in url or "/closed_by" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    closes = [r for r in relations if r.kind == "closes"]
    assert closes == []


def test_scan_depth_all_includes_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Depth=-1 → scan every comment body."""
    monkeypatch.setenv("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "-1")

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=""))
        if "/issues/5/notes" in url:
            return _json([{
                "id": 1, "body": "Closes #99",
                "system": False,
                "author": {"username": "a"},
                "created_at": "2024-01-01T00:00:00Z",
            }])
        if "/links" in url or "/closed_by" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    closes = [r for r in relations if r.kind == "closes"]
    assert len(closes) == 1
    assert closes[0].ticket_id == "#99"


# ---------- include_relations=False ------------------------------------------


def test_include_relations_false_skips_link_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `include_relations=False` path bypasses _fetch_relations
    entirely — we never hit the /links endpoint."""
    seen_urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(str(req.url))
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5))
        if "/issues/5/notes" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, truncated = GitLabProvider().get_ticket(
        _project(), "t", "5", include_relations=False,
    )
    assert relations == []
    assert truncated is None
    assert not any("/links" in u for u in seen_urls)
    assert not any("/closed_by" in u for u in seen_urls)


# ---------- duplicate_of dedup -----------------------------------------------


def test_duplicate_of_suppresses_relates_to_for_same_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After add_relation(kind="duplicate_of") the body contains 'Duplicate of #1'
    and the issue-links API returns a 'relates_to' link for the same target.
    get_ticket must return exactly one relation for #1 with kind 'duplicate_of',
    and NO 'relates_to' entry for the same target.
    """
    body = "Duplicate of #1"

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=body))
        if "/issues/5/links" in url:
            # The native relates_to link written by _gitlab_mark_duplicate_of.
            return _json([{
                "iid": 1,
                "link_type": "relates_to",
                "title": "Target issue",
                "web_url": "https://gitlab.com/acme/backend/-/issues/1",
                "state": "opened",
                "references": {"relative": "#1"},
            }])
        if "/issues/5/closed_by" in url:
            return _json([])
        if "/issues/5/notes" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    pairs = _kinds(relations)
    # Must have exactly one duplicate_of entry for #1.
    assert ("duplicate_of", "#1") in pairs
    # The spurious relates_to for the same target must be gone.
    assert ("relates_to", "#1") not in pairs
    # Exactly one entry for #1 in total.
    entries_for_1 = [(k, t) for k, t in pairs if t == "#1"]
    assert len(entries_for_1) == 1


# ---------- F10: state normalisation -----------------------------------------


def test_issue_link_state_opened_normalised_to_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab raw `state='opened'` in an issue link must surface as `'open'`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5))
        if "/issues/5/links" in url:
            return _json([{
                "iid": 10, "link_type": "blocks",
                "title": "blocked issue",
                "web_url": "https://gitlab.com/acme/backend/-/issues/10",
                "state": "opened",
                "references": {"relative": "#10"},
            }])
        if "/issues/5/closed_by" in url or "/issues/5/notes" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    blocks_rels = [r for r in relations if r.kind == "blocks"]
    assert len(blocks_rels) == 1
    assert blocks_rels[0].state == "open", (
        f"expected 'open', got {blocks_rels[0].state!r} — raw 'opened' must be normalised"
    )


def test_closing_mr_state_opened_normalised_to_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab raw `state='opened'` in a closing MR must surface as `'open'`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5))
        if "/closed_by" in url:
            return _json([{
                "iid": 50, "title": "open MR",
                "web_url": "https://gitlab.com/acme/backend/-/merge_requests/50",
                "state": "opened",
            }])
        return _json([], 200) if ("/notes" in url or "/links" in url) else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    closed_by = [r for r in relations if r.kind == "closed_by"]
    assert len(closed_by) == 1
    assert closed_by[0].state == "open", (
        f"expected 'open', got {closed_by[0].state!r} — raw 'opened' must be normalised"
    )


# ---------- F4: remove_relation raises RelationNotFound ----------------------


def test_remove_relation_link_not_found_raises_relation_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_relation raises RelationNotFound (a LookupError) when link absent."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/issues/5/links" in url:
            return _json([])  # no links
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationNotFound) as exc:
        GitLabProvider().remove_relation(
            _project(), "t", "5", "relates_to", "#7"
        )
    assert exc.value.kind == "relates_to"
    assert isinstance(exc.value, LookupError)


# ---------- F6: resolved field -----------------------------------------------


def test_issue_link_relations_have_resolved_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue-links API relations carry `resolved=True`."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5))
        if "/issues/5/links" in url:
            return _json([{
                "iid": 10, "link_type": "blocks",
                "title": "blocked issue",
                "web_url": "https://gitlab.com/acme/backend/-/issues/10",
                "state": "opened",
                "references": {"relative": "#10"},
            }])
        if "/issues/5/closed_by" in url or "/issues/5/notes" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    link_rels = [r for r in relations if r.kind == "blocks"]
    assert link_rels
    assert link_rels[0].resolved is True


def test_body_scan_mentions_have_resolved_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body-scan `mentions` carry `resolved=False`."""
    body = "see #42 for details"

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=body))
        aux = _empty_aux_handler(req)
        return aux if aux else _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    mentions = [r for r in relations if r.kind == "mentions"]
    assert mentions
    assert mentions[0].resolved is False


# ---------- blocks / blocked_by unsupported (ticket #20) --------------------


def test_blocks_relation_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_relation with 'blocks' must raise RelationKindUnsupported — no HTTP
    call needed because the guard fires before any I/o."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationKindUnsupported) as exc:
        GitLabProvider().add_relation(_project(), "t", "5", "blocks", "#2")
    assert exc.value.kind == "blocks"


def test_blocked_by_relation_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_relation with 'blocked_by' must raise RelationKindUnsupported."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationKindUnsupported) as exc:
        GitLabProvider().add_relation(_project(), "t", "5", "blocked_by", "#3")
    assert exc.value.kind == "blocked_by"


def test_supported_relation_kinds_excludes_blocks_and_blocked_by() -> None:
    """_SUPPORTED_RELATION_KINDS must not advertise blocks or blocked_by."""
    kinds = GitLabProvider._SUPPORTED_RELATION_KINDS
    assert "blocks" not in kinds
    assert "blocked_by" not in kinds


def test_remove_relation_blocks_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_relation with 'blocks' must raise RelationKindUnsupported — no
    HTTP call needed because the guard fires before any I/O."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationKindUnsupported) as exc:
        GitLabProvider().remove_relation(_project(), "t", "5", "blocks", "#2")
    assert exc.value.kind == "blocks"


def test_remove_relation_blocked_by_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_relation with 'blocked_by' must raise RelationKindUnsupported."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationKindUnsupported) as exc:
        GitLabProvider().remove_relation(_project(), "t", "5", "blocked_by", "#3")
    assert exc.value.kind == "blocked_by"


# ---------- Case 3: add_relation already-assigned 409 normalization ----------


def test_add_relation_relates_to_already_assigned_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation 'relates_to' hitting a 409 'Issue(s) already assigned'
    must raise RelationAlreadyExists with kind and target info."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        # Numeric project-id resolution.
        if url.endswith("/projects/acme%2Fbackend"):
            return _json({"id": 42})
        # Issue links POST → already-assigned 409.
        if "/issues/5/links" in url and req.method == "POST":
            return _json(
                {"message": "Issue(s) already assigned"},
                status_code=409,
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(RelationAlreadyExists) as exc:
        GitLabProvider().add_relation(_project(), "t", "5", "relates_to", "#7")
    assert exc.value.kind == "relates_to"
    assert exc.value.ticket_id == "5"
    assert "#7" in exc.value.target
    # Must be a ValueError subclass for _safe wrapper compatibility.
    assert isinstance(exc.value, ValueError)


def test_add_relation_self_relation_gitlab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation with ticket_id == target must raise ValueError with
    'self-relation' in the message — no HTTP call should be made."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for self-relation: {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="self-relation"):
        GitLabProvider().add_relation(_project(), "t", "5", "relates_to", "#5")


# ---------- R1: remove_relation duplicate_of strips body line ----------------


def test_remove_relation_duplicate_of_strips_body_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_relation(duplicate_of) must strip the 'Duplicate of #7' line
    from the issue body on the PUT call; other body content is preserved."""
    captured_put: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        # GET /links — return the link so delete works.
        if req.method == "GET" and "/issues/5/links" in url:
            return _json([{
                "iid": 7,
                "issue_link_id": 100,
                "link_type": "relates_to",
                "title": "target",
                "web_url": "https://gitlab.com/acme/backend/-/issues/7",
                "state": "opened",
            }])
        # DELETE /links/100 — succeed.
        if req.method == "DELETE" and "/links/100" in url:
            return _json({})
        # GET /issues/5 — return body with dup line.
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "Duplicate of #7\n\nsome content",
                "labels": [],
                "state": "closed",
            })
        # PUT /issues/5 — capture payload.
        if req.method == "PUT" and url.endswith("/issues/5"):
            captured_put["body"] = json.loads(req.content.decode())
            return _json({"iid": 5, "state": "opened"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = GitLabProvider().remove_relation(_project(), "t", "5", "duplicate_of", "#7")

    assert result == {"removed": True}
    desc = captured_put["body"]["description"]
    assert "Duplicate of #7" not in desc
    assert "some content" in desc
    assert captured_put["body"]["state_event"] == "reopen"


def test_remove_relation_duplicate_of_body_only_dup_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When body is only the dup line, after removal the body is the AI marker only."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and "/issues/5/links" in url:
            return _json([{
                "iid": 7, "issue_link_id": 200,
                "link_type": "relates_to", "title": "", "web_url": "", "state": "opened",
            }])
        if req.method == "DELETE" and "/links/200" in url:
            return _json({})
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "Duplicate of #7",
                "labels": [],
                "state": "closed",
            })
        if req.method == "PUT" and url.endswith("/issues/5"):
            captured_put["body"] = json.loads(req.content.decode())
            return _json({"iid": 5, "state": "opened"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    captured_put: dict = {}
    _install_mock(monkeypatch, handler)
    GitLabProvider().remove_relation(_project(), "t", "5", "duplicate_of", "#7")

    desc = captured_put["body"]["description"]
    assert "Duplicate of #7" not in desc
    # Body must at minimum have the AI marker prefix.
    assert "#ai-" in desc


def test_remove_relation_duplicate_of_preserves_ai_generated_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI-generated label on source → body keeps #ai-generated prefix."""
    captured_put: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and "/issues/5/links" in url:
            return _json([{
                "iid": 7, "issue_link_id": 300,
                "link_type": "relates_to", "title": "", "web_url": "", "state": "opened",
            }])
        if req.method == "DELETE" and "/links/300" in url:
            return _json({})
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "#ai-generated\n\nDuplicate of #7\n\nreal body",
                "labels": ["ai-generated"],
                "state": "closed",
            })
        if req.method == "PUT" and url.endswith("/issues/5"):
            captured_put["body"] = json.loads(req.content.decode())
            return _json({"iid": 5, "state": "opened"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    GitLabProvider().remove_relation(_project(), "t", "5", "duplicate_of", "#7")

    desc = captured_put["body"]["description"]
    assert "Duplicate of #7" not in desc
    assert desc.startswith("#ai-generated")
    assert "real body" in desc


def test_remove_relation_duplicate_of_leaves_other_dup_lines_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the 'Duplicate of #7' line is stripped; 'Duplicate of #8' stays."""
    captured_put: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and "/issues/5/links" in url:
            return _json([{
                "iid": 7, "issue_link_id": 400,
                "link_type": "relates_to", "title": "", "web_url": "", "state": "opened",
            }])
        if req.method == "DELETE" and "/links/400" in url:
            return _json({})
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "Duplicate of #7\n\nDuplicate of #8\n\nbody text",
                "labels": [],
                "state": "closed",
            })
        if req.method == "PUT" and url.endswith("/issues/5"):
            captured_put["body"] = json.loads(req.content.decode())
            return _json({"iid": 5, "state": "opened"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    GitLabProvider().remove_relation(_project(), "t", "5", "duplicate_of", "#7")

    desc = captured_put["body"]["description"]
    assert "Duplicate of #7" not in desc
    assert "Duplicate of #8" in desc
    assert "body text" in desc


def test_remove_relation_duplicate_of_roundtrip_no_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After remove_relation, get_ticket on the updated body returns no duplicate_of."""
    # Simulate the state after removal: body has had dup line stripped.
    stripped_body = "some content"

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("issues/5"):
            return _json(_issue_with_body(5, body=stripped_body))
        if "/issues/5/links" in url:
            return _json([])
        if "/issues/5/closed_by" in url or "/issues/5/notes" in url:
            return _json([])
        return _json([], 404)

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(_project(), "t", "5")
    dup_rels = [r for r in relations if r.kind == "duplicate_of"]
    assert dup_rels == [], (
        "After dup line is stripped from body, get_ticket must return no duplicate_of"
    )


# ---------- R2: add_relation duplicate_of when relates_to link already exists --


def test_add_relation_duplicate_of_when_relates_to_link_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the relates_to link already exists (RelationAlreadyExists from
    _gitlab_post_issue_link), _gitlab_mark_duplicate_of must NOT raise —
    it must fall through to body+close and return a Relation."""
    captured_put: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        # Numeric project-id resolution.
        if url.endswith("/projects/acme%2Fbackend"):
            return _json({"id": 42})
        # GET source issue.
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "original body",
                "labels": [],
                "state": "opened",
                "title": "Source",
                "web_url": "https://gitlab.com/acme/backend/-/issues/5",
            })
        # POST /links → 409 (already exists).
        if req.method == "POST" and "/issues/5/links" in url:
            return _json(
                {"message": "Issue(s) already assigned"},
                status_code=409,
            )
        # GET target issue (for the 409-path Relation synthesis).
        if req.method == "GET" and url.endswith("/issues/7"):
            return _json({
                "iid": 7,
                "title": "Target",
                "web_url": "https://gitlab.com/acme/backend/-/issues/7",
                "state": "opened",
            })
        # PUT /issues/5 (body + close).
        if req.method == "PUT" and url.endswith("/issues/5"):
            captured_put["body"] = json.loads(req.content.decode())
            return _json({
                "iid": 5, "state": "closed",
                "description": captured_put["body"].get("description", ""),
            })
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    # Must not raise, even though the relates_to link already exists.
    relation = GitLabProvider().add_relation(_project(), "t", "5", "duplicate_of", "#7")

    assert relation.kind == "duplicate_of"
    assert relation.ticket_id == "#7"
    # PUT must have been called with state_event=close.
    assert captured_put["body"]["state_event"] == "close"
    # Body must contain the "Duplicate of #7" annotation.
    assert "Duplicate of #7" in captured_put["body"]["description"]


def test_add_relation_duplicate_of_non_409_gitlab_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-409 GitLabError (e.g. 422 invalid target) from POST /links
    must still propagate — the link failure is not silenced."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/projects/acme%2Fbackend"):
            return _json({"id": 42})
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "",
                "labels": [],
                "state": "opened",
            })
        if req.method == "POST" and "/issues/5/links" in url:
            return _json({"message": "Unprocessable Entity"}, status_code=422)
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().add_relation(_project(), "t", "5", "duplicate_of", "#7")
    assert exc.value.status == 422


# ---------- blocking 3: partial iid match edge case --------------------------


def test_remove_relation_duplicate_of_partial_iid_not_corrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_relation(duplicate_of, '#7') must NOT corrupt a body that
    contains 'Duplicate of #70' — the regex must only match the exact iid.
    Without the (?!\\d) negative-lookahead fix, this body becomes '0\\n...'."""
    captured_put: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        # GET /links — return the #7 link so the delete step works.
        if req.method == "GET" and "/issues/5/links" in url:
            return _json([{
                "iid": 7,
                "issue_link_id": 500,
                "link_type": "relates_to",
                "title": "target",
                "web_url": "https://gitlab.com/acme/backend/-/issues/7",
                "state": "opened",
            }])
        # DELETE /links/500 — succeed.
        if req.method == "DELETE" and "/links/500" in url:
            return _json({})
        # GET /issues/5 — body has 'Duplicate of #70' (NOT #7).
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "Duplicate of #70\n\nsome content",
                "labels": [],
                "state": "closed",
            })
        # PUT /issues/5 — capture payload.
        if req.method == "PUT" and url.endswith("/issues/5"):
            captured_put["body"] = json.loads(req.content.decode())
            return _json({"iid": 5, "state": "opened"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    GitLabProvider().remove_relation(_project(), "t", "5", "duplicate_of", "#7")

    desc = captured_put["body"]["description"]
    # The #70 line must be preserved intact — not truncated to "0\n...".
    assert "Duplicate of #70" in desc, (
        f"'Duplicate of #70' was corrupted by partial iid match: {desc!r}"
    )
    assert "some content" in desc
    # There must be no stray "0\n" fragment from the partial match.
    assert desc.lstrip("#ai-generated\n").lstrip() != "0" and "0\n" not in desc.replace(
        "Duplicate of #70", ""
    )


# ---------- blocking 4: state normalization on the 409 path ------------------


def test_add_relation_duplicate_of_409_path_state_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the relates_to link already exists (409 path), the returned
    Relation.state must be 'open' (normalised) not 'opened' (raw GitLab)."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/projects/acme%2Fbackend"):
            return _json({"id": 42})
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "original body",
                "labels": [],
                "state": "opened",
                "title": "Source",
                "web_url": "https://gitlab.com/acme/backend/-/issues/5",
            })
        if req.method == "POST" and "/issues/5/links" in url:
            return _json(
                {"message": "Issue(s) already assigned"},
                status_code=409,
            )
        # GET target issue — return raw 'opened' state.
        if req.method == "GET" and url.endswith("/issues/7"):
            return _json({
                "iid": 7,
                "title": "Target",
                "web_url": "https://gitlab.com/acme/backend/-/issues/7",
                "state": "opened",
            })
        if req.method == "PUT" and url.endswith("/issues/5"):
            return _json({
                "iid": 5, "state": "closed",
                "description": "",
            })
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    relation = GitLabProvider().add_relation(_project(), "t", "5", "duplicate_of", "#7")

    assert relation.state == "open", (
        f"expected 'open' (normalised), got {relation.state!r} — "
        "raw 'opened' from GitLab must be normalised on the 409 synthesis path"
    )


# ---------- blocking 5: resolved=True on the 409 synthesis path ---------------


def test_add_relation_duplicate_of_409_path_resolved_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the relates_to link already exists (409 path), the synthesised
    Relation must carry resolved=True — consistent with the success path."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/projects/acme%2Fbackend"):
            return _json({"id": 42})
        if req.method == "GET" and url.endswith("/issues/5"):
            return _json({
                "iid": 5,
                "description": "original body",
                "labels": [],
                "state": "opened",
                "title": "Source",
                "web_url": "https://gitlab.com/acme/backend/-/issues/5",
            })
        if req.method == "POST" and "/issues/5/links" in url:
            return _json(
                {"message": "Issue(s) already assigned"},
                status_code=409,
            )
        if req.method == "GET" and url.endswith("/issues/7"):
            return _json({
                "iid": 7,
                "title": "Target",
                "web_url": "https://gitlab.com/acme/backend/-/issues/7",
                "state": "opened",
            })
        if req.method == "PUT" and url.endswith("/issues/5"):
            return _json({
                "iid": 5, "state": "closed",
                "description": "",
            })
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    relation = GitLabProvider().add_relation(_project(), "t", "5", "duplicate_of", "#7")

    assert relation.resolved is True, (
        f"expected resolved=True on the 409 synthesis path, got {relation.resolved!r}"
    )
