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
from lib_python_projects.providers.gitlab import GitLabProvider
from lib_python_projects.providers.base import RelationNotFound


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
            _project(), "t", "5", "blocks", "#7"
        )
    assert exc.value.kind == "blocks"
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
