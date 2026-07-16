"""GitHub provider — REST v3 implementation."""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
from typing import Any

import httpx

from lib_python_projects.models import ProjectConfig
from lib_python_projects.markers import (
    apply_body_marker,
    has_ai_generated_marker,
    label_color,
    label_description,
    ensure_body_prefix,
    ensure_comment_prefix,
    MarkerSet,
    strip_leading_ai_marker,
)
from lib_python_projects.providers.base import (
    BoardColumnSpec,
    BulkTicketResult,
    Comment,
    DiscoveredProject,
    FailingJob,
    FailureAnnotation,
    FieldSpec,
    Label,
    normalize_timestamp,
    PipelineFailure,
    PipelineRun,
    PRFilters,
    ProjectDiscoveryResult,
    ProviderError,
    PullRequest,
    RateLimitError,
    Relation,
    RelationAlreadyExists,
    RelationKindUnsupported,
    RelationNotFound,
    Review,
    review_decision_from_states,
    ReviewComment,
    ReviewState,
    Status,
    StatusSpec,
    Ticket,
    TicketFilters,
    TokenCapabilities,
    TokenProjectDiscoveryProvider,
    ViewerIdentity,
    ViewerIdentityProvider,
    _assert_not_self_relation,
    _extract_parent_id,
    _validate_label_lists,
    _validate_limit,
)
from lib_python_projects.providers._http_cache import make_cached_transport
from lib_python_projects.providers import _idempotency

log = logging.getLogger("project-issues.github")

USER_AGENT = "claude-code-project-issues-plugin/0.1.0"
ACCEPT = "application/vnd.github+json"
API_BASE = "https://api.github.com"

# Sentinel distinguishing "milestone kwarg not passed" from "explicitly
# clear the milestone" (`milestone=None`) on `create_ticket` /
# `update_ticket` (ticket #151).
_UNSET: Any = object()


_GITHUB_SUFFIX_RE = re.compile(r"\s*\(GitHub\s+\d+\)\s*$")


def _marker_set(project: ProjectConfig) -> MarkerSet:
    return MarkerSet(project.auto_labels.ai_generated, project.auto_labels.ai_modified)


class GitHubError(ProviderError):
    def __init__(self, status: int, message: str):
        # Strip any trailing "(GitHub NNN)" parenthetical to avoid the message
        # ending up as "GitHub 404: … (GitHub 404)" when errors are re-wrapped.
        cleaned = _GITHUB_SUFFIX_RE.sub("", message)
        RuntimeError.__init__(self, f"GitHub {status}: {cleaned}")
        self.status = status
        self.message = cleaned


class PartialTicketCreateError(GitHubError):
    """`create_ticket(custom_fields=...)` created the REST issue but the
    Projects v2 board write (add-to-board or a field update) failed
    partway through (ticket #131).

    The issue itself is NOT rolled back: deleting it needs elevated
    GraphQL rights and is destructive/irreversible, so this is a
    deliberate "enriched exception, no deletion" design — callers get
    `issue_number` / `issue_url` / `issue_node_id` as structured
    attributes (not just baked into the message string) so a retrying
    caller can dedupe against the already-created issue instead of
    creating a duplicate. Subclasses `GitHubError` so every existing
    `except GitHubError` call site keeps working unchanged.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        issue_number: int | None,
        issue_url: str | None,
        issue_node_id: str | None,
    ):
        super().__init__(status, message)
        self.issue_number = issue_number
        self.issue_url = issue_url
        self.issue_node_id = issue_node_id


def _client(token: str | None) -> httpx.Client:
    headers = {
        "Accept": ACCEPT,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=API_BASE,
        headers=headers,
        timeout=30.0,
        transport=make_cached_transport(),
    )


def _format_github_validation_errors(errs: list) -> str:
    """Turn GitHub's `errors: [...]` array into a human-readable summary.

    Ticket #48 finding 10: previously the array was Python-`repr`'d
    into the error string (single-quoted keys, not valid JSON, awkward
    for agents). This formatter produces a compact one-line summary
    that's still informative.

    Each item generally looks like:
        {"resource": "Issue", "field": "assignees", "code": "invalid",
         "value": "ghost"}
    or carries a free-form `message` field.
    """
    parts: list[str] = []
    for err in errs:
        if not isinstance(err, dict):
            parts.append(str(err))
            continue
        message = err.get("message")
        if message:
            parts.append(str(message))
            continue
        field = err.get("field") or "?"
        resource = err.get("resource") or "?"
        code = err.get("code") or "?"
        value = err.get("value")
        if value is not None:
            parts.append(f"{resource}.{field}={value!r} ({code})")
        else:
            parts.append(f"{resource}.{field} ({code})")
    return "; ".join(parts) if parts else "validation failed"


def _check(resp: httpx.Response) -> None:
    if resp.status_code == 304:
        return
    if resp.is_success:
        return
    try:
        payload = resp.json()
        msg = payload.get("message") or resp.reason_phrase
        errs = payload.get("errors")
        if errs:
            msg = f"{msg}: {_format_github_validation_errors(errs)}"
    except Exception:
        msg = resp.reason_phrase or "request failed"
    if resp.status_code == 429:
        retry_after: int | None = None
        ra_hdr = resp.headers.get("Retry-After")
        if ra_hdr is not None:
            try:
                retry_after = int(ra_hdr)
            except (ValueError, TypeError):
                retry_after = None
        if retry_after is None:
            reset_hdr = resp.headers.get("x-ratelimit-reset")
            if reset_hdr is not None:
                try:
                    retry_after = max(0, int(reset_hdr) - int(time.time()))
                except (ValueError, TypeError):
                    retry_after = None
        raise RateLimitError(429, msg, retry_after=retry_after)
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        reset_hdr = resp.headers.get("x-ratelimit-reset")
        retry_after_403: int | None = None
        if reset_hdr is not None:
            try:
                retry_after_403 = max(0, int(reset_hdr) - int(time.time()))
            except (ValueError, TypeError):
                retry_after_403 = None
        raise RateLimitError(403, msg, retry_after=retry_after_403)
    raise GitHubError(resp.status_code, msg)


def _check_side_step(resp: httpx.Response) -> str | None:
    """Like `_check`, but treats a 422 response as a non-fatal warning.

    Returns ``None`` on success, a warning string on 422, and raises
    ``GitHubError`` (or ``RateLimitError``) for any other failure status —
    identical behaviour to ``_check`` for those cases.
    """
    if resp.status_code == 304:
        return None
    if resp.is_success:
        return None
    try:
        payload = resp.json()
        msg = payload.get("message") or resp.reason_phrase
        errs = payload.get("errors")
        if errs:
            msg = f"{msg}: {_format_github_validation_errors(errs)}"
    except Exception:
        msg = resp.reason_phrase or "request failed"
    if resp.status_code == 422:
        return f"side-step 422: {msg}"
    if resp.status_code == 429:
        retry_after: int | None = None
        ra_hdr = resp.headers.get("Retry-After")
        if ra_hdr is not None:
            try:
                retry_after = int(ra_hdr)
            except (ValueError, TypeError):
                retry_after = None
        if retry_after is None:
            reset_hdr = resp.headers.get("x-ratelimit-reset")
            if reset_hdr is not None:
                try:
                    retry_after = max(0, int(reset_hdr) - int(time.time()))
                except (ValueError, TypeError):
                    retry_after = None
        raise RateLimitError(429, msg, retry_after=retry_after)
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        reset_hdr = resp.headers.get("x-ratelimit-reset")
        retry_after_403: int | None = None
        if reset_hdr is not None:
            try:
                retry_after_403 = max(0, int(reset_hdr) - int(time.time()))
            except (ValueError, TypeError):
                retry_after_403 = None
        raise RateLimitError(403, msg, retry_after=retry_after_403)
    raise GitHubError(resp.status_code, msg)


def _repo_path(project: ProjectConfig) -> str:
    return f"/repos/{project.owner}/{project.repo}"


_LINK_LAST_RE = re.compile(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"')


def _parse_link_last_page(link_header: str) -> int | None:
    """Return the page number from a `Link: rel="last"` entry, or None.

    Used by the tail-fetch path in `list_comments` (ticket #47 follow-up).
    """
    if not link_header:
        return None
    m = _LINK_LAST_RE.search(link_header)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):  # pragma: no cover - regex guards int
        return None


def _map_issue(raw: dict) -> Ticket:
    state = raw.get("state", "open")
    state_reason = raw.get("state_reason")
    # New encoding (see `Status` doc in providers/base.py): `open`,
    # `closed:completed`, or `closed:not_planned`. The `state_reason`
    # is preserved verbatim so the round-trip through
    # `list_ticket_statuses().hints` is lossless.
    if state == "open":
        status: Status = "open"
    elif state_reason == "not_planned":
        status = "closed:not_planned"
    else:
        # GitHub returns `state_reason="completed"` for "completed" and
        # null/unknown for legacy issues — both map to `closed:completed`.
        status = "closed:completed"
    # Labels are sorted alphabetically so callers comparing labels
    # across read tools (`list_tickets` vs `get_ticket`) get a stable
    # order — ticket #49 finding 11.
    label_names = sorted(lbl["name"] for lbl in (raw.get("labels") or []))
    return Ticket(
        id=str(raw["number"]),
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        status=status,
        author=(raw.get("user") or {}).get("login", ""),
        assignees=[a["login"] for a in (raw.get("assignees") or [])],
        labels=label_names,
        url=raw.get("html_url") or "",
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
    )


def _map_graphql_issue_content(content: dict) -> Ticket:
    """`_map_issue`'s counterpart for the GraphQL `Issue` content shape
    used by the Projects-v2 items query (ticket #118).

    GraphQL's `Issue.state` is the uppercase enum `OPEN`/`CLOSED` and
    `stateReason` is `COMPLETED`/`NOT_PLANNED`/`REOPENED` — both are
    normalised down to the same `Status` encoding `_map_issue` produces
    (`open` / `closed:completed` / `closed:not_planned`) so callers see
    one uniform shape regardless of which GitHub API answered the call.
    """
    state = (content.get("state") or "OPEN").upper()
    state_reason = (content.get("stateReason") or "").upper() or None
    if state != "CLOSED":
        status: Status = "open"
    elif state_reason == "NOT_PLANNED":
        status = "closed:not_planned"
    else:
        status = "closed:completed"
    label_names = sorted(
        n["name"] for n in ((content.get("labels") or {}).get("nodes") or [])
    )
    assignees = [
        a["login"] for a in ((content.get("assignees") or {}).get("nodes") or [])
    ]
    return Ticket(
        id=str(content.get("number")),
        title=content.get("title") or "",
        body=content.get("body") or "",
        status=status,
        author=(content.get("author") or {}).get("login") or "",
        assignees=assignees,
        labels=label_names,
        url=content.get("url") or "",
        created_at=normalize_timestamp(content.get("createdAt") or ""),
        updated_at=normalize_timestamp(content.get("updatedAt") or ""),
    )


def _map_comment(raw: dict) -> Comment:
    return Comment(
        id=str(raw["id"]),
        author=(raw.get("user") or {}).get("login", ""),
        body=raw.get("body") or "",
        url=raw.get("html_url") or "",
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
    )


def _map_review_comment(raw: dict) -> ReviewComment:
    """Translate a GitHub `/pulls/{n}/comments` item into `ReviewComment`."""
    in_reply_to = raw.get("in_reply_to_id")
    # discussion_id = thread anchor (= what caller passes to in_reply_to
    # when replying). On GitHub there is no separate discussion entity:
    # the top-of-thread note's id IS the anchor. Reply notes carry
    # `in_reply_to_id` pointing at that anchor; first notes are their
    # own anchor.
    discussion_id = (
        str(in_reply_to) if in_reply_to is not None else str(raw.get("id", ""))
    )
    return ReviewComment(
        id=str(raw.get("id", "")),
        author=(raw.get("user") or {}).get("login", ""),
        body=raw.get("body") or "",
        path=raw.get("path"),
        line=raw.get("line"),
        original_line=raw.get("original_line"),
        side=raw.get("side"),
        commit_sha=raw.get("commit_id") or raw.get("original_commit_id") or None,
        in_reply_to=str(in_reply_to) if in_reply_to is not None else None,
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
        url=raw.get("html_url") or None,
        discussion_id=discussion_id,
    )


_REVIEW_STATE_MAP: dict[str, ReviewState] = {
    "APPROVED": "approve",
    "CHANGES_REQUESTED": "request_changes",
    "COMMENTED": "comment",
    "DISMISSED": "comment",
}


def _map_review(raw: dict) -> Review | None:
    """Translate a GitHub `/pulls/{n}/reviews` item into `Review`.

    Returns `None` for `PENDING` entries (an unsubmitted review still
    being drafted by the reviewer) — callers filter those out so
    `list_pr_reviews` only reports reviews that were actually submitted.
    """
    state = _REVIEW_STATE_MAP.get(raw.get("state") or "")
    if state is None:
        return None
    return Review(
        id=str(raw["id"]),
        state=state,
        author=(raw.get("user") or {}).get("login", ""),
        body=raw.get("body") or "",
        url=raw.get("html_url") or "",
        submitted_at=raw.get("submitted_at") or "",
        commit_sha=raw.get("commit_id"),
    )


def _fetch_pr_reviews(client: httpx.Client, project: ProjectConfig, pr_id: str) -> list[Review]:
    """Fetch and map submitted reviews for a PR via an already-open client.

    Shared by `list_pr_reviews` and `get_pr` so the two paths can't
    diverge. Hits `GET /repos/{o}/{r}/pulls/{n}/reviews`, capped at 100
    per page (the GitHub maximum). `PENDING` reviews (still being
    drafted by the reviewer, not yet submitted) are skipped — see
    `_map_review`.
    """
    r = client.get(
        f"{_repo_path(project)}/pulls/{pr_id}/reviews",
        params={"per_page": 100},
    )
    _check(r)
    reviews = [_map_review(it) for it in r.json()]
    return [rv for rv in reviews if rv is not None]


def _latest_reviews_by_author(reviews: list[Review]) -> list[Review]:
    """Reduce a review list to one entry per author: the most recently
    submitted review. Ties (equal or missing `submitted_at`) fall back to
    list order — the entry appearing later in `reviews` wins.
    """
    latest: dict[str, Review] = {}
    for rv in reviews:
        existing = latest.get(rv.author)
        if existing is None or rv.submitted_at >= existing.submitted_at:
            latest[rv.author] = rv
    return list(latest.values())


def _map_pr(raw: dict) -> PullRequest:
    """Translate a GitHub pull-request payload into a `PullRequest`.

    Handles two payload shapes:
      - `GET /repos/{o}/{r}/pulls/{n}` — full PR object with `head`/`base`,
        `merged`, `mergeable`, `draft`, `requested_reviewers`, etc.
      - `GET /search/issues` items where `pull_request` is a small stub
        and the top-level fields look like an issue. For that shape the
        body/head/base/draft/merged fields aren't all present; we fall
        back to safe defaults so the dataclass is always constructable.
    """
    state = raw.get("state", "open")
    pr_stub = raw.get("pull_request") or {}
    merged = bool(raw.get("merged") or raw.get("merged_at") or pr_stub.get("merged_at"))
    if state == "open":
        status: str = "open"
    elif merged:
        status = "merged"
    else:
        status = "closed"

    # Some payloads (notably `/search/issues` PR results) lack the full
    # `head` / `base` blocks — coerce to safe empty refs so the dataclass
    # is always populated with the documented keys.
    head_raw = raw.get("head") or {}
    base_raw = raw.get("base") or {}
    head_repo = (head_raw.get("repo") or {}) if head_raw else {}
    head = {
        "ref": head_raw.get("ref", "") if head_raw else "",
        "sha": head_raw.get("sha", "") if head_raw else "",
        "repo_full_name": head_repo.get("full_name", "") if head_repo else "",
    }
    base = {
        "ref": base_raw.get("ref", "") if base_raw else "",
        "sha": base_raw.get("sha", "") if base_raw else "",
    }

    number = raw.get("number") or 0
    return PullRequest(
        id=str(number),
        number=int(number),
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        status=status,  # type: ignore[arg-type]
        draft=bool(raw.get("draft", False)),
        author=(raw.get("user") or {}).get("login", ""),
        assignees=[a["login"] for a in (raw.get("assignees") or [])],
        reviewers=[],  # populated by a follow-up /reviews call when needed
        requested_reviewers=[
            r["login"] for r in (raw.get("requested_reviewers") or [])
        ],
        labels=[lbl["name"] for lbl in (raw.get("labels") or [])],
        head=head,
        base=base,
        merged=merged,
        mergeable=raw.get("mergeable"),  # may be None when GitHub hasn't computed
        url=raw.get("html_url") or "",
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
        mergeable_state=raw.get("mergeable_state"),
        merge_commit_sha=raw.get("merge_commit_sha"),
        auto_merge=raw.get("auto_merge"),
    )


def _split_github_status(
    status: Status | None,
) -> tuple[str | None, str | None]:
    """Return (state, state_reason) tuple for a provider-native status.

    Accepted values mirror `list_statuses().values` for GitHub —
    `["open", "closed:completed", "closed:not_planned"]`. The legacy
    bare `"closed"` alias is NO LONGER accepted (ticket #49 finding 5):
    callers that previously got `closed` silently coerced into
    `closed:completed` now get a structured rejection pointing back to
    `list_ticket_statuses`. This keeps the discovery and write surfaces
    consistent.

      - None                  → (None, None)            (no state change)
      - "open"                → ("open", None)
      - "closed:completed"    → ("closed", "completed")
      - "closed:not_planned"  → ("closed", "not_planned")
    """
    if status is None:
        return None, None
    if status == "open":
        return "open", None
    if status == "closed:completed":
        return "closed", "completed"
    if status == "closed:not_planned":
        return "closed", "not_planned"
    raise ValueError(
        f"unsupported status {status!r} for GitHub — "
        f"use list_ticket_statuses to discover valid values. "
        f"Accepted: open, closed:completed, closed:not_planned."
    )


def _github_states_pairs(states: list[str]) -> list[tuple[str, str | None]]:
    """Validate `TicketFilters.states` against the GitHub-native
    vocabulary and return `(api_state, state_reason)` pairs.

    Reuses `_split_github_status` verbatim for validation, so an
    unrecognised value raises the exact same `ValueError` (with the
    "use list_ticket_statuses" hint) that the write paths already raise.
    """
    pairs: list[tuple[str, str | None]] = []
    for value in states:
        state, reason = _split_github_status(value)  # type: ignore[arg-type]
        assert state is not None  # value is never None here
        pairs.append((state, reason))
    return pairs


def _github_coarse_state(pairs: list[tuple[str, str | None]]) -> str:
    """Derive the coarse `open`/`closed`/`all` API state param from the
    validated `states` pairs (GitHub Search / `/issues` can't express
    `state_reason` set-membership directly, only the coarse `state`).
    """
    distinct = {state for state, _ in pairs}
    if distinct == {"open"}:
        return "open"
    if distinct == {"closed"}:
        return "closed"
    return "all"


def _github_item_matches_states(
    item: dict, pairs: list[tuple[str, str | None]],
) -> bool:
    """Client-side predicate: does a raw issue item match any requested
    `states` pair, exact-match on `(state, state_reason)`?
    """
    item_state = item.get("state", "open")
    item_reason = item.get("state_reason")
    for state, reason in pairs:
        if state == "open":
            if item_state == "open":
                return True
        elif item_state == "closed" and item_reason == reason:
            return True
    return False


def _parse_relation_target(
    target: str, project: ProjectConfig,
) -> tuple[str, str]:
    """Parse a relation target into (repo_path, issue_number_as_str).

    Accepts:
      - `"#5"` / `"5"` — same-repo as `project`.
      - `"owner/repo#5"` — cross-repo (raises NotImplementedError for
        now; left here so the surface is stable when we lift the
        restriction).

    Returns the repo prefix in the same shape `_repo_path` produces
    (`"/repos/{owner}/{repo}"`) so it can be slotted directly into URL
    construction.
    """
    raw = target.strip()
    if not raw:
        raise ValueError("relation target is empty")
    if "/" in raw and "#" in raw:
        repo_part, _, num_part = raw.partition("#")
        if "/" not in repo_part or not num_part:
            raise ValueError(
                f"invalid relation target {target!r}: "
                f"expected 'owner/repo#N' or '#N'"
            )
        raise NotImplementedError(
            "cross-repo relation targets are not yet supported"
        )
    num_part = raw.lstrip("#")
    if not num_part.isdigit():
        raise ValueError(
            f"invalid relation target {target!r}: expected '#N' "
            f"(same-repo issue number)"
        )
    return _repo_path(project), num_part


def _fetch_issue_payload(
    client: httpx.Client, repo_path: str, number: str,
) -> dict:
    """GET the issue payload for a number."""
    r = client.get(f"{repo_path}/issues/{number}")
    _check(r)
    return r.json()


def _fetch_issue_internal_id(
    client: httpx.Client, repo_path: str, number: str,
) -> tuple[int, dict]:
    """Return (internal_numeric_id, full_payload) for an issue number.

    The Sub-Issues and Dependencies APIs both take the *internal*
    issue id (the database PK in the response's `id` field), NOT the
    user-facing issue `number`. Callers must therefore GET the issue
    first to translate.
    """
    raw = _fetch_issue_payload(client, repo_path, number)
    internal_id = raw.get("id")
    if not isinstance(internal_id, int):
        raise GitHubError(
            500,
            f"issue payload for {repo_path}/issues/{number} did not "
            f"carry an int `id` field — cannot resolve internal id",
        )
    return internal_id, raw


def _github_post_sub_issue(
    client: httpx.Client,
    parent_repo_path: str,
    parent_issue_number: str,
    *,
    sub_issue_internal_id: int,
    relation_kind_for_caller: str,
    target_raw: dict,
    project: ProjectConfig,
    caller_ticket_id: str,
    caller_target_ref: str,
) -> Relation:
    """POST to the Sub-Issues endpoint and return a `Relation` for the agent.

    `caller_ticket_id` is the *logical* source ticket as seen by the caller
    (may differ from `parent_issue_number` when the wire direction is swapped,
    e.g. for `kind="parent"`).  `caller_target_ref` is the caller's logical
    target reference (e.g. "#7").
    """
    r = client.post(
        f"{parent_repo_path}/issues/{parent_issue_number}/sub_issues",
        json={"sub_issue_id": sub_issue_internal_id},
    )
    try:
        _check(r)
    except GitHubError as exc:
        if exc.status == 422:
            msg_lower = exc.message.lower()
            if "duplicate sub-issue" in msg_lower or "may not contain duplicate" in msg_lower:
                raise RelationAlreadyExists(
                    kind=relation_kind_for_caller,
                    ticket_id=caller_ticket_id,
                    target=caller_target_ref,
                ) from exc
        raise
    return _map_relation_from_sub_issue(
        target_raw, project, relation_kind_for_caller,
    )


def _github_post_dependency(
    client: httpx.Client,
    repo_path: str,
    source_issue_number: str,
    *,
    dep_endpoint: str,
    target_internal_id: int,
    relation_kind_for_caller: str,
    target_raw: dict,
    project: ProjectConfig,
    caller_ticket_id: str,
    caller_target_ref: str,
) -> Relation:
    """POST to the Dependencies endpoint (api 2026-03-10).

    `caller_ticket_id` is the *logical* source ticket as seen by the caller
    (may differ from `source_issue_number` when the wire direction is swapped,
    e.g. for `kind="blocks"`).  `caller_target_ref` is the caller's logical
    target reference (e.g. "#5").
    """
    r = client.post(
        f"{repo_path}/issues/{source_issue_number}/dependencies/{dep_endpoint}",
        json={"issue_id": target_internal_id},
    )
    try:
        _check(r)
    except GitHubError as exc:
        if exc.status == 422:
            msg_lower = exc.message.lower()
            if "cycle" in msg_lower or "circular" in msg_lower:
                raise GitHubError(
                    422,
                    f"relation would create a cycle — kind: {relation_kind_for_caller!r}, target: {caller_target_ref}",
                ) from exc
            if "already" in msg_lower or "duplicate" in msg_lower or "already assigned" in msg_lower:
                raise RelationAlreadyExists(
                    kind=relation_kind_for_caller,
                    ticket_id=caller_ticket_id,
                    target=caller_target_ref,
                ) from exc
        raise
    return _map_relation_from_sub_issue(
        target_raw, project, relation_kind_for_caller,
    )


def _github_assert_dependency_exists(
    client: httpx.Client,
    repo_path: str,
    source_issue_number: str,
    *,
    target_internal_id: int,
    source_ref: str,
    target_ref: str,
    kind: str = "blocked_by",
    ticket_id: str = "",
) -> None:
    """Raise `RelationNotFound` when no `blocked_by` dependency exists.

    Ticket #49 finding 8 / #48 finding 3: GitHub's
    `DELETE /dependencies/blocked_by/{id}` is silently idempotent —
    succeeds whether the link exists or not. The documented contract
    of `remove_relation` says removing a non-existent relation must
    raise, so we read the current dependency list first and raise
    `RelationNotFound` (a `LookupError` subclass) before the DELETE.
    """
    r = client.get(
        f"{repo_path}/issues/{source_issue_number}/dependencies/blocked_by",
    )
    _check(r)
    rows = r.json() or []
    for row in rows:
        # API surface uses the issue's internal id under either key
        # depending on the version — accept both.
        candidate = row.get("id") or row.get("internal_id")
        if isinstance(candidate, int) and candidate == target_internal_id:
            return
    raise RelationNotFound(
        kind=kind,
        ticket_id=ticket_id or source_issue_number,
        target=target_ref,
    )


def _github_dependency_already_exists(
    client: httpx.Client,
    repo_path: str,
    source_issue_number: str,
    *,
    target_internal_id: int,
) -> bool:
    """Return True if `target_internal_id` is already in the blocked_by list.

    GETs `{repo_path}/issues/{source_issue_number}/dependencies/blocked_by`
    and checks whether the target appears in the response.  Used as a
    pre-flight guard to detect inverse-kind duplicates before the POST.
    The existing 422-based guard in `_github_post_dependency` remains as a
    race-condition safety net.
    """
    r = client.get(
        f"{repo_path}/issues/{source_issue_number}/dependencies/blocked_by",
    )
    _check(r)
    rows = r.json() or []
    for row in rows:
        candidate = row.get("id") or row.get("internal_id")
        if isinstance(candidate, int) and candidate == target_internal_id:
            return True
    return False


def _github_sub_issue_already_exists(
    client: httpx.Client,
    repo_path: str,
    parent_number: str,
    *,
    sub_issue_internal_id: int,
) -> bool:
    """Return True if `sub_issue_internal_id` is already in the sub-issues list.

    GETs `{repo_path}/issues/{parent_number}/sub_issues` and checks whether the
    sub-issue appears.  Used as a pre-flight guard to detect inverse-kind
    duplicates before the POST.  The existing 422-based guard in
    `_github_post_sub_issue` remains as a race-condition safety net.
    """
    r = client.get(
        f"{repo_path}/issues/{parent_number}/sub_issues",
    )
    _check(r)
    rows = r.json() or []
    for row in rows:
        candidate = row.get("id") or row.get("internal_id")
        if isinstance(candidate, int) and candidate == sub_issue_internal_id:
            return True
    return False


def _github_mark_duplicate_of(
    client: httpx.Client,
    project: ProjectConfig,
    source_issue_number: str,
    *,
    target_number: str,
    target_raw: dict,
) -> Relation:
    """Mark `source` as duplicate of `target` via body edit + state change.

    GitHub has no native typed `duplicate_of` link surface.  The read
    path (`_fetch_relations`) detects `duplicate_of` solely from a
    ``Duplicate of #N`` line in the ticket body — the authoritative
    source of truth regardless of issue state.  We persist the link by:

      1. GET the source issue to read its current body + labels.
      2. Prepend a ``Duplicate of #N`` line to the body (after the AI
         marker, so `apply_body_marker` keeps the marker correct).
      3. PATCH with state=closed, state_reason="duplicate", body=new.

    Removal (`remove_relation("duplicate_of")`) strips the
    ``Duplicate of #N`` line from the body (and reopens the issue) so
    the read path no longer reports the relation.  Stripping IS the
    intended contract — body history is deliberately not preserved.
    """
    src = _fetch_issue_payload(
        client, _repo_path(project), source_issue_number,
    )
    current_body = src.get("body") or ""
    current_labels = {lbl["name"] for lbl in (src.get("labels") or [])}
    markers = _marker_set(project)
    will_be_ai_generated = project.auto_labels.ai_generated in current_labels

    dup_line = f"Duplicate of #{target_number}"
    # Insert the duplicate-of line AFTER the AI marker (re-stamped) but
    # at the start of the body so it's the first prose the reader
    # sees. apply_body_marker strips any existing leading #ai-* line.
    # Pre-strip marker so we can splice cleanly, then re-stamp via the
    # canonical helper.
    body_without_marker = strip_leading_ai_marker(current_body, markers=markers)
    if dup_line not in body_without_marker:
        if body_without_marker:
            new_body_core = f"{dup_line}\n\n{body_without_marker}"
        else:
            new_body_core = dup_line
    else:
        new_body_core = body_without_marker
    new_body = apply_body_marker(
        new_body_core, will_be_ai_generated=will_be_ai_generated, markers=markers,
    )
    pr = client.patch(
        f"{_repo_path(project)}/issues/{source_issue_number}",
        json={
            "state": "closed",
            "state_reason": "duplicate",
            "body": new_body,
        },
    )
    _check(pr)
    return _map_relation_from_sub_issue(target_raw, project, "duplicate_of")


def _issue_state(raw: dict) -> str:
    """Translate a GitHub issue/PR payload to one of "open"/"closed"/"merged"/""."""
    state = raw.get("state")
    if state == "open":
        return "open"
    if state == "closed":
        # PRs report `merged` separately; if the source was a merged PR,
        # `merged_at` is non-null. Some endpoints also include `pull_request.merged_at`.
        pr_info = raw.get("pull_request") or {}
        if raw.get("merged_at") or pr_info.get("merged_at"):
            return "merged"
        return "closed"
    return ""


def _ref_for(issue_raw: dict, project: ProjectConfig) -> tuple[str, bool]:
    """Build a `ticket_id` string and detect whether the referenced item is a PR.

    Returns ("#N", is_pr) for same-repo refs and ("owner/repo#N", is_pr)
    for cross-repo refs. Falls back to URL parsing when `repository` is
    absent from the payload.
    """
    number = issue_raw.get("number")
    is_pr = bool(issue_raw.get("pull_request"))
    repo = issue_raw.get("repository") or {}
    full_name = repo.get("full_name")
    if not full_name:
        # Older payloads omit `repository`; derive from the html/api url.
        url = issue_raw.get("html_url") or issue_raw.get("url") or ""
        # html_url: https://github.com/owner/repo/(issues|pull)/N
        # api url:  https://api.github.com/repos/owner/repo/issues/N
        parts = url.replace("https://api.github.com/repos/", "").replace(
            "https://github.com/", ""
        ).split("/")
        if len(parts) >= 2:
            full_name = f"{parts[0]}/{parts[1]}"
    same_repo = full_name == f"{project.owner}/{project.repo}"
    if same_repo or not full_name:
        return f"#{number}", is_pr
    return f"{full_name}#{number}", is_pr


def _map_relation_from_sub_issue(
    raw: dict,
    project: ProjectConfig,
    kind: str,
    *,
    resolved: bool | None = True,
) -> Relation:
    """Map a sub-issue (or the issue's own `parent` field) into a Relation.

    `resolved=True` (default) for API-fetched relations. Pass `resolved=False`
    for body-scan / text-inferred relations where the target was not fetched.
    """
    ticket_id, is_pr = _ref_for(raw, project)
    return Relation(
        kind=kind,
        ticket_id=ticket_id,
        title=raw.get("title") or "",
        url=raw.get("html_url") or "",
        state=_issue_state(raw),
        is_pull_request=is_pr,
        resolved=resolved,
    )


def _map_relation_from_timeline(
    event: dict, project: ProjectConfig, *, self_id: str
) -> Relation | None:
    """Map a GitHub timeline event to a Relation, or None if not relevant.

    Handles three timeline event types:
      - cross-referenced (`mentions` / `mentioned_by`)
      - connected (`closes` / `closed_by`)
      - marked_as_duplicate (`duplicate_of` / `duplicated_by`)
    """
    etype = event.get("event")
    if etype == "cross-referenced":
        source = (event.get("source") or {}).get("issue")
        if not source:
            return None
        # The cross-reference direction: GitHub records the event on the
        # side that was mentioned; `source.issue` is the OTHER side that
        # did the mentioning. So from the current ticket's POV we were
        # `mentioned_by` source.
        return _map_relation_from_sub_issue(source, project, "mentioned_by")
    if etype == "connected" or etype == "disconnected":
        # `connected`: another issue/PR linked itself as closing this one.
        # The `source.issue` is the closer. Direction: we are `closed_by` it.
        source = (event.get("source") or {}).get("issue")
        if not source:
            return None
        if etype == "disconnected":
            # A previously-connected ref was removed; skip it.
            return None
        return _map_relation_from_sub_issue(source, project, "closed_by")
    if etype == "marked_as_duplicate":
        # Recorded on both sides. `source.issue` is the OTHER side; the
        # event itself doesn't disclose which side is canonical, so we
        # report `duplicate_of` from this ticket's POV when the source
        # is the canonical, and `duplicated_by` when the source is the dup.
        # The GitHub payload exposes `source.type` == "issue" plus
        # `event.actor` etc, but not the direction explicitly. Convention:
        # the side that was MARKED as duplicate gets `duplicate_of` →
        # source.issue. The canonical side gets `duplicated_by` → source.
        # The `event` payload's `source.issue.state` helps but isn't
        # reliable; we use the convention that the timeline of the
        # "duplicate" issue contains a marked_as_duplicate event whose
        # `dupe.issue` is THIS issue. GitHub returns either a `dupe` or
        # `canonical` field on the event itself.
        dupe = event.get("dupe") or {}
        canonical = event.get("canonical") or {}
        # Pull whichever side is NOT self.
        dupe_id = str(dupe.get("number", "")) if dupe else ""
        canonical_id = str(canonical.get("number", "")) if canonical else ""
        if canonical and canonical_id != self_id:
            return _map_relation_from_sub_issue(canonical, project, "duplicate_of")
        if dupe and dupe_id != self_id:
            return _map_relation_from_sub_issue(dupe, project, "duplicated_by")
        # Fall back to source.issue if `dupe`/`canonical` are absent.
        source = (event.get("source") or {}).get("issue")
        if source:
            return _map_relation_from_sub_issue(source, project, "duplicate_of")
        return None
    return None


def _has_next_link(link_header: str | None) -> bool:
    """Detect whether an HTTP `Link` header advertises a `rel="next"` page."""
    if not link_header:
        return False
    # The header is comma-separated; each entry looks like:
    #   <https://api.github.com/...?page=2>; rel="next"
    for part in link_header.split(","):
        if 'rel="next"' in part.replace("'", '"'):
            return True
    return False


def _next_link_url(link_header: str | None) -> str | None:
    """Extract the URL of the ``rel="next"`` page from an HTTP ``Link`` header.

    Returns ``None`` when the header is absent or contains no ``rel="next"``
    entry.  The URL is the bare string between ``<`` and ``>`` in the entry.
    """
    if not _has_next_link(link_header):
        return None
    for part in link_header.split(","):  # type: ignore[union-attr]
        normalised = part.replace("'", '"')
        if 'rel="next"' in normalised:
            # Each part looks like: <https://...>; rel="next"
            start = normalised.find("<")
            end = normalised.find(">")
            if start != -1 and end != -1 and end > start:
                return normalised[start + 1 : end].strip()
    return None


# ---------- ref / closing-keyword scanning ---------------------------------

_CLOSING_KEYWORDS = ("close", "closes", "closed", "fix", "fixes", "fixed",
                     "resolve", "resolves", "resolved")
_REF_RE = re.compile(
    r"(?:^|(?<=[\s,;:.!?(\[]))"
    r"(?P<full>(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._\-]+))?#(?P<num>\d+)"
)
_CLOSING_RE = re.compile(
    r"\b(?P<kw>" + "|".join(_CLOSING_KEYWORDS) + r")\b\s*:?\s*"
    r"(?P<full>(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._\-]+))?#(?P<num>\d+)",
    re.IGNORECASE,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)


def _strip_noise(text: str) -> str:
    """Remove fenced code blocks and HTML comments before scanning for refs."""
    if not text:
        return ""
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _FENCED_RE.sub(" ", text)
    return text


def _scan_refs(text: str, *, closing_only: bool) -> list[tuple[str | None, str | None, int]]:
    """Return a list of (owner, repo, number) tuples from `text`.

    `owner` and `repo` are `None` for same-repo refs (`#N`). When
    `closing_only` is True, only refs preceded by a closing keyword
    (`closes`/`fixes`/`resolves` and tense variants) are returned.
    """
    cleaned = _strip_noise(text or "")
    if not cleaned:
        return []
    out: list[tuple[str | None, str | None, int]] = []
    seen: set[tuple[str | None, str | None, int]] = set()
    pattern = _CLOSING_RE if closing_only else _REF_RE
    for m in pattern.finditer(cleaned):
        owner = m.group("owner") if m.group("full") else None
        repo = m.group("repo") if m.group("full") else None
        try:
            num = int(m.group("num"))
        except (TypeError, ValueError):
            continue
        key = (owner, repo, num)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _mentions_scan_depth() -> int:
    """Read the env-configurable mentions scan depth.

    `PROJECT_ISSUES_MENTIONS_SCAN_DEPTH` semantics (D2 follow-up on #5):
      - unset / -1  -> -1 (scan body + ALL comments; full pagination)
      - 0           ->  0 (scan body only)
      - N > 0       ->  N (scan body + first N comments)
    Invalid values fall back to -1.
    """
    raw = os.environ.get("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH")
    if raw is None or raw == "":
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _ref_to_relation(
    owner: str | None,
    repo: str | None,
    num: int,
    project: ProjectConfig,
    kind: str,
) -> Relation:
    """Build a bare Relation from a parsed `#N` / `owner/repo#N` reference."""
    if owner and repo:
        full = f"{owner}/{repo}"
        if full == f"{project.owner}/{project.repo}":
            ticket_id = f"#{num}"
            url = f"https://github.com/{project.owner}/{project.repo}/issues/{num}"
        else:
            ticket_id = f"{full}#{num}"
            url = f"https://github.com/{full}/issues/{num}"
    else:
        ticket_id = f"#{num}"
        url = f"https://github.com/{project.owner}/{project.repo}/issues/{num}"
    # title="", state="", resolved=False is the intentional "not fetched"
    # sentinel for all body-scan-derived relations (including duplicate_of).
    # The target is not independently fetched at read time; callers that need
    # live data must resolve the relation themselves.
    return Relation(
        kind=kind,
        ticket_id=ticket_id,
        title="",
        url=url,
        state="",
        is_pull_request=False,
        resolved=False,
    )


def _fetch_duplicate_of_relation(
    client: httpx.Client,
    project: ProjectConfig,
    owner: str | None,
    repo: str | None,
    num: int,
) -> Relation:
    """Build a fully-hydrated `duplicate_of` Relation via a live GET.

    Unlike `closes`/`mentions` (which stay thin via `_ref_to_relation`),
    `duplicate_of` is meant to read the same way `parent`/`child` do:
    `resolved=True` plus the target's real `title`/`state`. This does the
    extra REST fetch of the target issue and maps it through the same
    `_map_relation_from_sub_issue` reference-implementation helper parent/
    child use.

    Degrades gracefully to the thin `_ref_to_relation` sentinel (same
    `ticket_id`/`url`, so `_dedupe_relations` still matches it) on any
    fetch failure — 404/410 "not found", other GitHub API errors, or a
    transport-level failure — rather than raising and breaking the whole
    `get_ticket` call over one dangling reference.
    """
    if owner and repo:
        repo_path = f"/repos/{owner}/{repo}"
    else:
        repo_path = _repo_path(project)
    try:
        raw = _fetch_issue_payload(client, repo_path, str(num))
    except (ProviderError, httpx.HTTPError, OSError):
        return _ref_to_relation(owner, repo, num, project, "duplicate_of")
    return _map_relation_from_sub_issue(raw, project, "duplicate_of")


# ---------- GraphQL helpers ------------------------------------------------


_CONVERT_DRAFT_MUTATION = (
    "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id})"
    "{pullRequest{id isDraft}}}"
)
_MARK_READY_MUTATION = (
    "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
    "{pullRequest{id isDraft}}}"
)


def _set_pr_draft_via_graphql(
    client: httpx.Client, node_id: str, draft: bool,
) -> None:
    """Toggle a PR's draft state via GraphQL.

    GitHub's REST `PATCH /pulls/{n}` does not accept a `draft` field —
    the only way to flip the state programmatically is the GraphQL
    `convertPullRequestToDraft` / `markPullRequestReadyForReview`
    mutations. We POST to `/graphql` on the same client so the test
    `_client` monkeypatch covers this path too.
    """
    query = _CONVERT_DRAFT_MUTATION if draft else _MARK_READY_MUTATION
    r = client.post(
        "/graphql",
        json={"query": query, "variables": {"id": node_id}},
    )
    _check(r)
    body = r.json()
    if body.get("errors"):
        raise GitHubError(400, f"GraphQL error toggling draft: {body['errors']}")


# ---------- GraphQL: GitHub Projects v2 board support (ticket #118) --------
#
# Projects v2 are org- or user-scoped, not repo-bound: the same field
# selection has to be tried under `organization(login:)` and, on failure,
# retried under `user(login:)`. Every board query below is built in both
# flavours from a single template so the two stay in lockstep.


def _board_columns_query(owner_field: str) -> str:
    return (
        "query($owner:String!,$number:Int!,$fieldName:String!){"
        f"{owner_field}(login:$owner){{projectV2(number:$number){{"
        "field(name:$fieldName){"
        "...on ProjectV2SingleSelectField{id name options{id name}}"
        "}"
        "}}}"
    )


_BOARD_COLUMNS_ORG_QUERY = _board_columns_query("organization")
_BOARD_COLUMNS_USER_QUERY = _board_columns_query("user")


def _board_items_query(owner_field: str) -> str:
    return (
        "query($owner:String!,$number:Int!,$fieldName:String!,$after:String){"
        f"{owner_field}(login:$owner){{projectV2(number:$number){{"
        "items(first:100,after:$after){"
        "pageInfo{hasNextPage endCursor}"
        "nodes{"
        "fieldValueByName(name:$fieldName){"
        "...on ProjectV2ItemFieldSingleSelectValue{name optionId}"
        "}"
        "content{"
        "__typename"
        "...on Issue{"
        "number title body state stateReason url "
        "repository{nameWithOwner} "
        "createdAt updatedAt "
        "author{login}"
        "assignees(first:50){nodes{login}}"
        "labels(first:50){nodes{name}}"
        "}"
        "}"
        "}"
        "}"
        "}}}"
    )


_BOARD_ITEMS_ORG_QUERY = _board_items_query("organization")
_BOARD_ITEMS_USER_QUERY = _board_items_query("user")


def _is_organization_resolution_error(body: dict) -> bool:
    """True iff `body`'s GraphQL `errors` say the owner login isn't an
    Organization (GitHub's message for `organization(login:)` pointed at
    a user account) — the signal that we should retry under `user(login:)`.
    """
    for err in body.get("errors") or []:
        message = str((err or {}).get("message") or "").lower()
        if "could not resolve to an organization" in message:
            return True
    return False


def _fetch_projects_v2_via_graphql(
    client: httpx.Client,
    *,
    owner: str,
    project_number: int,
    org_query: str,
    user_query: str,
    variables: dict[str, Any],
) -> dict:
    """POST `org_query` first (`organization(login:$owner)`); if GitHub's
    GraphQL response says that login can't resolve to an Organization,
    retry the identical field selection as `user_query`
    (`user(login:$owner)`). Returns the `projectV2` payload (never
    `None`) from whichever attempt found the project.

    Raises `GitHubError` on a hard GraphQL error from either attempt
    (other than the org/user resolution signal), and `ValueError` when
    both attempts succeed at the transport level but neither actually
    has the project (bad `owner`/`project_number`).
    """
    r = client.post(
        "/graphql", json={"query": org_query, "variables": variables},
    )
    _check(r)
    body = r.json()
    owner_key = "organization"
    if body.get("errors") and _is_organization_resolution_error(body):
        r = client.post(
            "/graphql", json={"query": user_query, "variables": variables},
        )
        _check(r)
        body = r.json()
        owner_key = "user"
        if body.get("errors"):
            raise GitHubError(
                400,
                f"GraphQL error resolving GitHub Projects v2 project "
                f"#{project_number} for owner {owner!r} (tried both "
                f"organization and user): {body['errors']}",
            )
    elif body.get("errors"):
        raise GitHubError(
            400,
            f"GraphQL error resolving GitHub Projects v2 project "
            f"#{project_number} for owner {owner!r}: {body['errors']}",
        )
    project_v2 = ((body.get("data") or {}).get(owner_key) or {}).get("projectV2")
    if not project_v2:
        tried = "organization and user" if owner_key == "user" else "organization"
        raise ValueError(
            f"GitHub Projects v2 project #{project_number} not found for "
            f"owner {owner!r} (tried {tried})"
        )
    return project_v2


# ---------- GraphQL: custom_fields read/write via Projects v2 (ticket #123) -
#
# `get_ticket(..., include_custom_fields=True)` / `create_ticket(...,
# custom_fields=...)` piggyback on the same `github-projects-v2` board
# binding as `list_board_columns` (ticket #118): custom fields *are* the
# board's Projects v2 fields.


_ISSUE_PROJECT_FIELDS_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){"
    "repository(owner:$owner,name:$repo){"
    "issue(number:$number){"
    "projectItems(first:20){"
    "nodes{"
    "project{number}"
    "fieldValues(first:50){"
    "nodes{"
    "...on ProjectV2ItemFieldSingleSelectValue{name field{...on ProjectV2FieldCommon{name}}}"
    "...on ProjectV2ItemFieldTextValue{text field{...on ProjectV2FieldCommon{name}}}"
    "...on ProjectV2ItemFieldNumberValue{number field{...on ProjectV2FieldCommon{name}}}"
    "...on ProjectV2ItemFieldDateValue{date field{...on ProjectV2FieldCommon{name}}}"
    "...on ProjectV2ItemFieldIterationValue{title field{...on ProjectV2FieldCommon{name}}}"
    "}"
    "}"
    "}"
    "}"
    "}"
    "}}"
)


def _extract_project_field_values(item: dict) -> dict[str, Any]:
    """Flatten a `projectItems` node's typed `fieldValues` into a plain
    `{field name: native display value}` map (ticket #123).

    Each `fieldValues` node only carries the sub-selection matching its
    concrete GraphQL type (the inline fragment that applied), so exactly
    one of `name`/`text`/`number`/`date`/`title` is present per node —
    whichever is present *is* the field's native value.
    """
    result: dict[str, Any] = {}
    for fv in (item.get("fieldValues") or {}).get("nodes") or []:
        field_name = (fv.get("field") or {}).get("name")
        if not field_name:
            continue
        for key in ("name", "text", "number", "date", "title"):
            if key in fv:
                result[field_name] = fv[key]
                break
    return result


def _fetch_issue_project_item_via_graphql(
    client: httpx.Client,
    *,
    repo_owner: str,
    repo: str,
    issue_number: int,
    project_number: int,
) -> dict[str, Any] | None:
    """Return the raw `projectItems` node for `issue_number` on the
    Projects v2 board `project_number`, or `None` if the issue has no
    item on that project.

    This is the single query result shared by `custom_fields`
    (`_extract_project_field_values`) and `milestone`
    (`_extract_iteration_value`) — ticket #151 requires reusing one
    `projectItems` round-trip for both rather than double-querying.

    Scoped through the repository (not org/user), so no org/user retry
    is needed here — `list_board_columns`'s retry dance is only for
    resolving the *board itself* by owner login.
    """
    r = client.post(
        "/graphql",
        json={
            "query": _ISSUE_PROJECT_FIELDS_QUERY,
            "variables": {
                "owner": repo_owner, "repo": repo, "number": issue_number,
            },
        },
    )
    _check(r)
    body = r.json()
    if body.get("errors"):
        raise GitHubError(
            400,
            f"GraphQL error fetching project fields for issue "
            f"#{issue_number}: {body['errors']}",
        )
    issue = ((body.get("data") or {}).get("repository") or {}).get("issue") or {}
    for item in (issue.get("projectItems") or {}).get("nodes") or []:
        if (item.get("project") or {}).get("number") == project_number:
            return item
    return None


def _fetch_issue_project_fields_via_graphql(
    client: httpx.Client,
    *,
    repo_owner: str,
    repo: str,
    issue_number: int,
    project_number: int,
) -> dict[str, Any] | None:
    """Return the `custom_fields` map for `issue_number`'s item on the
    Projects v2 board `project_number`, or `None` if the issue has no
    item on that project (ticket #123 read path)."""
    item = _fetch_issue_project_item_via_graphql(
        client,
        repo_owner=repo_owner, repo=repo,
        issue_number=issue_number, project_number=project_number,
    )
    return _extract_project_field_values(item) if item is not None else None


def _extract_iteration_value(item: dict, iteration_field: str | None) -> str | None:
    """Return the milestone/iteration field's title from a `projectItems`
    node's raw `fieldValues` (ticket #151).

    Scans `fieldValues.nodes` for the iteration-typed value — the only
    type in `_ISSUE_PROJECT_FIELDS_QUERY`'s selection that carries a
    `title` key (`ProjectV2ItemFieldIterationValue`). When
    `iteration_field` is set, matches that field name exactly; otherwise
    the first iteration-typed node wins (deterministic by node order).
    """
    for fv in (item.get("fieldValues") or {}).get("nodes") or []:
        if "title" not in fv:
            continue
        field_name = (fv.get("field") or {}).get("name")
        if iteration_field is not None and field_name != iteration_field:
            continue
        return fv.get("title")
    return None


def _populate_board_fields(
    client: httpx.Client,
    ticket: Ticket,
    *,
    repo_owner: str,
    repo: str,
    issue_number: int,
    binding: Any,
    include_custom_fields: bool,
) -> None:
    """Populate `ticket.milestone` (and, when `include_custom_fields` is
    True, `ticket.custom_fields`) from a single shared `projectItems`
    GraphQL read (ticket #151/#185).

    This is the board-bound assembly `get_ticket` performs inline, factored
    out so `update_ticket`'s post-board-write re-GET (`_reget_issue`) can
    reuse it too — after a `custom_fields` write or a reopen board-column
    reset, the returned `Ticket` should report the same board-derived
    `custom_fields`/`milestone` shape an immediate
    `get_ticket(..., include_custom_fields=True)` would (ticket #185).

    Mirrors `get_ticket`'s board-bound branch exactly: `milestone` is
    always set (`None` when the issue has no item on the project);
    `custom_fields` is set only when `include_custom_fields` is True
    (`{}`, not `None`, when the issue has no item — "bound but empty").
    """
    item = _fetch_issue_project_item_via_graphql(
        client,
        repo_owner=repo_owner,
        repo=repo,
        issue_number=issue_number,
        project_number=binding.project_number,
    )
    ticket.milestone = (
        _extract_iteration_value(item, binding.iteration_field)
        if item is not None else None
    )
    if include_custom_fields:
        ticket.custom_fields = (
            _extract_project_field_values(item) if item is not None else {}
        )


def _board_field_write_query(owner_field: str) -> str:
    """Like `_board_columns_query`, but exposes the field's own `id`
    for *every* field type (via the `ProjectV2FieldCommon` interface),
    not just single-select — the write path needs `fieldId` regardless
    of whether the field is single-select, text, number, or date.
    """
    return (
        "query($owner:String!,$number:Int!,$fieldName:String!){"
        f"{owner_field}(login:$owner){{projectV2(number:$number){{"
        "field(name:$fieldName){"
        "...on ProjectV2FieldCommon{id name}"
        "...on ProjectV2SingleSelectField{options{id name}}"
        "...on ProjectV2IterationField{configuration{iterations{id title} completedIterations{id title}}}"
        "}"
        "}}}"
    )


_BOARD_FIELD_WRITE_ORG_QUERY = _board_field_write_query("organization")
_BOARD_FIELD_WRITE_USER_QUERY = _board_field_write_query("user")


def _board_field_options_query(owner_field: str) -> str:
    """Like `_board_columns_query`, but additionally selects each option's
    `color`/`description` — `ensure_board_column` (ticket #192) needs the
    full option payload to round-trip every existing option through the
    `updateProjectV2Field` mutation without losing data.

    Kept as its own template (not folded into `_board_columns_query` or
    `_board_field_write_query`) so each query keeps selecting exactly what
    its one caller needs.
    """
    return (
        "query($owner:String!,$number:Int!,$fieldName:String!){"
        f"{owner_field}(login:$owner){{projectV2(number:$number){{"
        "field(name:$fieldName){"
        "...on ProjectV2SingleSelectField{id name options{id name color description}}"
        "}"
        "}}}"
    )


_BOARD_FIELD_OPTIONS_ORG_QUERY = _board_field_options_query("organization")
_BOARD_FIELD_OPTIONS_USER_QUERY = _board_field_options_query("user")


def _board_project_id_query(owner_field: str) -> str:
    return (
        "query($owner:String!,$number:Int!){"
        f"{owner_field}(login:$owner){{projectV2(number:$number){{id}}}}}}"
    )


_BOARD_PROJECT_ID_ORG_QUERY = _board_project_id_query("organization")
_BOARD_PROJECT_ID_USER_QUERY = _board_project_id_query("user")


_ADD_PROJECT_V2_ITEM_MUTATION = (
    "mutation($projectId:ID!,$contentId:ID!){"
    "addProjectV2ItemById(input:{projectId:$projectId,contentId:$contentId})"
    "{item{id}}}"
)
_UPDATE_PROJECT_V2_ITEM_FIELD_VALUE_MUTATION = (
    "mutation($projectId:ID!,$itemId:ID!,$fieldId:ID!,$value:ProjectV2FieldValue!){"
    "updateProjectV2ItemFieldValue(input:{projectId:$projectId,itemId:$itemId,"
    "fieldId:$fieldId,value:$value}){projectV2Item{id}}}"
)
_UPDATE_PROJECT_V2_FIELD_OPTIONS_MUTATION = (
    "mutation($fieldId:ID!,$options:[ProjectV2SingleSelectFieldOptionInput!]!){"
    "updateProjectV2Field(input:{fieldId:$fieldId,singleSelectOptions:$options})"
    "{projectV2Field{...on ProjectV2SingleSelectField{id}}}}"
)


def _resolve_project_field_for_write(
    client: httpx.Client, binding: Any, field_name: str,
) -> dict:
    """Resolve `field_name` against the bound Projects v2 board.

    Returns the GraphQL `field` payload: `{"id":..., "name":...,
    "options":[{"id":..., "name":...}, ...]}` for single-select fields,
    `{"id":..., "name":...}` for text/number/date/iteration fields.
    Raises `ValueError` when the field doesn't exist on the board.
    """
    project_v2 = _fetch_projects_v2_via_graphql(
        client,
        owner=binding.owner,
        project_number=binding.project_number,
        org_query=_BOARD_FIELD_WRITE_ORG_QUERY,
        user_query=_BOARD_FIELD_WRITE_USER_QUERY,
        variables={
            "owner": binding.owner,
            "number": binding.project_number,
            "fieldName": field_name,
        },
    )
    field = project_v2.get("field")
    if not field or not field.get("id"):
        raise ValueError(
            f"GitHub Projects v2 field {field_name!r} was not found on "
            f"project #{binding.project_number} for owner {binding.owner!r}"
        )
    return field


def _ensure_single_select_option(
    client: httpx.Client, binding: Any, field_name: str, option_name: str,
) -> bool:
    """Fetch `field_name`'s live single-select options and, if `option_name`
    isn't already present (case-insensitively), add it via
    `updateProjectV2Field` (ticket #192 — `ensure_board_column`).

    Every existing option is round-tripped (`id`/`name`/`color`/
    `description`) in the mutation's `$options` array so GitHub preserves
    stable option ids and existing item assignments — the mutation is
    non-clobbering. `color`/`description` fall back to `"GRAY"`/`""`
    respectively if the API response omits them, since
    `ProjectV2SingleSelectFieldOptionInput` requires non-null values for
    both. The new option is sent without an `id` (GitHub assigns one) and
    defaults to `color="GRAY"`, `description=""`.

    Returns `True` if the mutation was issued (option created), `False`
    if the option already existed and no mutation was sent (idempotent
    no-op).

    Raises `ValueError` when `field_name` doesn't resolve to a
    single-select field on the board — mirrors `list_board_columns`'s
    field-not-found error. Unlike `list_board_columns`, an *empty*
    `options` list is valid here: a brand-new Status field with zero
    options must still accept its first column.
    """
    project_v2 = _fetch_projects_v2_via_graphql(
        client,
        owner=binding.owner,
        project_number=binding.project_number,
        org_query=_BOARD_FIELD_OPTIONS_ORG_QUERY,
        user_query=_BOARD_FIELD_OPTIONS_USER_QUERY,
        variables={
            "owner": binding.owner,
            "number": binding.project_number,
            "fieldName": field_name,
        },
    )
    field = project_v2.get("field")
    if not field or not field.get("id"):
        raise ValueError(
            f"GitHub Projects v2 field {field_name!r} was not found (or is "
            f"not a single-select field) on project #{binding.project_number} "
            f"for owner {binding.owner!r}"
        )
    options = field.get("options") or []
    by_lower_name = {opt["name"].lower(): opt for opt in options}
    if option_name.lower() in by_lower_name:
        return False
    new_options = [
        {
            "id": opt["id"],
            "name": opt["name"],
            "color": opt.get("color") or "GRAY",
            "description": opt.get("description") or "",
        }
        for opt in options
    ]
    new_options.append({"name": option_name, "color": "GRAY", "description": ""})
    r = client.post(
        "/graphql",
        json={
            "query": _UPDATE_PROJECT_V2_FIELD_OPTIONS_MUTATION,
            "variables": {"fieldId": field["id"], "options": new_options},
        },
    )
    _check(r)
    body = r.json()
    if body.get("errors"):
        raise GitHubError(
            400,
            f"GraphQL error adding board column {option_name!r} to field "
            f"{field_name!r}: {body['errors']}",
        )
    updated_field = (
        (body.get("data") or {}).get("updateProjectV2Field") or {}
    ).get("projectV2Field")
    if not updated_field:
        raise GitHubError(
            500, "updateProjectV2Field did not return a projectV2Field"
        )
    return True


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _project_v2_field_value_input(
    field: dict, field_name: str, value: Any,
) -> dict:
    """Map a `custom_fields` value to the `ProjectV2FieldValue` input
    shape `updateProjectV2ItemFieldValue` expects — exactly one of
    `singleSelectOptionId` / `text` / `number` / `date` is set.

    Single-select fields resolve `value` (a display-name string) to its
    live `optionId`, matched case-insensitively; an unmatched value
    raises `ValueError`. Non-single-select fields infer the input kind
    from `value`'s Python type (`bool`/`str` digits-only date -> date,
    `int`/`float` -> number, everything else -> text).

    Iteration fields (`configuration` present — ticket #151's
    `milestone=` write path) resolve `value` (a title string) against
    the field's active + completed iterations, matched
    case-insensitively; an unmatched title raises `ValueError` listing
    the available titles.
    """
    configuration = field.get("configuration")
    if configuration:
        if not isinstance(value, str):
            raise ValueError(
                f"custom_fields[{field_name!r}] must be a string iteration "
                f"title for iteration field {field_name!r}, got {value!r}"
            )
        iterations = [
            *(configuration.get("iterations") or []),
            *(configuration.get("completedIterations") or []),
        ]
        by_lower_title = {it["title"].lower(): it for it in iterations}
        matched = by_lower_title.get(value.lower())
        if matched is None:
            available = sorted(it["title"] for it in iterations)
            raise ValueError(
                f"custom_fields[{field_name!r}] value {value!r} is not a "
                f"valid iteration (available: {available})"
            )
        return {"iterationId": matched["id"]}
    options = field.get("options")
    if options:
        if not isinstance(value, str):
            raise ValueError(
                f"custom_fields[{field_name!r}] must be a string option "
                f"name for single-select field {field_name!r}, got {value!r}"
            )
        by_lower_name = {opt["name"].lower(): opt for opt in options}
        opt = by_lower_name.get(value.lower())
        if opt is None:
            available = sorted(o["name"] for o in options)
            raise ValueError(
                f"custom_fields[{field_name!r}] value {value!r} is not a "
                f"valid option (available: {available})"
            )
        return {"singleSelectOptionId": opt["id"]}
    if isinstance(value, bool):
        return {"text": str(value)}
    if isinstance(value, (int, float)):
        return {"number": value}
    if isinstance(value, str) and _ISO_DATE_RE.match(value):
        return {"date": value}
    return {"text": "" if value is None else str(value)}


def _add_project_v2_item(
    client: httpx.Client, project_id: str, content_id: str,
) -> str:
    """Add `content_id` (an issue's `node_id`) to `project_id`'s board
    and return the new item's node id."""
    r = client.post(
        "/graphql",
        json={
            "query": _ADD_PROJECT_V2_ITEM_MUTATION,
            "variables": {"projectId": project_id, "contentId": content_id},
        },
    )
    _check(r)
    body = r.json()
    if body.get("errors"):
        raise GitHubError(
            400, f"GraphQL error adding issue to project: {body['errors']}"
        )
    item = ((body.get("data") or {}).get("addProjectV2ItemById") or {}).get("item") or {}
    item_id = item.get("id")
    if not item_id:
        raise GitHubError(500, "addProjectV2ItemById did not return an item id")
    return item_id


def _update_project_v2_item_field_value(
    client: httpx.Client,
    project_id: str,
    item_id: str,
    field_id: str,
    value: dict,
) -> None:
    r = client.post(
        "/graphql",
        json={
            "query": _UPDATE_PROJECT_V2_ITEM_FIELD_VALUE_MUTATION,
            "variables": {
                "projectId": project_id,
                "itemId": item_id,
                "fieldId": field_id,
                "value": value,
            },
        },
    )
    _check(r)
    body = r.json()
    if body.get("errors"):
        raise GitHubError(
            400, f"GraphQL error updating project field value: {body['errors']}"
        )


def _write_custom_fields_to_board(
    client: httpx.Client, binding: Any, content_id: str, custom_fields: dict[str, Any],
) -> None:
    """Add the created issue to the bound Projects v2 board and write
    each `custom_fields` entry onto it (ticket #123 write path)."""
    project_v2 = _fetch_projects_v2_via_graphql(
        client,
        owner=binding.owner,
        project_number=binding.project_number,
        org_query=_BOARD_PROJECT_ID_ORG_QUERY,
        user_query=_BOARD_PROJECT_ID_USER_QUERY,
        variables={"owner": binding.owner, "number": binding.project_number},
    )
    project_id = project_v2.get("id")
    if not project_id:
        raise GitHubError(
            500,
            f"GitHub Projects v2 project #{binding.project_number} for "
            f"owner {binding.owner!r} did not return an 'id'",
        )
    item_id = _add_project_v2_item(client, project_id, content_id)
    for field_name, value in custom_fields.items():
        field = _resolve_project_field_for_write(client, binding, field_name)
        value_input = _project_v2_field_value_input(field, field_name, value)
        _update_project_v2_item_field_value(
            client, project_id, item_id, field["id"], value_input,
        )


_CLEAR_PROJECT_V2_ITEM_FIELD_VALUE_MUTATION = (
    "mutation($projectId:ID!,$itemId:ID!,$fieldId:ID!){"
    "clearProjectV2ItemFieldValue(input:{projectId:$projectId,itemId:$itemId,"
    "fieldId:$fieldId}){projectV2Item{id}}}"
)


def _clear_project_v2_item_field_value(
    client: httpx.Client, project_id: str, item_id: str, field_id: str,
) -> None:
    r = client.post(
        "/graphql",
        json={
            "query": _CLEAR_PROJECT_V2_ITEM_FIELD_VALUE_MUTATION,
            "variables": {
                "projectId": project_id, "itemId": item_id, "fieldId": field_id,
            },
        },
    )
    _check(r)
    body = r.json()
    if body.get("errors"):
        raise GitHubError(
            400, f"GraphQL error clearing project field value: {body['errors']}"
        )


def _write_milestone_to_board(
    client: httpx.Client, binding: Any, content_id: str, milestone: str | None,
) -> None:
    """Write `milestone` (an iteration title, or `None` to clear) to the
    bound board's `binding.iteration_field` (ticket #151).

    Mirrors `_write_custom_fields_to_board`'s project/item resolution but
    branches to `clearProjectV2ItemFieldValue` when `milestone` is `None`
    instead of `updateProjectV2ItemFieldValue` (which has no "clear"
    input shape). Callers must have already validated that
    `binding.iteration_field` is configured.
    """
    project_v2 = _fetch_projects_v2_via_graphql(
        client,
        owner=binding.owner,
        project_number=binding.project_number,
        org_query=_BOARD_PROJECT_ID_ORG_QUERY,
        user_query=_BOARD_PROJECT_ID_USER_QUERY,
        variables={"owner": binding.owner, "number": binding.project_number},
    )
    project_id = project_v2.get("id")
    if not project_id:
        raise GitHubError(
            500,
            f"GitHub Projects v2 project #{binding.project_number} for "
            f"owner {binding.owner!r} did not return an 'id'",
        )
    item_id = _add_project_v2_item(client, project_id, content_id)
    field = _resolve_project_field_for_write(client, binding, binding.iteration_field)
    if milestone is None:
        _clear_project_v2_item_field_value(client, project_id, item_id, field["id"])
        return
    value_input = _project_v2_field_value_input(field, binding.iteration_field, milestone)
    _update_project_v2_item_field_value(
        client, project_id, item_id, field["id"], value_input,
    )


# ---------- GraphQL fallback for `parent` ---------------------------------

_PARENT_GRAPHQL_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){"
    "repository(owner:$owner,name:$repo){"
    "issueOrPullRequest(number:$number){"
    "...on Issue{parent{number title url state repository{nameWithOwner}}}"
    "}}}"
)


def _fetch_parent_via_graphql(
    token: str | None,
    project: ProjectConfig,
    ticket_id: str,
) -> dict | None:
    """One-shot GraphQL call: return a REST-shaped parent payload or None.

    Any error collapses to `None` — this is a best-effort sidecall.
    """
    if not token:
        return None
    try:
        ticket_num = int(ticket_id)
    except (TypeError, ValueError):
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    payload = {
        "query": _PARENT_GRAPHQL_QUERY,
        "variables": {
            "owner": project.owner,
            "repo": project.repo,
            "number": ticket_num,
        },
    }
    try:
        with httpx.Client(headers=headers, timeout=15.0) as c:
            r = c.post("https://api.github.com/graphql", json=payload)
    except (httpx.HTTPError, OSError):
        return None
    if not r.is_success:
        return None
    try:
        body = r.json()
    except Exception:
        return None
    if body.get("errors"):
        return None
    issue = (
        ((body.get("data") or {}).get("repository") or {})
        .get("issueOrPullRequest")
    ) or {}
    parent = issue.get("parent")
    if not parent:
        return None
    repo_node = parent.get("repository") or {}
    state = (parent.get("state") or "").lower()
    return {
        "number": parent.get("number"),
        "title": parent.get("title") or "",
        "html_url": parent.get("url") or "",
        "state": "open" if state == "open" else "closed",
        "repository": {"full_name": repo_node.get("nameWithOwner") or ""},
    }


# ---------- helpers for incoming relabel + blocking events -----------------


def _relabel_incoming(
    rel: Relation, source_issue: dict, *, ticket_num: int | None,
) -> Relation:
    """Promote a `mentioned_by` to a stronger incoming kind when possible.

    - Source closed as duplicate, body's "Duplicate of #N" -> us:
      relabel to `duplicated_by`.
    - Source is a merged PR whose body has a closing keyword
      targeting us: relabel to `closed_by`.
    """
    src_state = source_issue.get("state")
    src_state_reason = source_issue.get("state_reason")
    src_body = source_issue.get("body") or ""
    is_pr = bool(source_issue.get("pull_request"))
    merged_at = source_issue.get("merged_at") or (
        (source_issue.get("pull_request") or {}).get("merged_at")
    )

    if (
        src_state == "closed"
        and src_state_reason == "duplicate"
        and ticket_num is not None
    ):
        m = re.search(
            r"duplicate\s+of\s+(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
            r"/[A-Za-z0-9._\-]+)?#(\d+)",
            src_body,
            re.IGNORECASE,
        )
        if m:
            try:
                tgt = int(m.group(1))
            except (TypeError, ValueError):
                tgt = None
            if tgt == ticket_num:
                return Relation(
                    kind="duplicated_by",
                    ticket_id=rel.ticket_id,
                    title=rel.title,
                    url=rel.url,
                    state=rel.state,
                    is_pull_request=rel.is_pull_request,
                    resolved=rel.resolved,
                )

    if is_pr and merged_at and ticket_num is not None:
        for owner, repo, num in _scan_refs(src_body, closing_only=True):
            if num == ticket_num and (owner is None or repo is None):
                return Relation(
                    kind="closed_by",
                    ticket_id=rel.ticket_id,
                    title=rel.title,
                    url=rel.url,
                    state=rel.state,
                    is_pull_request=rel.is_pull_request,
                    resolved=rel.resolved,
                )

    return rel


def _map_blocking_event(
    event: dict, project: ProjectConfig, kind: str,
) -> Relation | None:
    """Map a GitHub Issue-Dependencies timeline event into a Relation."""
    blocked_by = event.get("blocked_by_issue") or {}
    blocking = event.get("blocking_issue") or {}
    src = (event.get("source") or {}).get("issue") or {}
    issue_raw = blocked_by or blocking or src
    if not issue_raw or not issue_raw.get("number"):
        return None
    return _map_relation_from_sub_issue(issue_raw, project, kind)


def _fetch_dependencies(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
) -> list[Relation]:
    """Read both directions of the Issue Dependencies API (2026-03-10).

    Authoritative source for `blocks` / `blocked_by` — the legacy
    timeline-event surface (`blocked_by_added` / `blocking_added`) is
    no longer emitted for dependencies created via the REST endpoints,
    so polling Timeline alone misses everything `add_relation` writes.

    Returns `[]` for repos where the endpoints don't exist (404/410)
    or the caller lacks permission (403) — non-fatal so other relation
    kinds keep flowing.
    """
    out: list[Relation] = []
    for endpoint, kind in (
        ("blocked_by", "blocked_by"),
        ("blocking", "blocks"),
    ):
        r = client.get(
            f"{_repo_path(project)}/issues/{ticket_id}/dependencies/{endpoint}",
            params={"per_page": 100},
        )
        if r.status_code in (403, 404, 410):
            continue
        _check(r)
        for issue_raw in r.json() or []:
            if not issue_raw.get("number"):
                continue
            out.append(
                _map_relation_from_sub_issue(issue_raw, project, kind),
            )
    return out


# ---------- core relation collector ----------------------------------------


def _fetch_relations(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
    issue_payload: dict,
    token: str | None = None,
) -> tuple[list[Relation], bool]:
    """Collect all relation links for a ticket. Returns (relations, truncated).

    Relation kinds emitted (per ticket #5):
      - `parent` — REST `parent` field, GraphQL fallback when missing.
      - `child` — `/sub_issues` walk.
      - `closes` / `mentions` — outgoing, scanned from queried ticket's
        body (and comments per env-configurable depth).
      - `duplicate_of` — outgoing, detected from a ``Duplicate of #N``
        line in the ticket body (body is the source of truth; state is
        irrelevant).
      - `mentioned_by` — generic incoming cross-references.
      - `duplicated_by` / `closed_by` — re-labeled from incoming
        cross-refs by inspecting source state / body.
      - `blocks` / `blocked_by` — GitHub Issue Dependencies events.
    """
    relations: list[Relation] = []
    truncated = False
    self_repo_full = f"{project.owner}/{project.repo}"
    try:
        self_num = int(ticket_id)
    except (TypeError, ValueError):
        self_num = None

    # parent
    parent = issue_payload.get("parent")
    if parent:
        relations.append(_map_relation_from_sub_issue(parent, project, "parent"))
    else:
        graphql_parent = _fetch_parent_via_graphql(token, project, ticket_id)
        if graphql_parent:
            relations.append(
                _map_relation_from_sub_issue(graphql_parent, project, "parent")
            )

    # children via /sub_issues
    sub_r = client.get(
        f"{_repo_path(project)}/issues/{ticket_id}/sub_issues",
        params={"per_page": 100},
    )
    if sub_r.status_code in (404, 410):
        pass
    else:
        _check(sub_r)
        for sub in sub_r.json() or []:
            relations.append(_map_relation_from_sub_issue(sub, project, "child"))

    # blocks / blocked_by via Issue Dependencies REST (api 2026-03-10) —
    # authoritative source. Timeline events for these are no longer
    # emitted for dependencies created via the REST endpoints, so the
    # later timeline scan alone misses everything write-side
    # `add_relation(kind="blocks"/"blocked_by")` persists.
    relations.extend(_fetch_dependencies(client, project, ticket_id))

    # outgoing scan: closes / mentions / duplicate_of
    self_body = issue_payload.get("body") or ""
    closes_refs = _scan_refs(self_body, closing_only=True)
    all_refs = _scan_refs(self_body, closing_only=False)

    depth = _mentions_scan_depth()
    if depth != 0:
        comments_payload = _fetch_comments_for_scan(
            client, project, ticket_id, depth
        )
        for comment in comments_payload:
            cbody = comment.get("body") or ""
            for ref in _scan_refs(cbody, closing_only=True):
                if ref not in closes_refs:
                    closes_refs.append(ref)
            for ref in _scan_refs(cbody, closing_only=False):
                if ref not in all_refs:
                    all_refs.append(ref)

    def _is_self(owner: str | None, repo: str | None, num: int) -> bool:
        if num != self_num:
            return False
        if owner is None and repo is None:
            return True
        return f"{owner}/{repo}" == self_repo_full

    closes_set = {
        (o, r, n) for (o, r, n) in closes_refs if not _is_self(o, r, n)
    }
    mentions_only = [
        (o, r, n) for (o, r, n) in all_refs
        if not _is_self(o, r, n) and (o, r, n) not in closes_set
    ]

    for owner, repo, num in closes_set:
        relations.append(_ref_to_relation(owner, repo, num, project, "closes"))
    for owner, repo, num in mentions_only:
        relations.append(_ref_to_relation(owner, repo, num, project, "mentions"))

    # Line-anchored match: the marker must occupy its own line, exactly as
    # `_github_mark_duplicate_of` writes it ("Duplicate of #N" at line start).
    # Anchoring prevents false positives from embedded prose like
    # "this is not a duplicate of #12, see discussion."
    m = re.search(
        r"(?m)^duplicate\s+of\s+(?:(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)"
        r"/(?P<repo>[A-Za-z0-9._\-]+))?#(?P<num>\d+)\s*$",
        self_body or "",
        re.IGNORECASE,
    )
    if m:
        try:
            num = int(m.group("num"))
        except (TypeError, ValueError):
            num = None
        if num is not None and not _is_self(
            m.group("owner"), m.group("repo"), num
        ):
            relations.append(
                _fetch_duplicate_of_relation(
                    client, project, m.group("owner"), m.group("repo"), num,
                )
            )

    # incoming: timeline scan
    tl_r = client.get(
        f"{_repo_path(project)}/issues/{ticket_id}/timeline",
        params={"per_page": 100},
        headers={"Accept": "application/vnd.github+json"},
    )
    _check(tl_r)
    truncated = _has_next_link(tl_r.headers.get("Link"))
    for event in tl_r.json() or []:
        etype = event.get("event")
        if etype in ("blocked_by_added", "blocking_added"):
            kind = "blocked_by" if etype == "blocked_by_added" else "blocks"
            mapped = _map_blocking_event(event, project, kind)
            if mapped is not None:
                relations.append(mapped)
            continue
        if etype in ("blocked_by_removed", "blocking_removed"):
            continue
        mapped = _map_relation_from_timeline(
            event, project, self_id=str(ticket_id),
        )
        if mapped is None:
            continue
        if mapped.kind == "mentioned_by" and etype == "cross-referenced":
            src = (event.get("source") or {}).get("issue") or {}
            mapped = _relabel_incoming(mapped, src, ticket_num=self_num)
        relations.append(mapped)

    return _dedupe_relations(relations), truncated


def _fetch_comments_for_scan(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
    depth: int,
) -> list[dict]:
    """Fetch comments for ref-scanning, honoring the env-configurable depth."""
    if depth == 0:
        return []
    if depth < 0:
        out: list[dict] = []
        page = 1
        while True:
            r = client.get(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                params={"per_page": 100, "page": page},
            )
            _check(r)
            chunk = r.json() or []
            out.extend(chunk)
            if not _has_next_link(r.headers.get("Link")):
                break
            page += 1
            if page > 100:
                break
        return out
    per_page = min(max(1, depth), 100)
    r = client.get(
        f"{_repo_path(project)}/issues/{ticket_id}/comments",
        params={"per_page": per_page},
    )
    _check(r)
    return (r.json() or [])[:depth]


def _dedupe_relations(rels: list[Relation]) -> list[Relation]:
    """Collapse duplicates and drop weakened labels.

    1. Collapse exact (kind, ticket_id) duplicates. A `resolved=True`
       entry supersedes an earlier `resolved=False` one for the same
       key (upgrade in place, keeping the original slot's position) —
       otherwise an unresolved body-scan stub (e.g. `duplicate_of` with
       empty `title`/`state`) can beat a later, fully-resolved timeline
       entry for the same real target and its metadata gets discarded
       (ticket #136). Among entries of equal resolution, the first
       occurrence wins.
    2. When a target has a stronger label, drop the generic
       `mentioned_by` / `mentions` for that same target.
    """
    seen: dict[tuple[str, str], Relation] = {}
    strong_kinds_by_target: dict[str, set[str]] = {}
    for r in rels:
        key = (r.kind, r.ticket_id)
        if key in seen:
            existing = seen[key]
            if r.resolved and not existing.resolved:
                seen[key] = r
            continue
        seen[key] = r
        if r.kind in {
            "duplicated_by", "closed_by", "duplicate_of", "closes",
            "blocks", "blocked_by", "parent", "child",
        }:
            strong_kinds_by_target.setdefault(r.ticket_id, set()).add(r.kind)
    out: list[Relation] = []
    for r in seen.values():
        if r.kind == "mentioned_by" and strong_kinds_by_target.get(r.ticket_id):
            continue
        if (
            r.kind == "mentions"
            and strong_kinds_by_target.get(r.ticket_id, set()) & {"closes", "duplicate_of"}
        ):
            continue
        out.append(r)
    return out


def _ensure_label(
    client: httpx.Client, project: ProjectConfig, name: str, role: str
) -> None:
    """Create the label on the target repo if it doesn't already exist.

    Hard-fails (raises `GitHubError`) when GitHub refuses the create call —
    most notably 403 for tokens without `push` permission on the target.
    The historical "log and continue" behaviour caused two production
    failure modes for `ai-generated` (see ticket
    Seretos/agent-marketplace#15): Mode A silent label-drop on the
    follow-up `POST /issues` (the label vanished from the response and
    the caller never knew) and Mode B hard 403 on the same POST when the
    label didn't yet exist on the target.

    Idempotent: 422 ("already_exists") is treated as success. Callers
    that can tolerate a missing label — e.g. `create_ticket` /
    `create_pr` / `update_ticket` / `update_pr`, all of which carry the
    marker in the body prefix as the canonical source of truth — should
    wrap this call in `_ensure_label_best_effort` so the operation
    proceeds without the label rather than aborting.
    """
    payload = {
        "name": name,
        "color": label_color(role),
        "description": label_description(role),
    }
    resp = client.post(f"{_repo_path(project)}/labels", json=payload)
    if resp.status_code in (200, 201):
        return
    if resp.status_code == 422:
        return  # already exists
    _check(resp)


def _ensure_label_best_effort(
    client: httpx.Client, project: ProjectConfig, name: str, role: str
) -> bool:
    """Best-effort wrapper around `_ensure_label`.

    Returns True when the label is known to exist on the repo (created
    or already-present), False when the repo refused creation (typically
    403 for tokens that lack `push`). The False case lets the caller
    drop the label from the subsequent POST payload so the issue / PR is
    still created — the body-prefix marker is the canonical source of
    truth and survives a missing label.
    """
    try:
        _ensure_label(client, project, name, role)
        return True
    except GitHubError as exc:
        log.warning(
            "could not ensure label '%s' on %s/%s: %s; falling back to "
            "body-prefix marker only",
            name, project.owner, project.repo, exc,
        )
        return False


def _assert_labels_exist(
    client: httpx.Client, project: ProjectConfig, names: list[str]
) -> None:
    """Raise GitHubError(404) for any label name in *names* that does not
    exist on the repo.

    Uses ``GET /repos/{owner}/{repo}/labels/{name}`` (one call per name).
    The project's configured AI-attribution labels are excluded — they
    keep intentional best-effort auto-create via
    ``_ensure_label_best_effort``.

    404 → ``GitHubError(404, "label {name!r} does not exist in {project.id}")``.
    Other non-2xx statuses go through ``_check`` as usual.
    """
    _ai_labels = {project.auto_labels.ai_generated, project.auto_labels.ai_modified}
    for name in names:
        if name in _ai_labels:
            continue
        r = client.get(f"{_repo_path(project)}/labels/{name}")
        if r.status_code == 404:
            raise GitHubError(
                404, f"label {name!r} does not exist in {project.id}"
            )
        _check(r)


def _label_present(payload: dict, name: str) -> bool:
    """True iff GitHub's response payload includes a label named `name`.

    GitHub silently drops labels from `POST /issues` when the caller has
    no `triage` (Mode A in ticket #15): the request succeeds with 201 but
    the resulting `labels` array is empty. This helper lets callers detect
    that case after the fact and warn — the body-prefix marker still gives
    machine-grep-able attribution, but the label is gone.
    """
    labels = payload.get("labels") or []
    for entry in labels:
        if isinstance(entry, str) and entry == name:
            return True
        if isinstance(entry, dict) and entry.get("name") == name:
            return True
    return False


def _quote_label(name: str) -> str:
    """Wrap a label name in double quotes for the Search qualifier if it
    contains whitespace; otherwise return as-is. The Search API treats
    `label:"foo bar"` as one qualifier with a space-bearing value.
    """
    if any(ch.isspace() for ch in name):
        return f'"{name}"'
    return name


def _requires_search(filters: TicketFilters) -> bool:
    """Return True iff any filter forces us off the cheap `/issues` path
    and onto `/search/issues`.

    `search` (free-text) ALSO requires the search endpoint, but the
    legacy code-path already handled that. This helper specifically
    captures the NEW filters added in Plan 7.
    """
    if filters.not_labels:
        return True
    if filters.author:
        return True
    if filters.created_after or filters.created_before:
        return True
    if filters.updated_after or filters.updated_before:
        return True
    return False


def _list_via_search(
    client: httpx.Client,
    project: ProjectConfig,
    filters: TicketFilters,
) -> list[dict]:
    """Hit `GET /search/issues` and return the raw `items` list.

    Builds a `q=` string that mirrors the legacy `/issues` semantics plus
    the new Plan-7 filters (`not_labels`, `author`, date ranges). Sort is
    expressed as a `sort:<key>-<order>` qualifier appended to `q` (NOT a
    separate `sort=` param — that's the legacy endpoint's convention).
    """
    per_page = min(max(1, filters.limit), 100)
    qual_parts: list[str] = [
        "is:issue",
        f"repo:{project.owner}/{project.repo}",
    ]
    # state qualifier: search supports `open`/`closed` only — omit for "any".
    # `states`, when non-empty, takes precedence over `status` entirely —
    # translate to the coarse open/closed/all state and let the caller
    # filter the returned items client-side for exact state_reason match.
    if filters.states:
        coarse = _github_coarse_state(_github_states_pairs(filters.states))
        if coarse in ("open", "closed"):
            qual_parts.append(f"state:{coarse}")
    elif filters.status in ("open", "closed"):
        qual_parts.append(f"state:{filters.status}")
    if filters.assignee:
        qual_parts.append(f"assignee:{filters.assignee}")
    if filters.author:
        qual_parts.append(f"author:{filters.author}")
    for lbl in filters.labels:
        qual_parts.append(f"label:{_quote_label(lbl)}")
    for lbl in filters.not_labels:
        qual_parts.append(f"-label:{_quote_label(lbl)}")
    if filters.created_after:
        qual_parts.append(f"created:>={filters.created_after}")
    if filters.created_before:
        qual_parts.append(f"created:<={filters.created_before}")
    if filters.updated_after:
        qual_parts.append(f"updated:>={filters.updated_after}")
    if filters.updated_before:
        qual_parts.append(f"updated:<={filters.updated_before}")
    qual_parts.append(f"sort:{filters.sort_by}-{filters.sort_order}")
    pieces = [filters.search] if filters.search else []
    pieces.extend(qual_parts)
    q = " ".join(pieces)
    r = client.get("/search/issues", params={"q": q, "per_page": per_page})
    _check(r)
    return r.json().get("items", [])


def _map_permissions_to_capabilities(perms: dict) -> TokenCapabilities:
    """Translate the `permissions` block GitHub returns on `GET /repos`
    into a `TokenCapabilities`.

    GitHub's permission ladder (low -> high): `pull`, `triage`, `push`,
    `maintain`, `admin`. The mapping:

    - `pull`     -> read only (no write flags).
    - `triage+`  -> `issues.modify` (label/assign/state on existing issues).
    - `push+`    -> `issues.create`, `pulls.create`, `pulls.modify`.
    - `maintain+` -> `pulls.merge` (matches GitHub's branch-protection
      semantics where merge requires maintain-equivalent rights).
    - `admin`    -> everything.

    `pull` alone never grants any write capability.
    """
    admin = bool(perms.get("admin"))
    maintain = bool(perms.get("maintain")) or admin
    push = bool(perms.get("push")) or maintain
    triage = bool(perms.get("triage")) or push
    # `pull` is the read flag; not needed for any of the write bits
    # because triage/push/maintain/admin all imply read.
    return TokenCapabilities(
        issues_create=push,
        issues_modify=triage,
        pulls_create=push,
        pulls_modify=push,
        pulls_merge=maintain,
        reason=None,
    )


# ticket #178: after a board write that may cascade into a REST-visible
# issue state change (e.g. Status:"Done" auto-closing the issue via a
# workflow), the cascade is asynchronous server-side. A single re-GET can
# still observe the pre-cascade snapshot. The bounded poll below is a
# BEST-EFFORT fast path — there is no GitHub API to await a pending
# Projects-v2 workflow, so an exhausted poll returns the last (possibly
# stale) REST `state`/`state_reason` snapshot rather than raising; callers
# needing the guaranteed settled REST state must issue a follow-up
# `get_ticket`. `_reget_sleep` is a mockable indirection so tests can
# replace the backoff with a no-op. This poll is about REST `state` only —
# ticket #185's `custom_fields`/`milestone` read-back (see
# `_populate_board_fields`) is a separate, non-polled GraphQL read that
# always reflects the board's current value at the time it fires.
_REGET_POLL_MAX_ATTEMPTS = 4
_REGET_POLL_BACKOFFS = (0.05, 0.1, 0.2)


def _reget_sleep(seconds: float) -> None:
    time.sleep(seconds)


class GitHubProvider(TokenProjectDiscoveryProvider, ViewerIdentityProvider):
    def probe_token_capabilities(
        self, project: ProjectConfig, token: str
    ) -> TokenCapabilities:
        """Probe `GET /repos/{owner}/{repo}` to learn what `token` may
        do against `project`.

        Failure modes are returned (not raised) so the caller can pass
        the result through to `_project_to_dict` unconditionally. See
        `TokenCapabilities.reason` for the stable failure identifiers.
        """
        try:
            with _client(token) as client:
                r = client.get(_repo_path(project))
        except httpx.HTTPError:
            return TokenCapabilities(reason="network_error")
        if r.status_code == 401:
            return TokenCapabilities(reason="bad_credentials")
        if r.status_code == 404:
            return TokenCapabilities(reason="repo_invisible_to_token")
        if not r.is_success:
            # Treat other unexpected statuses the same way as a missing
            # field: don't grant any write capability, but record what
            # happened so a caller can debug.
            return TokenCapabilities(
                reason=f"http_{r.status_code}"
            )
        try:
            body = r.json()
        except ValueError:
            return TokenCapabilities(reason="permissions_field_missing")
        perms = body.get("permissions") if isinstance(body, dict) else None
        if not isinstance(perms, dict):
            return TokenCapabilities(reason="permissions_field_missing")
        return _map_permissions_to_capabilities(perms)

    def resolve_viewer_login(
        self, project: ProjectConfig, token: str
    ) -> ViewerIdentity:
        """Resolve which account `token` authenticates as via
        `GET /user`.

        Failure modes are returned (not raised) so callers can degrade
        gracefully. See `ViewerIdentity.reason` for the stable failure
        identifiers:

        - 401                        -> `"bad_credentials"`
        - other non-2xx              -> `"http_{status}"`
        - non-JSON body or missing
          `login` field              -> `"identity_field_missing"`
        - transport failure          -> `"network_error"`
        """
        try:
            with _client(token) as client:
                r = client.get("/user")
        except httpx.HTTPError:
            return ViewerIdentity(reason="network_error")
        if r.status_code == 401:
            return ViewerIdentity(reason="bad_credentials")
        if not r.is_success:
            return ViewerIdentity(reason=f"http_{r.status_code}")
        try:
            body = r.json()
        except ValueError:
            return ViewerIdentity(reason="identity_field_missing")
        if not isinstance(body, dict) or not body.get("login"):
            return ViewerIdentity(reason="identity_field_missing")
        return ViewerIdentity(
            login=body["login"],
            display_name=body.get("name"),
            provider="github",
        )

    def list_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> tuple[list[Ticket], bool]:
        """List issues for a repository.

        `filters.states`, when non-empty, takes precedence over
        `filters.status` entirely (including `status == "any"`): the
        coarse open/closed/all API state is derived from the requested
        native values (since GitHub Search / `/issues` can't express
        `state_reason` set-membership directly) and returned items are
        further filtered client-side on exact `(state, state_reason)`
        match. Unknown values raise `ValueError` (via
        `_split_github_status`) pointing back to `list_ticket_statuses`.

        `filters.area_path` is an Azure-DevOps-only concept (`System.AreaPath`)
        and raises `ValueError` on this provider rather than being silently
        ignored.

        `filters.board_column` (ticket #118) takes a dedicated path — see
        `_list_tickets_by_board_column` — instead of the search/`/issues`
        REST endpoints below: it resolves the logical column against
        `project.board`, runs a single Projects-v2 GraphQL items query,
        and applies labels/not_labels/assignee/states/status client-side.
        """
        if filters and filters.area_path:
            raise ValueError(
                "area_path is not supported on GitHub — it is an Azure DevOps "
                "System.AreaPath filter"
            )
        _validate_limit(filters.limit)
        if filters and filters.board_column:
            return self._list_tickets_by_board_column(project, token, filters)
        per_page = min(max(1, filters.limit), 100)
        # Normalize `not_labels=[]` (truthy-but-empty containers) to "not set".
        if not filters.not_labels:
            filters.not_labels = []
        # Validate up front — even on the cheap `/issues` path — so an
        # unrecognised native value raises before any HTTP call.
        state_pairs = _github_states_pairs(filters.states) if filters.states else None
        with _client(token) as client:
            if filters.search or _requires_search(filters):
                items = _list_via_search(client, project, filters)
                has_more = len(items) >= per_page
                filtered = [it for it in items if "pull_request" not in it]
                if state_pairs is not None:
                    filtered = [
                        it for it in filtered
                        if _github_item_matches_states(it, state_pairs)
                    ]
                return [_map_issue(it) for it in filtered], has_more
            else:
                if state_pairs is not None:
                    state_param = _github_coarse_state(state_pairs)
                else:
                    state_param = filters.status if filters.status in ("open", "closed") else "all"
                base_params: dict[str, Any] = {
                    "per_page": per_page,
                    "state": state_param,
                    "sort": filters.sort_by,
                    "direction": filters.sort_order,
                }
                if filters.labels:
                    base_params["labels"] = ",".join(filters.labels)
                if filters.assignee:
                    base_params["assignee"] = filters.assignee
                # Paginate until we have `limit` real issues (filtering
                # out mixed-in PRs client-side) or the API runs dry.
                collected: list[Any] = []
                page = 1
                last_raw_page_full = False
                while len(collected) < filters.limit:
                    params = {**base_params, "page": page}
                    r = client.get(f"{_repo_path(project)}/issues", params=params)
                    try:
                        _check(r)
                    except GitHubError as exc:
                        # GitHub returns 422 when the assignee filter value is
                        # not a valid user.  Treat this as "no matching issues"
                        # rather than a hard error, mirroring the search path
                        # which returns empty results for unknown assignees.
                        if exc.status == 422 and filters.assignee:
                            return [], False
                        raise
                    raw_page = r.json()
                    last_raw_page_full = len(raw_page) >= per_page
                    # The /issues endpoint includes PRs; skip them.
                    for it in raw_page:
                        if "pull_request" not in it:
                            if state_pairs is not None and not _github_item_matches_states(it, state_pairs):
                                continue
                            collected.append(it)
                            if len(collected) >= filters.limit:
                                break
                    if not last_raw_page_full:
                        break
                    page += 1
                has_more = len(collected) >= filters.limit and last_raw_page_full
                return [_map_issue(it) for it in collected[: filters.limit]], has_more

    def _list_tickets_by_board_column(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> tuple[list[Ticket], bool]:
        """Dedicated `filters.board_column` listing path (ticket #118).

        Resolves the logical column against `project.board`, then runs a
        single (paginated) Projects-v2 GraphQL items query — instead of
        the search/`/issues` REST paths `list_tickets` otherwise uses —
        keeping only items whose status field matches the resolved
        native column and whose content is an `Issue` (PRs/DraftIssues
        are dropped, same invariant as the REST paths). The remaining
        cheap filters (`labels`/`not_labels`/`assignee`/`states`/
        `status`) are then applied client-side, and results are sorted
        and paginated per `sort_by`/`sort_order`/`limit`.
        """
        if filters.search:
            raise ValueError(
                "board_column cannot be combined with search — the "
                "board-column path runs a dedicated GitHub Projects v2 "
                "GraphQL query, not the search/`/issues` REST paths"
            )
        board = project.board
        if board is None:
            raise ValueError(
                f"project {project.id!r} has no 'board' configuration — "
                f"board_column requires one"
            )
        binding = board.binding
        if binding.kind != "github-projects-v2":
            raise ValueError(
                f"project {project.id!r} board binding is {binding.kind!r}, "
                f"not 'github-projects-v2' — board_column filtering is "
                f"GitHub-only"
            )
        columns_lower = {c.lower() for c in board.columns}
        if filters.board_column.lower() not in columns_lower:
            raise ValueError(
                f"board_column {filters.board_column!r} is not one of "
                f"this project's board columns {board.columns!r}"
            )
        if not binding.owner or not binding.project_number:
            raise ValueError(
                f"project {project.id!r} board binding is missing "
                f"'owner' and/or 'project_number' — both are required to "
                f"resolve a GitHub Projects v2 board"
            )
        native_column = board.resolve(filters.board_column)
        target = native_column.lower()
        state_pairs = _github_states_pairs(filters.states) if filters.states else None
        not_labels = filters.not_labels or []
        # Projects v2 boards can span multiple repos under the same owner;
        # match case-insensitively since GitHub owner logins/repo names
        # are themselves case-insensitive.
        expected_repo = f"{project.owner}/{project.repo}".lower()

        matched: list[Ticket] = []
        after: str | None = None
        with _client(token) as client:
            while True:
                variables = {
                    "owner": binding.owner,
                    "number": binding.project_number,
                    "fieldName": binding.status_field,
                    "after": after,
                }
                project_v2 = _fetch_projects_v2_via_graphql(
                    client,
                    owner=binding.owner,
                    project_number=binding.project_number,
                    org_query=_BOARD_ITEMS_ORG_QUERY,
                    user_query=_BOARD_ITEMS_USER_QUERY,
                    variables=variables,
                )
                items = project_v2.get("items") or {}
                for node in items.get("nodes") or []:
                    content = node.get("content") or {}
                    if content.get("__typename") != "Issue":
                        continue  # drop PRs / DraftIssues
                    # The board can span repos beyond this project's own
                    # owner/repo; drop anything that isn't actually ours
                    # (case-insensitive: GitHub owner/repo names are).
                    repo_name = (content.get("repository") or {}).get("nameWithOwner") or ""
                    if repo_name.lower() != expected_repo:
                        continue
                    field_value = node.get("fieldValueByName") or {}
                    option_name = (field_value.get("name") or "").lower()
                    if option_name != target:
                        continue
                    ticket = _map_graphql_issue_content(content)
                    if filters.labels and not set(filters.labels).issubset(
                        ticket.labels
                    ):
                        continue
                    if not_labels and set(not_labels) & set(ticket.labels):
                        continue
                    if filters.assignee and filters.assignee.lower() not in {
                        a.lower() for a in ticket.assignees
                    }:
                        continue
                    if state_pairs is not None:
                        pseudo_raw = {
                            "state": "open" if ticket.status == "open" else "closed",
                            "state_reason": (
                                None if ticket.status == "open"
                                else ticket.status.split(":", 1)[1]
                            ),
                        }
                        if not _github_item_matches_states(pseudo_raw, state_pairs):
                            continue
                    elif filters.status == "open" and ticket.status != "open":
                        continue
                    elif filters.status == "closed" and ticket.status == "open":
                        continue
                    matched.append(ticket)
                page_info = items.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")

        sort_attr = "updated_at" if filters.sort_by == "updated" else "created_at"
        matched.sort(key=lambda t: getattr(t, sort_attr), reverse=filters.sort_order == "desc")
        has_more = len(matched) > filters.limit
        return matched[: filters.limit], has_more

    def get_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        *,
        include_relations: bool = True,
        include_custom_fields: bool = False,
    ) -> tuple[Ticket, list[Comment], list[Relation] | None, bool | None]:
        """Fetch a single ticket with its comments and (optionally) relations.

        Returns `(ticket, comments, relations, relations_truncated)`.
        When `include_relations` is False, skips the extra relation API
        calls and returns `([], None)` for the relation fields.
        `truncated=None` signals "skipped"; `truncated=False` signals
        "fetched but empty".  `relations` is always a list (never `None`).

        When `include_custom_fields` is `True`, `ticket.custom_fields` is
        populated from the configured `github-projects-v2` board binding
        (`project.board.binding`): the issue's `fieldValues` on that
        board's item, keyed by field name (ticket #123). No board
        binding configured (or the binding isn't `github-projects-v2`,
        or is missing `owner`/`project_number`) -> `custom_fields` stays
        `None` (never raises on read — "not applicable" semantics).
        Binding configured but the issue has no item on that project ->
        `custom_fields = {}`.

        `ticket.milestone` (ticket #151) is populated from the same bound
        board's *iteration* field (see `GithubProjectsV2Binding.iteration_field`)
        — GitHub has no native issue-milestone concept in this surface.
        No board bound -> `milestone` stays `None` and no extra GraphQL
        call is issued. Board bound but the issue has no item on that
        project -> `milestone = None` (not an error). When a board IS
        bound, the `projectItems` query runs on every read regardless of
        `include_custom_fields`; if both are requested, the single query
        result is reused for both rather than double-querying.

        `ticket.parent_id` (ticket #151) is a pure projection over the
        `parent` relation `_fetch_relations` already computes below —
        populated only when `include_relations=True`.
        """
        with _client(token) as client:
            r = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"ticket '{project.id}#{ticket_id}' not found"
                    ) from exc
                raise
            issue_raw = r.json()
            ticket = _map_issue(issue_raw)
            board = project.board
            binding = board.binding if board is not None else None
            board_bound = (
                binding is not None
                and binding.kind == "github-projects-v2"
                and binding.owner
                and binding.project_number
            )
            if board_bound:
                _populate_board_fields(
                    client,
                    ticket,
                    repo_owner=project.owner,
                    repo=project.repo,
                    issue_number=int(ticket_id),
                    binding=binding,
                    include_custom_fields=include_custom_fields,
                )
            elif include_custom_fields:
                ticket.custom_fields = None
            c = client.get(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                params={"per_page": 100},
            )
            _check(c)
            comments = [_map_comment(it) for it in c.json()]
            if include_relations:
                relations, truncated = _fetch_relations(
                    client, project, ticket_id, issue_raw, token=token,
                )
                ticket.parent_id = _extract_parent_id(relations)
            else:
                relations, truncated = [], None
        return ticket, comments, relations, truncated

    def create_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        title: str,
        body: str,
        labels: list[str],
        assignees: list[str],
        *,
        status: Status | None = None,
        custom_fields: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        milestone: Any = _UNSET,
    ) -> Ticket:
        """Create an issue with the project's AI-generated attribution marker.

        Marker policy (ticket Seretos/agent-marketplace#15):
          - The body prefix (`#<ai_generated>\\n\\n`, per `project.auto_labels`)
            is the canonical source of truth and is always applied (idempotent).
          - The `ai_generated` LABEL is best-effort. If the caller cannot
            create or apply the label (typically tokens without
            `push` / `triage` on the target repo), the label is dropped
            from the POST payload so the issue is still created. Mode A
            (silent label-drop in the response despite a successful POST)
            is detected after the call and logged.

        Optional `status` (ticket #42) accepts the same vocabulary as
        `update_ticket.status`. The GitHub `POST /issues` endpoint
        always creates in `open` state, so non-`open` requests are
        landed via a follow-up PATCH inside this method — the agent
        sees one logical call.

        A non-empty `custom_fields` requires a `github-projects-v2` board
        binding configured on `project.board` (ticket #123): after the
        issue is created, it's added to the bound board via
        `addProjectV2ItemById`, then each `custom_fields` entry is
        written via `updateProjectV2ItemFieldValue` (single-select
        values are resolved to their live `optionId`, matched
        case-insensitively). No board binding configured -> `ValueError`
        naming the missing config. `None`/`{}` is a silent no-op so
        existing callers are unaffected.

        Optional `idempotency_key` (ticket #150): a retried call with the
        same key (scoped to this project) returns the ticket created by
        the first successful call instead of creating a duplicate, with
        `idempotent_replay=True` set on the result. Only `title`/`body`
        are compared across calls with the same key — `labels`,
        `assignees`, `status`, and `custom_fields` are ignored for
        conflict detection. A retry with the same key but a different
        `title`/`body` raises `IdempotencyConflict`. `None`/`""` (the
        default) disables idempotency entirely — behaviour is unchanged.

        Optional `milestone` (ticket #151, keyword-only): an iteration
        title, written to the bound board's `binding.iteration_field` via
        the same board-write machinery as `custom_fields`. Requires a
        `github-projects-v2` board bound AND `iteration_field` configured
        on it — the write path can't auto-detect the iteration field by
        type the way `get_ticket`'s read path does. Missing/unconfigured
        -> `ValueError` naming the missing config. Uses the `_UNSET`
        sentinel default so "not provided" issues no milestone write at
        all. A board-write failure after the issue is created raises
        `PartialTicketCreateError`, same as `custom_fields`.
        """
        if not title or not title.strip():
            raise ValueError("title must not be blank")
        if idempotency_key:
            replay = _idempotency.lookup(
                (project.provider, project.id),
                idempotency_key,
                {"title": title, "body": body},
            )
            if replay is not None:
                return replay
        binding = None
        if custom_fields:
            board = project.board
            binding = board.binding if board is not None else None
            if (
                binding is None
                or binding.kind != "github-projects-v2"
                or not binding.owner
                or not binding.project_number
            ):
                raise ValueError(
                    f"custom_fields was provided but project {project.id!r} "
                    f"has no 'github-projects-v2' board configured with "
                    f"'owner' and 'project_number' — add one to "
                    f"projects.yml before calling create_ticket with "
                    f"custom_fields"
                )
        if milestone is not _UNSET:
            board = project.board
            milestone_binding = board.binding if board is not None else None
            if (
                milestone_binding is None
                or milestone_binding.kind != "github-projects-v2"
                or not milestone_binding.owner
                or not milestone_binding.project_number
                or not milestone_binding.iteration_field
            ):
                raise ValueError(
                    f"milestone was provided but project {project.id!r} "
                    f"has no 'github-projects-v2' board configured with "
                    f"'owner', 'project_number', and 'iteration_field' — "
                    f"add one to projects.yml before calling create_ticket "
                    f"with milestone"
                )
            binding = milestone_binding
        # Deduplicate while preserving order, ensure ai-generated is present.
        ai_generated_label = project.auto_labels.ai_generated
        board_create_labels = (
            project.board.auto_label_names_on_create()
            if project.board is not None
            else []
        )
        merged = list(
            dict.fromkeys([*labels, ai_generated_label, *board_create_labels])
        )
        prefixed_body = ensure_body_prefix(body, markers=_marker_set(project))
        # Validate `status` up-front so an invalid value rejects before
        # the POST commits an issue we'd then have to delete or close.
        patch_state, patch_state_reason = _split_github_status(status)
        with _client(token) as client:
            _assert_labels_exist(client, project, labels)
            label_ok = _ensure_label_best_effort(
                client, project, ai_generated_label, "generated"
            )
            # Best-effort ensure each board `on_create` label too (ticket
            # #154), same non-blocking lifecycle as the AI marker: a
            # label that can't be created is dropped from the payload
            # rather than aborting the create.
            dropped_labels: set[str] = set() if label_ok else {ai_generated_label}
            for name in board_create_labels:
                if not _ensure_label_best_effort(client, project, name, "board"):
                    dropped_labels.add(name)
            payload: dict[str, Any] = {
                "title": title,
                "body": prefixed_body,
            }
            payload_labels = [lbl for lbl in merged if lbl not in dropped_labels]
            if payload_labels:
                payload["labels"] = payload_labels
            if assignees:
                payload["assignees"] = assignees
            r = client.post(f"{_repo_path(project)}/issues", json=payload)
            _check(r)
            raw = r.json()
            if label_ok and not _label_present(raw, ai_generated_label):
                log.warning(
                    "ticket #%s created on %s/%s without '%s' label "
                    "(GitHub silently dropped it — caller likely lacks "
                    "triage permission); body-prefix marker remains",
                    raw.get("number"), project.owner, project.repo,
                    ai_generated_label,
                )
            # Follow-up PATCH for non-`open` initial status.
            if patch_state is not None and patch_state != "open":
                patch_payload: dict[str, Any] = {"state": patch_state}
                if patch_state_reason is not None:
                    patch_payload["state_reason"] = patch_state_reason
                number = raw.get("number")
                pr = client.patch(
                    f"{_repo_path(project)}/issues/{number}",
                    json=patch_payload,
                )
                _check(pr)
                raw = pr.json()
            if custom_fields:
                content_id = raw.get("node_id")
                if not content_id:
                    raise GitHubError(
                        500,
                        f"created issue #{raw.get('number')} payload missing "
                        f"'node_id'; cannot write custom_fields",
                    )
                try:
                    _write_custom_fields_to_board(
                        client, binding, content_id, custom_fields,
                    )
                except GitHubError as exc:
                    number = raw.get("number")
                    url = raw.get("html_url")
                    raise PartialTicketCreateError(
                        exc.status,
                        f"issue #{number} ({url}) was created, but writing "
                        f"custom_fields to its Projects v2 board failed: "
                        f"{exc.message}",
                        issue_number=number,
                        issue_url=url,
                        issue_node_id=content_id,
                    ) from exc
            if milestone is not _UNSET:
                content_id = raw.get("node_id")
                if not content_id:
                    raise GitHubError(
                        500,
                        f"created issue #{raw.get('number')} payload missing "
                        f"'node_id'; cannot write milestone",
                    )
                try:
                    _write_milestone_to_board(client, binding, content_id, milestone)
                except GitHubError as exc:
                    number = raw.get("number")
                    url = raw.get("html_url")
                    raise PartialTicketCreateError(
                        exc.status,
                        f"issue #{number} ({url}) was created, but writing "
                        f"milestone to its Projects v2 board failed: "
                        f"{exc.message}",
                        issue_number=number,
                        issue_url=url,
                        issue_node_id=content_id,
                    ) from exc
            ticket = _map_issue(raw)
            if idempotency_key:
                _idempotency.record(
                    (project.provider, project.id),
                    idempotency_key,
                    {"title": title, "body": body},
                    ticket,
                )
            return ticket

    def update_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        status: Status | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
        custom_fields: dict[str, Any] | None = None,
        milestone: Any = _UNSET,
    ) -> Ticket:
        """Update an issue, optionally also writing `custom_fields` to its
        bound Projects v2 board (ticket #145) — the update-side
        counterpart to `create_ticket`'s `custom_fields` support.

        A non-empty `custom_fields` requires the same `github-projects-v2`
        board binding as `create_ticket` (`project.board.binding`, with
        `owner` and `project_number` set); an invalid/missing binding
        raises `ValueError` before any write happens. `None`/`{}` is a
        silent no-op, matching `create_ticket`'s contract.

        The board write reuses `_write_custom_fields_to_board`, the same
        helper `create_ticket` uses — `addProjectV2ItemById` is
        idempotent, so no separate "resolve existing item id" step is
        needed. Unlike `create_ticket`, a failed board write here raises
        a plain `GitHubError` (not `PartialTicketCreateError`): the REST
        PATCH (if any) has already landed by the time the board write is
        attempted, so there's no "was the issue even created" ambiguity
        to name a partial-failure type for.

        A non-empty `custom_fields` write can cascade REST-visible issue
        state server-side (e.g. a board automation that closes the issue
        when its Status column moves to "Done"), and that cascade runs
        asynchronously on GitHub's side — there is no API to await it.
        Whenever `custom_fields` is non-empty (or a reopen resets the
        board column — see the `status` paragraph below), this method
        re-GETs the issue after the board write and maps that fresh
        snapshot instead of the pre-write REST payload (ticket #178),
        polling a bounded number of times to give a fast cascade a chance
        to land. The polled REST `state`/`state_reason` is a
        **best-effort** value, not a guarantee: if the cascade lands
        after the poll budget is exhausted, the returned `Ticket`
        reflects the last snapshot observed and MAY LAG the eventual
        settled state (open/unchanged rather than the closed:completed
        the automation will still produce moments later). Callers that
        need guaranteed post-cascade REST state must issue a follow-up
        `get_ticket` after `update_ticket` returns.

        Independently of that REST-state staleness, on the SAME
        board-write path this method also reads back `Ticket.custom_fields`
        and `Ticket.milestone` from the board (ticket #185): after a
        `custom_fields` write or a reopen board-column reset, the
        returned `custom_fields`/`milestone` are populated from the same
        Projects-v2 `projectItems` GraphQL read `get_ticket` uses, so the
        return matches an immediate
        `get_ticket(..., include_custom_fields=True)` — at the cost of
        one extra round trip, confined to this path. A `milestone`-only
        board write does not trigger any of this (neither the state poll
        nor the board read-back): the iteration field has no REST-visible
        effect on the issue, so the returned `custom_fields` and
        `milestone` both stay `None` — call `get_ticket` for those. The
        pure-REST path (no `custom_fields`, no `milestone`, no reopen
        reset) makes no extra requests either and likewise returns
        `custom_fields`/`milestone` as `None`.

        Optional `milestone` (ticket #151, keyword-only): mirrors
        `create_ticket`'s `milestone=` — requires `github-projects-v2` +
        `iteration_field` configured (`ValueError` otherwise), writes via
        `_write_milestone_to_board`. `milestone=None` clears the
        iteration field (`clearProjectV2ItemFieldValue`); `milestone=`
        omitted (`_UNSET`) issues no milestone write at all. A failed
        board write raises a plain `GitHubError`, same as `custom_fields`.

        `status` normally changes only the REST issue state (`state`/
        `state_reason` via the PATCH below) and does not touch the
        Projects-v2 board's Status field. The one exception (ticket
        #175): reopening a ticket (`status="open"`) that was previously
        closed **and** whose bound board has moved it into a terminal
        column resets that column back to the board's first configured
        column (`project.board.columns[0]`, resolved through
        `Board.resolve` so a `binding.map` alias is honored). This only
        fires when a `github-projects-v2` board is configured (an
        `azure-boards` binding, or no board at all, is a silent no-op —
        Azure's status/column model is out of scope here) and only when
        the caller did **not** already pass an explicit value for the
        board's `status_field` via `custom_fields` in the same call —
        an explicit caller value always wins and no reset write happens
        on top of it. A failed reset write raises `GitHubError`, same
        as any other board write in this method. Per the `custom_fields`
        paragraph above (ticket #185), this reset write also puts
        `update_ticket` on the board read-back path: the returned
        `Ticket.custom_fields` reflects the reset first-column value
        (and `Ticket.milestone` is populated too), even when the caller
        passed no `custom_fields` of their own.
        """
        _validate_label_lists(labels_add, labels_remove)
        binding = None
        if custom_fields:
            board = project.board
            binding = board.binding if board is not None else None
            if (
                binding is None
                or binding.kind != "github-projects-v2"
                or not binding.owner
                or not binding.project_number
            ):
                raise ValueError(
                    f"custom_fields was provided but project {project.id!r} "
                    f"has no 'github-projects-v2' board configured with "
                    f"'owner' and 'project_number' — add one to "
                    f"projects.yml before calling update_ticket with "
                    f"custom_fields"
                )
        if milestone is not _UNSET:
            board = project.board
            milestone_binding = board.binding if board is not None else None
            if (
                milestone_binding is None
                or milestone_binding.kind != "github-projects-v2"
                or not milestone_binding.owner
                or not milestone_binding.project_number
                or not milestone_binding.iteration_field
            ):
                raise ValueError(
                    f"milestone was provided but project {project.id!r} "
                    f"has no 'github-projects-v2' board configured with "
                    f"'owner', 'project_number', and 'iteration_field' — "
                    f"add one to projects.yml before calling update_ticket "
                    f"with milestone"
                )
            binding = milestone_binding
        # ticket #175: on a closed→open transition, reset a terminal
        # board column back to the board's first column. Only fires for
        # a `github-projects-v2` board — an `azure-boards` binding, or no
        # board at all, is a silent no-op here — and only when the
        # caller hasn't already passed an explicit value for the
        # board's status_field via `custom_fields` in this same call
        # (checked below, once `custom_fields` is known to be valid).
        reopen_binding: Any = None
        if (
            project.board is not None
            and project.board.binding.kind == "github-projects-v2"
        ):
            reopen_binding = project.board.binding
        with _client(token) as client:
            r0 = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
            try:
                _check(r0)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"ticket '{project.id}#{ticket_id}' not found"
                    ) from exc
                raise
            current = r0.json()
            current_labels = {lbl["name"] for lbl in (current.get("labels") or [])}
            current_assignees = {a["login"] for a in (current.get("assignees") or [])}
            markers = _marker_set(project)

            def _reget_issue(
                prev_state: str | None, board_binding: Any = None
            ) -> Ticket:
                # A board write via `_write_custom_fields_to_board` can
                # cascade REST-visible issue state server-side (e.g. a
                # Status:"Done" column write auto-closing the issue via a
                # workflow) — but that cascade is ASYNC, so a single
                # re-GET can still observe the pre-cascade snapshot
                # (ticket #178). Poll a bounded number of times, stopping
                # as soon as `state` differs from `prev_state` (the
                # cascade landed). This is a BEST-EFFORT fast path, not a
                # guarantee: GitHub exposes no API to await a pending
                # Projects-v2 workflow, so if the cap is reached without a
                # change, return the last snapshot fetched as-is. That
                # exhausted-poll return is the correct outcome both when
                # the write legitimately doesn't cascade (e.g. a
                # custom_fields write that isn't bound to an auto-close
                # workflow) AND when it does cascade but hasn't landed
                # yet — in the latter case the returned snapshot's REST
                # `status` is documented as potentially stale (see the
                # #178 paragraph in `update_ticket`'s docstring); it is
                # never an error, and callers needing the settled REST
                # state must re-`get_ticket`.
                last: dict[str, Any] | None = None
                for attempt in range(_REGET_POLL_MAX_ATTEMPTS):
                    rr = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
                    _check(rr)
                    last = rr.json()
                    if last.get("state") != prev_state:
                        break
                    if attempt < _REGET_POLL_MAX_ATTEMPTS - 1:
                        _reget_sleep(_REGET_POLL_BACKOFFS[attempt])
                ticket = _map_issue(last)
                # ticket #185: on a board-write path (`board_binding` set
                # whenever `custom_fields` was written OR a reopen reset
                # the board column — see the two call sites below), also
                # read back `custom_fields`/`milestone` from the SAME
                # Projects-v2 GraphQL read `get_ticket` uses, so this
                # return matches an immediate
                # `get_ticket(..., include_custom_fields=True)`. This is
                # ONE extra round trip, confined to the board-write path —
                # a milestone-only write never reaches `_reget_issue` at
                # all (see `update_ticket`'s docstring), so it still
                # returns `custom_fields=None`/`milestone=None`.
                if board_binding is not None:
                    _populate_board_fields(
                        client,
                        ticket,
                        repo_owner=project.owner,
                        repo=project.repo,
                        issue_number=int(ticket_id),
                        binding=board_binding,
                        include_custom_fields=True,
                    )
                return ticket

            if labels_add:
                _assert_labels_exist(client, project, labels_add)

            new_labels = set(current_labels)
            if labels_add:
                new_labels.update(labels_add)
            if labels_remove:
                new_labels.difference_update(labels_remove)

            # If this ticket wasn't created by us, mark it as AI-modified.
            # Label application is best-effort (see ticket #15): if the
            # caller can't create or apply the label, proceed without it
            # rather than blocking the legitimate update.
            will_be_ai_generated = project.auto_labels.ai_generated in current_labels
            if not will_be_ai_generated:
                ai_modified_label = project.auto_labels.ai_modified
                if ai_modified_label not in new_labels:
                    if _ensure_label_best_effort(
                        client, project, ai_modified_label, "modified"
                    ):
                        new_labels.add(ai_modified_label)
                else:
                    new_labels.add(ai_modified_label)

            # Board-column-dependent auto-labels (ticket #154), additive
            # and best-effort like the AI markers above.
            if project.board is not None:
                for name in project.board.auto_label_names_on_update():
                    if name not in new_labels and _ensure_label_best_effort(
                        client, project, name, "board"
                    ):
                        new_labels.add(name)

                # `on_move_to`: fires unconditionally (no prior-column
                # diff) whenever `custom_fields` carries a new value for
                # the board's status field. GitHub-only for now — Azure
                # and GitLab have no equivalent board-write path here.
                if custom_fields:
                    status_field = binding.status_field  # type: ignore[union-attr]
                    move_value: str | None = None
                    for key, value in custom_fields.items():
                        if key.lower() == status_field.lower():
                            if isinstance(value, str):
                                move_value = value
                            break
                    if move_value is not None:
                        for name in project.board.auto_label_names_for_move(
                            move_value
                        ):
                            if name not in new_labels and _ensure_label_best_effort(
                                client, project, name, "board"
                            ):
                                new_labels.add(name)

            new_assignees = set(current_assignees)
            if assignees_add:
                new_assignees.update(assignees_add)
            if assignees_remove:
                new_assignees.difference_update(assignees_remove)

            payload: dict[str, Any] = {}
            state: str | None = None
            if title is not None:
                payload["title"] = title
            if body is not None:
                # Re-stamp the body's `#ai-*` marker so it matches the
                # resource's label state after this update (ticket #44).
                # Caller should NOT prepend the marker themselves; if
                # they do, we strip + re-add the correct one.
                payload["body"] = apply_body_marker(
                    body, will_be_ai_generated=will_be_ai_generated, markers=markers,
                )
            if status is not None:
                # New provider-native string API (see ticket #7).
                # Accepted values: open / closed / closed:completed /
                # closed:not_planned. Legacy 3-value enum no longer
                # accepted — see `_split_github_status` for the error
                # message agents see on unknown values.
                state, state_reason = _split_github_status(status)
                if state is not None:
                    payload["state"] = state
                if state_reason is not None:
                    payload["state_reason"] = state_reason
            if new_labels != current_labels:
                payload["labels"] = sorted(new_labels)
            if new_assignees != current_assignees:
                payload["assignees"] = sorted(new_assignees)

            # ticket #175: reopening (closed→open) a ticket whose bound
            # `github-projects-v2` board has it parked in a terminal
            # column resets that column back to the board's first
            # configured column — unless the caller already passed an
            # explicit value for the board's status_field via
            # `custom_fields` in this same call, in which case the
            # caller's value wins and no reset write happens on top of
            # it. Reuses the same case-insensitive key scan as the
            # `on_move_to` auto-label lookup above to detect an explicit
            # override.
            should_reset_board_column = False
            if (
                reopen_binding is not None
                and current.get("state") == "closed"
                and state == "open"
            ):
                status_field = reopen_binding.status_field
                explicit_override = False
                if custom_fields:
                    for key in custom_fields:
                        if key.lower() == status_field.lower():
                            explicit_override = True
                            break
                should_reset_board_column = not explicit_override

            if not payload:
                # Nothing to do via REST — still honor a pending board
                # write, then return the current state.
                if custom_fields:
                    content_id = current.get("node_id")
                    if not content_id:
                        raise GitHubError(
                            500,
                            f"ticket '{project.id}#{ticket_id}' payload "
                            f"missing 'node_id'; cannot write custom_fields",
                        )
                    _write_custom_fields_to_board(
                        client, binding, content_id, custom_fields,
                    )
                if milestone is not _UNSET:
                    content_id = current.get("node_id")
                    if not content_id:
                        raise GitHubError(
                            500,
                            f"ticket '{project.id}#{ticket_id}' payload "
                            f"missing 'node_id'; cannot write milestone",
                        )
                    _write_milestone_to_board(client, binding, content_id, milestone)
                if custom_fields:
                    return _reget_issue(current.get("state"), binding or reopen_binding)
                return _map_issue(current)

            r = client.patch(f"{_repo_path(project)}/issues/{ticket_id}", json=payload)
            _check(r)
            raw = r.json()
            if custom_fields:
                content_id = raw.get("node_id")
                if not content_id:
                    raise GitHubError(
                        500,
                        f"ticket '{project.id}#{ticket_id}' payload missing "
                        f"'node_id'; cannot write custom_fields",
                    )
                _write_custom_fields_to_board(
                    client, binding, content_id, custom_fields,
                )
            if milestone is not _UNSET:
                content_id = raw.get("node_id")
                if not content_id:
                    raise GitHubError(
                        500,
                        f"ticket '{project.id}#{ticket_id}' payload missing "
                        f"'node_id'; cannot write milestone",
                    )
                _write_milestone_to_board(client, binding, content_id, milestone)
            if should_reset_board_column:
                content_id = raw.get("node_id")
                if not content_id:
                    raise GitHubError(
                        500,
                        f"ticket '{project.id}#{ticket_id}' payload missing "
                        f"'node_id'; cannot reset board column on reopen",
                    )
                reset_value = project.board.resolve(project.board.columns[0])  # type: ignore[union-attr]
                _write_custom_fields_to_board(
                    client,
                    reopen_binding,
                    content_id,
                    {reopen_binding.status_field: reset_value},
                )
            if custom_fields or should_reset_board_column:
                # Use the PRE-write state (`current`, captured before the
                # PATCH above) as the poll's baseline — not `raw["state"]`
                # — so a state-changing PATCH in this same call (e.g. an
                # explicit `status=` reopen) doesn't mask detection of the
                # async board-cascade state change layered on top of it.
                return _reget_issue(current.get("state"), binding or reopen_binding)
            return _map_issue(raw)

    def bulk_update_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_ids: list[str],
        *,
        title: str | None = None,
        body: str | None = None,
        status: Status | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
        custom_fields: dict[str, Any] | None = None,
    ) -> list[BulkTicketResult]:
        """Apply the same update to each id in `ticket_ids` (ticket #149).

        Loops over `ticket_ids`, calling `update_ticket` once per id and
        catching `(ProviderError, ValueError)` around each call so one
        failing id (e.g. 404, invalid status) does not abort the rest of
        the batch. Results preserve `ticket_ids` order 1:1 — duplicates
        are not deduped, each occurrence is updated independently. An
        empty `ticket_ids` returns `[]` without any HTTP call.
        """
        results: list[BulkTicketResult] = []
        for ticket_id in ticket_ids:
            try:
                ticket = self.update_ticket(
                    project,
                    token,
                    ticket_id,
                    title=title,
                    body=body,
                    status=status,
                    labels_add=labels_add,
                    labels_remove=labels_remove,
                    assignees_add=assignees_add,
                    assignees_remove=assignees_remove,
                    custom_fields=custom_fields,
                )
            except (ProviderError, ValueError) as exc:
                results.append(
                    BulkTicketResult(ticket_id=ticket_id, ticket=None, error=str(exc))
                )
            else:
                results.append(
                    BulkTicketResult(ticket_id=ticket_id, ticket=ticket, error=None)
                )
        return results

    def list_statuses(
        self,
        project: ProjectConfig,  # noqa: ARG002 — kept for provider-agnostic signature
        token: str | None,        # noqa: ARG002 — same
    ) -> StatusSpec:
        """Return the GitHub-static status spec.

        GitHub's state-space is fixed (`open` / `closed`) but we expose
        the `state_reason` distinction through suffix-encoded values so
        agents can choose between "done as planned" vs "not planned"
        terminal states. The state-space is identical for every GitHub
        project, so this is a static return value — no API call.
        """
        return StatusSpec(
            values=["open", "closed:completed", "closed:not_planned"],
            transitions={
                "open": ["closed:completed", "closed:not_planned"],
                # GitHub allows re-closing a closed issue with a different
                # state_reason (e.g. completed → not_planned or vice versa)
                # via a single PATCH call, so both cross-terminal moves are
                # valid transitions (ticket #50).
                "closed:completed": ["open", "closed:not_planned"],
                "closed:not_planned": ["open", "closed:completed"],
            },
            hints={
                "default_open": "open",
                "terminal": ["closed:completed", "closed:not_planned"],
                "terminal_completed": "closed:completed",
                "terminal_declined": "closed:not_planned",
            },
        )

    def list_fields(
        self,
        project: ProjectConfig,  # noqa: ARG002 — kept for provider-agnostic signature
        token: str | None,        # noqa: ARG002 — same
        *,
        work_item_type: str | None = None,  # noqa: ARG002 — same
    ) -> list[FieldSpec]:
        """Return an empty list — GitHub issues have no structured field schema.

        GitHub does not expose a discoverable field vocabulary for issues.
        This stub satisfies the provider-agnostic surface so callers can
        iterate over all providers without special-casing GitHub.
        """
        return []

    def list_board_columns(
        self, project: ProjectConfig, token: str | None,
    ) -> list[BoardColumnSpec]:
        """Resolve `project.board.columns` against the live GitHub
        Projects v2 board (ticket #118).

        Reads the single-select field named `binding.status_field`
        (default `"Status"`) and its `options`, then pairs each logical
        column with its resolved native option (`Board.resolve()`:
        explicit `map` wins, else case-insensitive identity fallback)
        and that option's live `id`.

        Raises `ValueError` when: `project.board` is unset; the binding
        isn't `kind="github-projects-v2"`; the binding is missing
        `owner`/`project_number`; the named status field doesn't exist
        or isn't a single-select field; or a resolved native option
        isn't present among the live board's options.
        """
        board = project.board
        if board is None:
            raise ValueError(
                f"project {project.id!r} has no 'board' configuration — "
                f"add one to projects.yml before calling list_board_columns"
            )
        binding = board.binding
        if binding.kind != "github-projects-v2":
            raise ValueError(
                f"project {project.id!r} board binding is {binding.kind!r}, "
                f"not 'github-projects-v2' — list_board_columns is GitHub-only"
            )
        if not binding.owner or not binding.project_number:
            raise ValueError(
                f"project {project.id!r} board binding is missing "
                f"'owner' and/or 'project_number' — both are required to "
                f"resolve a GitHub Projects v2 board"
            )
        with _client(token) as client:
            project_v2 = _fetch_projects_v2_via_graphql(
                client,
                owner=binding.owner,
                project_number=binding.project_number,
                org_query=_BOARD_COLUMNS_ORG_QUERY,
                user_query=_BOARD_COLUMNS_USER_QUERY,
                variables={
                    "owner": binding.owner,
                    "number": binding.project_number,
                    "fieldName": binding.status_field,
                },
            )
        field = project_v2.get("field")
        live_options = (field or {}).get("options") or []
        if not field or not live_options:
            raise ValueError(
                f"GitHub Projects v2 field {binding.status_field!r} was not "
                f"found (or is not a single-select field) on project "
                f"#{binding.project_number} for owner {binding.owner!r}"
            )
        by_lower_name = {opt["name"].lower(): opt for opt in live_options}
        result: list[BoardColumnSpec] = []
        for col in board.columns:
            native = board.resolve(col)
            opt = by_lower_name.get(native.lower())
            if opt is None:
                available = sorted(o["name"] for o in live_options)
                raise ValueError(
                    f"board column {col!r} resolves to native option "
                    f"{native!r}, which is not present on the live GitHub "
                    f"Projects v2 board (available options: {available})"
                )
            result.append(
                BoardColumnSpec(logical=col, native=native, option_id=opt["id"])
            )
        return result

    def ensure_board_column(
        self, project: ProjectConfig, token: str | None, column_name: str,
    ) -> bool:
        """Idempotently provision `column_name` as an option on the live
        GitHub Projects v2 board's Status field (ticket #192).

        Reads the current `binding.status_field` single-select field and,
        if `column_name` is already present among its options
        (case-insensitively), no-ops and returns `False`. Otherwise adds
        it via an `updateProjectV2Field` mutation that round-trips every
        existing option so no columns are destroyed, and returns `True`.

        GitHub only — the Azure DevOps counterpart is a separate ticket
        (#193). Raises `ValueError` under the same conditions as
        `list_board_columns`: `project.board` is unset; the binding isn't
        `kind="github-projects-v2"`; the binding is missing `owner`/
        `project_number`; or the named status field doesn't exist (or
        isn't a single-select field).
        """
        board = project.board
        if board is None:
            raise ValueError(
                f"project {project.id!r} has no 'board' configuration — "
                f"add one to projects.yml before calling ensure_board_column"
            )
        binding = board.binding
        if binding.kind != "github-projects-v2":
            raise ValueError(
                f"project {project.id!r} board binding is {binding.kind!r}, "
                f"not 'github-projects-v2' — ensure_board_column is GitHub-only"
            )
        if not binding.owner or not binding.project_number:
            raise ValueError(
                f"project {project.id!r} board binding is missing "
                f"'owner' and/or 'project_number' — both are required to "
                f"resolve a GitHub Projects v2 board"
            )
        with _client(token) as client:
            return _ensure_single_select_option(
                client, binding, binding.status_field, column_name,
            )

    def add_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        body: str,
    ) -> Comment:
        if not body or not body.strip():
            raise ValueError("body must not be empty")
        prefixed = ensure_comment_prefix(body, markers=_marker_set(project))
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                json={"body": prefixed},
            )
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"ticket '{project.id}#{ticket_id}' not found"
                    ) from exc
                raise
            return _map_comment(r.json())

    def list_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        limit: int = 30,
        *,
        since: str | None = None,
        page: int = 1,
        order: str = "asc",
    ) -> tuple[list[Comment], bool]:
        """List comments on a ticket (capped at `limit`, max 100 per page).

        Returns `(rows, has_more)`. `since` is forwarded natively
        (`?since=<iso>`). `page` is 1-based.

        Tail-fetch (ticket #47 follow-up): when `order="desc"`,
        `page=1`, and no `since`, the implementation probes the
        pagination Link header to find the last page, fetches from
        the end backwards until `limit` items are collected, and
        returns them newest-first. This makes `order="desc",
        limit=N` actually return the LAST N comments — the recipe
        promised by the ticket body. Without this special case the
        provider would just reverse page 1, which is the OLDEST N
        in reverse order rather than the newest.

        `has_more` semantics:
          - asc / explicit page / since: True when a `rel="next"`
            link exists (more comments after the returned page).
          - desc tail-fetch: True when older pages exist (more
            comments BEFORE the returned tail slice).
        """
        per_page = min(max(1, limit), 100)
        with _client(token) as client:
            if order == "desc" and page == 1 and not since:
                return self._list_comments_tail(
                    client, project, ticket_id,
                    per_page=per_page, limit=limit,
                )
            params: dict[str, Any] = {"per_page": per_page, "page": page}
            if since:
                params["since"] = since
            r = client.get(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                params=params,
            )
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"ticket '{project.id}#{ticket_id}' not found"
                    ) from exc
                raise
            rows = [_map_comment(it) for it in r.json()]
            link = r.headers.get("Link", "") or ""
            has_more = 'rel="next"' in link
            return rows, has_more

    def _list_comments_tail(
        self,
        client: httpx.Client,
        project: ProjectConfig,
        ticket_id: str,
        *,
        per_page: int,
        limit: int,
    ) -> tuple[list[Comment], bool]:
        """Smart-fetch the last `limit` comments newest-first.

        Algorithm: probe page 1 to read the `Link rel="last"` header,
        then fetch from the last page backwards, collecting until we
        have at least `limit` items. Reverse + slice for the final
        return. `has_more` indicates older pages still exist.
        """
        url = f"{_repo_path(project)}/issues/{ticket_id}/comments"
        probe = client.get(url, params={"per_page": per_page, "page": 1})
        try:
            _check(probe)
        except GitHubError as exc:
            if exc.status == 404:
                raise GitHubError(
                    404, f"ticket '{project.id}#{ticket_id}' not found"
                ) from exc
            raise
        link = probe.headers.get("Link", "") or ""
        last_page = _parse_link_last_page(link)
        if last_page is None or last_page <= 1:
            rows = [_map_comment(it) for it in probe.json()]
            rows.reverse()
            return rows, False

        # Multi-page thread: walk backwards from the last page.
        collected_oldest_first: list[Comment] = []
        cur = last_page
        while cur >= 1 and len(collected_oldest_first) < limit:
            r = client.get(url, params={"per_page": per_page, "page": cur})
            _check(r)
            page_rows = [_map_comment(it) for it in r.json()]
            collected_oldest_first = page_rows + collected_oldest_first
            cur -= 1

        # `collected_oldest_first` is ascending. We want the last `limit`
        # items newest-first.
        tail = collected_oldest_first[-limit:]
        tail.reverse()
        # `cur >= 1` means we stopped before reaching page 1 (older pages
        # exist that weren't fetched at all).  `len > limit` means we fetched
        # all remaining pages but collected more items than `limit`, so there
        # are older comments in `collected_oldest_first` that were trimmed by
        # the slice and won't be returned in this batch.
        has_more = cur >= 1 or len(collected_oldest_first) > limit
        return tail, has_more

    def get_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        ticket_id: str | None = None,  # noqa: ARG002 — accepted for cross-provider symmetry
    ) -> Comment:
        """Fetch a single comment by its repo-wide comment id.

        `ticket_id` is accepted for cross-provider signature symmetry
        (GitLab needs it to address a note) but is not used here —
        GitHub comment ids are repo-wide and look up directly.
        """
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
            )
            _check(r)
            return _map_comment(r.json())

    def update_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        body: str,
        ticket_id: str | None = None,  # noqa: ARG002 — accepted for cross-provider symmetry
    ) -> Comment:
        """Update a comment's body, re-stamping the AI-marker.

        Marker policy (ticket #44):
          - If the existing comment body carries the project's configured
            generated marker, the edited body is re-stamped with that same
            marker (we wrote it originally, this is just another AI edit).
          - Otherwise the comment was human-authored — the edited body
            is stamped with the project's configured modified marker to
            mirror the label distinction `update_ticket` makes between
            AI-generated and AI-modified resources.

        Comments don't carry labels, so the body marker is the only
        signal a reader has of authorship — getting it right here is
        important. Costs one extra GET before the PATCH.
        """
        if not body or not body.strip():
            raise ValueError("body must not be empty")
        with _client(token) as client:
            r0 = client.get(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
            )
            try:
                _check(r0)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"comment {comment_id!r} not found in {project.id}"
                    ) from exc
                raise
            current_body = r0.json().get("body") or ""
            markers = _marker_set(project)
            will_be_ai_generated = has_ai_generated_marker(current_body, markers=markers)
            prefixed = apply_body_marker(
                body, will_be_ai_generated=will_be_ai_generated, markers=markers,
            )
            r = client.patch(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
                json={"body": prefixed},
            )
            _check(r)
            return _map_comment(r.json())

    def delete_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        ticket_id: str | None = None,  # noqa: ARG002 — accepted for cross-provider symmetry
    ) -> None:
        """Delete a comment by its id.

        Raises `GitHubError(404, ...)` when the comment does not exist.
        Returns `None` on success (GitHub responds with 204 No Content).
        """
        with _client(token) as client:
            r = client.delete(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
            )
            if r.status_code == 404:
                raise GitHubError(
                    404, f"comment {comment_id!r} not found in {project.id}"
                )
            _check(r)

    # ---------- pull requests ------------------------------------------------

    def list_prs(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: PRFilters,
    ) -> tuple[list[PullRequest], bool]:
        """List pull requests for a project.

        Routing mirrors `list_tickets`: when `labels`, `assignee`, or
        `search` are set, switch from the cheap `/pulls` endpoint to
        `/search/issues` with `is:pr` so the additional filters can be
        expressed as Search qualifiers. The `head`/`base` filters work on
        both paths (Search via the `head:`/`base:` qualifiers).

        Returns `(prs, has_more)`. `has_more` is True when the API returned
        exactly `per_page` results, indicating more pages may exist.
        """
        per_page = min(max(1, filters.limit), 100)
        use_search = bool(
            filters.labels or filters.assignee or filters.search
        )
        with _client(token) as client:
            if use_search:
                qual_parts: list[str] = [
                    "is:pr",
                    f"repo:{project.owner}/{project.repo}",
                ]
                if filters.status in ("open", "closed"):
                    qual_parts.append(f"state:{filters.status}")
                if filters.assignee:
                    qual_parts.append(f"assignee:{filters.assignee}")
                for lbl in filters.labels:
                    qual_parts.append(f"label:{_quote_label(lbl)}")
                if filters.head:
                    qual_parts.append(f"head:{filters.head}")
                if filters.base:
                    qual_parts.append(f"base:{filters.base}")
                pieces = [filters.search] if filters.search else []
                pieces.extend(qual_parts)
                q = " ".join(pieces)
                r = client.get("/search/issues", params={"q": q, "per_page": per_page})
                _check(r)
                stubs = r.json().get("items", [])
                # Back-fill: search items are issue-shaped stubs that lack PR
                # fields (head/base/mergeable_state/draft). Fetch the full PR
                # payload for each stub so callers always get complete objects.
                full_items: list[dict] = []
                for stub in stubs:
                    number = stub.get("number")
                    detail = client.get(f"{_repo_path(project)}/pulls/{number}")
                    _check(detail)
                    full_items.append(detail.json())
                has_more = len(full_items) >= per_page
                return [_map_pr(it) for it in full_items], has_more
            else:
                params: dict[str, Any] = {
                    "per_page": per_page,
                    "state": (
                        filters.status if filters.status in ("open", "closed") else "all"
                    ),
                    "sort": "created",
                    "direction": "desc",
                }
                if filters.head:
                    # GitHub /pulls requires "owner:branch" format; auto-qualify
                    # bare branch names (those without a colon) so callers can
                    # pass plain branch names without the filter being silently
                    # ignored by the API.
                    head_val = filters.head
                    if ":" not in head_val and project.owner:
                        head_val = f"{project.owner}:{head_val}"
                    params["head"] = head_val
                if filters.base:
                    params["base"] = filters.base
                r = client.get(f"{_repo_path(project)}/pulls", params=params)
                _check(r)
                items = r.json()
        has_more = len(items) >= per_page
        return [_map_pr(it) for it in items], has_more

    def get_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> tuple[PullRequest, list[Comment]]:
        """Fetch a single PR plus its issue-style comments.

        Returns `(pr, comments)`. Code-review threads live on a different
        endpoint (`/pulls/{n}/comments`) and aren't merged in here — the
        plan scopes PR comments to the issue-shared `/issues/{n}/comments`
        endpoint, which is what `add_pr_comment` posts to.

        Also fetches submitted reviews (`GET /pulls/{n}/reviews`, via the
        shared `_fetch_pr_reviews` helper also used by `list_pr_reviews`)
        and populates `pr.reviews` (all submitted reviews), `pr.reviewers`
        (distinct authors, keeping each author's most recently submitted
        review), and `pr.review_decision` (derived from the latest
        per-author review states; ticket #148).
        """
        with _client(token) as client:
            r = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            _check(r)
            pr = _map_pr(r.json())
            c = client.get(
                f"{_repo_path(project)}/issues/{pr_id}/comments",
                params={"per_page": 100},
            )
            _check(c)
            comments = [_map_comment(it) for it in c.json()]
            reviews = _fetch_pr_reviews(client, project, pr_id)
        latest_by_author = _latest_reviews_by_author(reviews)
        pr.reviews = reviews
        pr.reviewers = [rv.author for rv in latest_by_author]
        pr.review_decision = review_decision_from_states(
            [rv.state for rv in latest_by_author]
        )
        return pr, comments

    def create_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        requested_reviewers: list[str] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> PullRequest:
        """Create a pull request, applying the AI-generated marker.

        Marker policy mirrors `create_ticket` (see ticket
        Seretos/agent-marketplace#15): body prefix is the canonical
        source of truth; the `ai_generated` LABEL is best-effort. When
        the caller lacks permission to create or apply the label, the
        PR is still created and the follow-up labels POST is skipped (or
        restricted to caller-supplied labels). Mode A silent-drop on the
        labels POST is detected and logged.

        Labels, assignees, and reviewer requests are applied in
        follow-up calls because the `POST /pulls` endpoint doesn't
        accept them inline.

        Optional `idempotency_key` (ticket #150): a retried call with the
        same key (scoped to this project) returns the PR created by the
        first successful call instead of creating a duplicate, with
        `idempotent_replay=True` set on the result. Only `title`/`head`/
        `base` are compared across calls with the same key — `body`,
        `draft`, `labels`, `assignees`, `requested_reviewers` are ignored
        for conflict detection. A retry with the same key but a different
        `title`/`head`/`base` raises `IdempotencyConflict`. `None`/`""`
        (the default) disables idempotency entirely.
        """
        if idempotency_key:
            replay = _idempotency.lookup(
                (project.provider, project.id),
                idempotency_key,
                {"title": title, "head": head, "base": base},
            )
            if replay is not None:
                return replay
        ai_generated_label = project.auto_labels.ai_generated
        merged_labels = list(dict.fromkeys([*(labels or []), ai_generated_label]))
        prefixed_body = ensure_body_prefix(body, markers=_marker_set(project))
        with _client(token) as client:
            _assert_labels_exist(client, project, labels or [])
            label_ok = _ensure_label_best_effort(
                client, project, ai_generated_label, "generated"
            )
            payload: dict[str, Any] = {
                "title": title,
                "body": prefixed_body,
                "head": head,
                "base": base,
                "draft": draft,
            }
            r = client.post(f"{_repo_path(project)}/pulls", json=payload)
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 422 and (
                    "pullrequest.head" in exc.message.lower()
                    or "head branch" in exc.message.lower()
                ):
                    raise GitHubError(
                        422,
                        f"create_pr: head branch {head!r} does not exist in"
                        f" {project.id} — push it first;"
                        f" original error: {exc.message}",
                    ) from exc
                raise
            pr_raw = r.json()
            pr_number = pr_raw["number"]
            # Apply labels via the issues endpoint (PRs share it). Skip
            # the call entirely when there's nothing to apply — including
            # the case where `ai-generated` couldn't be ensured and the
            # caller didn't supply any other labels.
            labels_to_apply = (
                merged_labels
                if label_ok
                else [lbl for lbl in merged_labels if lbl != ai_generated_label]
            )
            warnings: list[str] = []
            if labels_to_apply:
                lbl_resp = client.post(
                    f"{_repo_path(project)}/issues/{pr_number}/labels",
                    json={"labels": labels_to_apply},
                )
                lbl_warn = _check_side_step(lbl_resp)
                if lbl_warn is not None:
                    warnings.append(f"labels: {lbl_warn}")
                else:
                    applied_raw = lbl_resp.json()
                    # Reflect the new labels back into the PR payload so the
                    # returned dataclass advertises them.
                    pr_raw["labels"] = applied_raw
                    if label_ok and not _label_present(
                        {"labels": applied_raw}, ai_generated_label
                    ):
                        log.warning(
                            "PR #%s created on %s/%s without '%s' label "
                            "(GitHub silently dropped it — caller likely "
                            "lacks triage permission); body-prefix marker "
                            "remains",
                            pr_number, project.owner, project.repo,
                            ai_generated_label,
                        )
            if assignees:
                a_resp = client.post(
                    f"{_repo_path(project)}/issues/{pr_number}/assignees",
                    json={"assignees": assignees},
                )
                a_warn = _check_side_step(a_resp)
                if a_warn is not None:
                    warnings.append(f"assignees: {a_warn}")
                else:
                    # The /assignees endpoint returns the issue payload with
                    # the updated assignee list; mirror it.
                    pr_raw["assignees"] = a_resp.json().get("assignees") or []
            if requested_reviewers:
                rv_resp = client.post(
                    f"{_repo_path(project)}/pulls/{pr_number}/requested_reviewers",
                    json={"reviewers": requested_reviewers},
                )
                rv_warn = _check_side_step(rv_resp)
                if rv_warn is not None:
                    warnings.append(f"requested_reviewers: {rv_warn}")
                else:
                    pr_raw["requested_reviewers"] = (
                        rv_resp.json().get("requested_reviewers") or []
                    )
            pr = _map_pr(pr_raw)
            if warnings:
                pr = dataclasses.replace(pr, warnings=warnings)
            if idempotency_key:
                _idempotency.record(
                    (project.provider, project.id),
                    idempotency_key,
                    {"title": title, "head": head, "base": base},
                    pr,
                )
            return pr

    def update_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        base: str | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
        reviewers_add: list[str] | None = None,
        reviewers_remove: list[str] | None = None,
        draft: bool | None = None,
    ) -> PullRequest:
        """Update a PR's title/body/state/base, plus label/assignee/reviewer deltas.

        `status` accepts `"open"` or `"closed"` only. To merge a PR call
        `merge_pr` — `status="merged"` is rejected by the tool layer.

        Reviewer requests use a separate endpoint from assignees
        (`POST/DELETE /pulls/{n}/requested_reviewers`) — GitHub
        models the two concepts independently because reviewers carry
        per-user review state.

        `draft` toggles the PR's draft state. GitHub's REST PATCH does
        not accept a draft flag; we issue the corresponding GraphQL
        mutation when the value differs from the current state.

        Applies the project's configured `ai_modified` label (mirroring
        `update_ticket`) when the PR wasn't originally created by us.
        """
        _validate_label_lists(labels_add, labels_remove)
        with _client(token) as client:
            r0 = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            try:
                _check(r0)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(404, f"PR '{project.id}#{pr_id}' not found") from exc
                raise
            current = r0.json()
            current_labels = {lbl["name"] for lbl in (current.get("labels") or [])}
            current_assignees = {a["login"] for a in (current.get("assignees") or [])}
            markers = _marker_set(project)

            if labels_add:
                _assert_labels_exist(client, project, labels_add)

            new_labels = set(current_labels)
            if labels_add:
                new_labels.update(labels_add)
            if labels_remove:
                new_labels.difference_update(labels_remove)
            # `ai-modified` is best-effort (see ticket #15): if we can't
            # ensure the label exists, proceed without it rather than
            # failing the legitimate update.
            will_be_ai_generated = project.auto_labels.ai_generated in current_labels
            if not will_be_ai_generated:
                ai_modified_label = project.auto_labels.ai_modified
                if ai_modified_label not in new_labels:
                    if _ensure_label_best_effort(
                        client, project, ai_modified_label, "modified"
                    ):
                        new_labels.add(ai_modified_label)
                else:
                    new_labels.add(ai_modified_label)

            new_assignees = set(current_assignees)
            if assignees_add:
                new_assignees.update(assignees_add)
            if assignees_remove:
                new_assignees.difference_update(assignees_remove)

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                # Ticket #44: re-stamp body marker to match label state.
                payload["body"] = apply_body_marker(
                    body, will_be_ai_generated=will_be_ai_generated, markers=markers,
                )
            if status is not None:
                # The tool layer already restricts `status` to open/closed;
                # accept those here and ignore anything else (the layer
                # raised before us when the value was "merged").
                if status in ("open", "closed"):
                    payload["state"] = status
            if base is not None:
                payload["base"] = base

            # PATCH /pulls only takes pull-request scoped fields; labels
            # and assignees are managed via the issues endpoint.
            if payload:
                pr_resp = client.patch(
                    f"{_repo_path(project)}/pulls/{pr_id}", json=payload
                )
                _check(pr_resp)
                current = pr_resp.json()

            if new_labels != current_labels:
                lbl_resp = client.put(
                    f"{_repo_path(project)}/issues/{pr_id}/labels",
                    json={"labels": sorted(new_labels)},
                )
                _check(lbl_resp)
                current["labels"] = lbl_resp.json()

            if new_assignees != current_assignees:
                # `assignees_add`/`remove` map to two separate endpoints.
                to_add = new_assignees - current_assignees
                to_remove = current_assignees - new_assignees
                if to_add:
                    a_resp = client.post(
                        f"{_repo_path(project)}/issues/{pr_id}/assignees",
                        json={"assignees": sorted(to_add)},
                    )
                    _check(a_resp)
                if to_remove:
                    a_resp = client.request(
                        "DELETE",
                        f"{_repo_path(project)}/issues/{pr_id}/assignees",
                        json={"assignees": sorted(to_remove)},
                    )
                    _check(a_resp)
                # Re-fetch so the returned PR reflects the final state.
                r_final = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
                _check(r_final)
                current = r_final.json()

            if reviewers_add or reviewers_remove:
                # Reviewer add/remove live on `/pulls/{n}/requested_reviewers`,
                # distinct from the issues-shared assignees endpoint.
                current_reviewers = {
                    r["login"]
                    for r in (current.get("requested_reviewers") or [])
                }
                new_reviewers = set(current_reviewers)
                if reviewers_add:
                    new_reviewers.update(reviewers_add)
                if reviewers_remove:
                    new_reviewers.difference_update(reviewers_remove)
                to_add = new_reviewers - current_reviewers
                to_remove = current_reviewers - new_reviewers
                if to_add:
                    rv_resp = client.post(
                        f"{_repo_path(project)}/pulls/{pr_id}/requested_reviewers",
                        json={"reviewers": sorted(to_add)},
                    )
                    _check(rv_resp)
                if to_remove:
                    rv_resp = client.request(
                        "DELETE",
                        f"{_repo_path(project)}/pulls/{pr_id}/requested_reviewers",
                        json={"reviewers": sorted(to_remove)},
                    )
                    _check(rv_resp)
                r_final = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
                _check(r_final)
                current = r_final.json()

            if draft is not None and bool(current.get("draft", False)) != draft:
                node_id = current.get("node_id")
                if not node_id:
                    raise GitHubError(
                        500,
                        f"PR #{pr_id} payload missing 'node_id'; "
                        "cannot toggle draft state via GraphQL",
                    )
                _set_pr_draft_via_graphql(client, node_id, draft)
                r_final = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
                _check(r_final)
                current = r_final.json()

            return _map_pr(current)

    def add_pr_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        body: str,
    ) -> Comment:
        """Add a discussion comment to a PR (NOT a code-review comment).

        Uses the shared `/issues/{n}/comments` endpoint; the AI-marker
        prefix is applied via `ensure_comment_prefix`.
        """
        prefixed = ensure_comment_prefix(body, markers=_marker_set(project))
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/issues/{pr_id}/comments",
                json={"body": prefixed},
            )
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"PR '{project.id}#{pr_id}' not found"
                    ) from exc
                raise
            return _map_comment(r.json())

    def list_pr_review_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> list[ReviewComment]:
        """List inline code-review comments on a PR.

        Hits `GET /repos/{o}/{r}/pulls/{n}/comments` — distinct from the
        issue-style `/issues/{n}/comments` endpoint surfaced via
        `get_pr().comments`. Result is paginated; we cap at 100 per page
        (the GitHub maximum). Reviewers typically don't stack hundreds
        of inline comments, so the single-page take is acceptable.
        """
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/pulls/{pr_id}/comments",
                params={"per_page": 100},
            )
            _check(r)
            return [_map_review_comment(it) for it in r.json()]

    def list_pr_reviews(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> list[Review]:
        """List submitted reviews on a PR.

        Hits `GET /repos/{o}/{r}/pulls/{n}/reviews`, capped at 100 per
        page (the GitHub maximum) — matching `list_pr_review_comments`'s
        single-page take. `PENDING` reviews (still being drafted by the
        reviewer, not yet submitted) are skipped.
        """
        with _client(token) as client:
            return _fetch_pr_reviews(client, project, pr_id)

    def add_pr_review_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        body: str,
        path: str | None = None,
        line: int | None = None,
        side: str = "RIGHT",
        commit_sha: str | None = None,
        in_reply_to: str | None = None,
    ) -> ReviewComment:
        """Add an inline code-review comment.

        Two modes:
          - **New thread**: `path`, `line`, and `commit_sha` are
            required; `in_reply_to` must be `None`.
          - **Reply**: only `in_reply_to` (parent comment id) and
            `body`; positional fields must be `None`.

        Mode is validated at the tool layer; this method trusts its
        inputs and routes them to the matching POST shape.
        """
        prefixed = ensure_comment_prefix(body, markers=_marker_set(project))
        if in_reply_to is not None:
            payload: dict[str, Any] = {
                "body": prefixed,
                "in_reply_to": int(in_reply_to),
            }
        else:
            payload = {
                "body": prefixed,
                "path": path,
                "line": line,
                "side": side,
                "commit_id": commit_sha,
            }
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/pulls/{pr_id}/comments",
                json=payload,
            )
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 422:
                    raise GitHubError(
                        422,
                        f"add_pr_review_comment: could not resolve diff location"
                        f" — check that path={path!r}, line={line!r},"
                        f" commit_sha={commit_sha!r} refer to an existing"
                        f" position in PR {pr_id};"
                        f" original error: {exc.message}",
                    ) from exc
                raise
            return _map_review_comment(r.json())

    def submit_pr_review(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        state: str,
        body: str | None = None,
        commit_sha: str | None = None,
    ) -> Review:
        """Submit a PR review via `POST /pulls/{n}/reviews`.

        `state` is one of `"approve"`, `"request_changes"`, `"comment"`
        (normalized values, lower-case). They map to GitHub's `event`
        enum (`APPROVE` / `REQUEST_CHANGES` / `COMMENT`).

        GitHub requires a non-empty body for `REQUEST_CHANGES` and
        `COMMENT`; we surface that as a `ValueError` to fail fast
        without round-tripping. The body is marker-prefixed via
        `ensure_comment_prefix` when present.
        """
        event_map = {
            "approve": "APPROVE",
            "request_changes": "REQUEST_CHANGES",
            "comment": "COMMENT",
        }
        event = event_map.get(state)
        if event is None:
            raise ValueError(
                f"unsupported review state {state!r} — accepted: "
                f"{sorted(event_map)}"
            )
        if event in ("REQUEST_CHANGES", "COMMENT") and not body:
            raise ValueError(
                f"a review body is required when state={state!r}"
            )
        payload: dict[str, Any] = {"event": event}
        if body:
            payload["body"] = ensure_comment_prefix(body, markers=_marker_set(project))
        if commit_sha:
            payload["commit_id"] = commit_sha
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/pulls/{pr_id}/reviews",
                json=payload,
            )
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"PR '{project.id}#{pr_id}' not found"
                    ) from exc
                if exc.status == 422 and "your own pull request" in exc.message.lower():
                    raise GitHubError(
                        422,
                        exc.message + " (GitHub platform restriction; use another account)",
                    ) from exc
                raise
            raw = r.json()
        return Review(
            id=str(raw.get("id", "")),
            state=state,  # type: ignore[arg-type]
            author=(raw.get("user") or {}).get("login", ""),
            body=raw.get("body") or "",
            url=raw.get("html_url") or "",
            submitted_at=raw.get("submitted_at") or "",
            commit_sha=raw.get("commit_id"),
        )

    def merge_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        merge_method: str = "merge",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> PullRequest:
        """Merge a PR. `merge_method` is one of "merge", "squash", "rebase".

        Translates the GitHub merge-not-allowed 405 into a `GitHubError`
        via `_check`. After the merge succeeds, re-fetches the PR so the
        returned dataclass advertises the merged state.
        """
        if merge_method not in ("merge", "squash", "rebase"):
            raise GitHubError(400, f"invalid merge_method '{merge_method}'")
        payload: dict[str, Any] = {"merge_method": merge_method}
        if commit_title is not None:
            payload["commit_title"] = commit_title
        if commit_message is not None:
            payload["commit_message"] = commit_message
        with _client(token) as client:
            # Pre-flight: if the PR is already merged, raise before PUT so
            # callers get a clear "already merged" error rather than a silent
            # HTTP 200 that looks like a fresh merge.
            preflight = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            _check(preflight)
            if preflight.json().get("merged") is True:
                raise GitHubError(
                    405, f"PR '{project.id}#{pr_id}' is already merged"
                )
            r = client.put(
                f"{_repo_path(project)}/pulls/{pr_id}/merge", json=payload
            )
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 405:
                    # GitHub returns 405 for both "already merged" and
                    # "merge conflict / not mergeable".  Probe the PR to
                    # find out which situation we're in.
                    probe = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
                    _check(probe)
                    raw = probe.json()
                    if raw.get("merged") is True:
                        raise GitHubError(
                            405, f"PR '{project.id}#{pr_id}' is already merged"
                        ) from exc
                    mergeable_state = raw.get("mergeable_state") or "unknown"
                    raise GitHubError(
                        405,
                        f"PR '{project.id}#{pr_id}' cannot be merged:"
                        f" mergeable_state='{mergeable_state}'"
                        f" — rebase or resolve conflicts and retry",
                    ) from exc
                raise
            # Re-fetch so the response carries the merged state/timestamp.
            r2 = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            _check(r2)
            return _map_pr(r2.json())

    # ---------- relations (write side) --------------------------------------

    _SUPPORTED_RELATION_KINDS: tuple[str, ...] = (
        "parent", "child", "blocks", "blocked_by", "duplicate_of",
    )

    def add_relation(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        kind: str,
        target: str,
    ) -> Relation:
        """Create a typed relation from `ticket_id` to `target` (ticket #41).

        Provider mapping:
          - `parent` / `child` → Sub-Issues API (issues/.../sub_issues).
            `add_relation(A, kind="parent", target=B)` means A is B's
            parent: POST to A's own sub-issues endpoint with B as the
            sub-issue. `add_relation(A, kind="child", target=B)` means A
            is B's child: POST to B's own sub-issues endpoint with A as
            the sub-issue. The read path (`_fetch_relations`) is
            independent of this write mapping and stays authoritative:
            after `add_relation(A, "parent", B)`, `get_ticket(A)` reports
            a child relation to B and `get_ticket(B)` reports a parent
            relation to A.
          - `blocks` / `blocked_by` → Dependencies API (api 2026-03-10):
            `POST /issues/{n}/dependencies/blocked_by` with
            `{"issue_id": <internal_id>}`. `blocks` is implemented as
            `add_relation(target, kind=blocked_by, source=ticket_id)`.
          - `duplicate_of` → side-effect: append `Duplicate of #N` to the
            body **and** close the source with state_reason="duplicate"
            so the existing read path (`_fetch_relations`) surfaces the
            link. The body edit is re-marked via `apply_body_marker`
            to keep the AI-attribution marker consistent.
          - `relates_to` → unsupported on GitHub (no native typed link).

        `target` is parsed via `_parse_relation_target`; currently
        same-repo only (cross-repo targets raise NotImplementedError).
        """
        if kind not in self._SUPPORTED_RELATION_KINDS:
            raise RelationKindUnsupported(
                kind, "github", self._SUPPORTED_RELATION_KINDS,
            )
        # Resolve target to (repo_path, issue_number) before opening a
        # network connection so the self-relation guard fires cheaply.
        target_repo, target_number = _parse_relation_target(target, project)
        # Self-relation guard: for parent/child, the relation is expressed
        # as ticket_id vs target_number in their *logical* direction.
        # For blocks, the wire direction is flipped but the logical relation
        # is still between ticket_id and target_number.
        _assert_not_self_relation(ticket_id, target_number)
        with _client(token) as client:
            try:
                target_internal_id, target_raw = _fetch_issue_internal_id(
                    client, target_repo, target_number,
                )
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404,
                        f"target issue #{target_number} not found in {project.owner}/{project.repo}",
                    ) from exc
                raise
            if kind == "parent":
                # parent(A→B): A is B's parent → POST /issues/A/sub_issues
                # (A's own endpoint) with sub_issue_id=B (target becomes
                # A's child on the wire — no source/target swap needed).
                if _github_sub_issue_already_exists(
                    client, _repo_path(project), ticket_id,
                    sub_issue_internal_id=target_internal_id,
                ):
                    raise RelationAlreadyExists(
                        kind="parent",
                        ticket_id=ticket_id,
                        target=f"#{target_number}",
                    )
                return _github_post_sub_issue(
                    client, _repo_path(project), ticket_id,
                    sub_issue_internal_id=target_internal_id,
                    relation_kind_for_caller="parent",
                    target_raw=target_raw,
                    project=project,
                    caller_ticket_id=ticket_id,
                    caller_target_ref=f"#{target_number}",
                )
            if kind == "child":
                # child(A→B): A is B's child → POST /issues/B/sub_issues
                # (B's own endpoint) with sub_issue_id=A. Source/target
                # swap on the wire.
                try:
                    ticket_internal_id = _fetch_issue_internal_id(
                        client, _repo_path(project), ticket_id,
                    )[0]
                except GitHubError as exc:
                    if exc.status == 404:
                        raise GitHubError(
                            404, f"ticket '{project.id}#{ticket_id}' not found"
                        ) from exc
                    raise
                # Pre-flight: detect inverse-kind duplicates (e.g. a
                # "parent" edge already exists for this pair, which maps
                # to the same wire endpoint).  The wire parent is
                # target_number and the wire sub-issue is ticket_internal_id.
                if _github_sub_issue_already_exists(
                    client, target_repo, target_number,
                    sub_issue_internal_id=ticket_internal_id,
                ):
                    raise RelationAlreadyExists(
                        kind="child",
                        ticket_id=ticket_id,
                        target=f"#{target_number}",
                    )
                return _github_post_sub_issue(
                    client, target_repo, target_number,
                    sub_issue_internal_id=ticket_internal_id,
                    relation_kind_for_caller="child",
                    target_raw=_fetch_issue_payload(
                        client, target_repo, target_number,
                    ),
                    project=project,
                    caller_ticket_id=ticket_id,
                    caller_target_ref=f"#{target_number}",
                )
            if kind == "blocked_by":
                # Pre-flight: detect inverse-kind duplicates (e.g. a
                # "blocks" edge already exists from the same pair, which
                # maps to the same wire endpoint and id).
                if _github_dependency_already_exists(
                    client, _repo_path(project), ticket_id,
                    target_internal_id=target_internal_id,
                ):
                    raise RelationAlreadyExists(
                        kind="blocked_by",
                        ticket_id=ticket_id,
                        target=f"#{target_number}",
                    )
                return _github_post_dependency(
                    client, _repo_path(project), ticket_id,
                    dep_endpoint="blocked_by",
                    target_internal_id=target_internal_id,
                    relation_kind_for_caller="blocked_by",
                    target_raw=target_raw,
                    project=project,
                    caller_ticket_id=ticket_id,
                    caller_target_ref=f"#{target_number}",
                )
            if kind == "blocks":
                # blocks(A→B): A blocks B → on B's endpoint, add A as
                # blocked_by. Swap source/target on the wire.
                try:
                    source_internal_id, _ = _fetch_issue_internal_id(
                        client, _repo_path(project), ticket_id,
                    )
                except GitHubError as exc:
                    if exc.status == 404:
                        raise GitHubError(
                            404, f"ticket '{project.id}#{ticket_id}' not found"
                        ) from exc
                    raise
                # Pre-flight: detect inverse-kind duplicates (e.g. a
                # "blocked_by" edge already exists from the same pair,
                # which maps to the same wire endpoint).  The wire source
                # for "blocks" is target_number and the wire target is
                # source_internal_id.
                if _github_dependency_already_exists(
                    client, target_repo, target_number,
                    target_internal_id=source_internal_id,
                ):
                    raise RelationAlreadyExists(
                        kind="blocks",
                        ticket_id=ticket_id,
                        target=f"#{target_number}",
                    )
                return _github_post_dependency(
                    client, target_repo, target_number,
                    dep_endpoint="blocked_by",
                    target_internal_id=source_internal_id,
                    relation_kind_for_caller="blocks",
                    target_raw=target_raw,
                    project=project,
                    caller_ticket_id=ticket_id,
                    caller_target_ref=f"#{target_number}",
                )
            if kind == "duplicate_of":
                return _github_mark_duplicate_of(
                    client, project, ticket_id,
                    target_number=target_number,
                    target_raw=target_raw,
                )
            raise RelationKindUnsupported(
                kind, "github", self._SUPPORTED_RELATION_KINDS,
            )

    def remove_relation(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        kind: str,
        target: str,
    ) -> dict:
        """Remove a typed relation. Inverse of `add_relation`.

        For `duplicate_of`, removal strips the ``Duplicate of #N`` marker
        line from the source issue's body *and* reopens the issue.  The
        body is the sole source of truth for this relation kind
        (`_fetch_relations` scans the body regardless of state), so
        stripping the marker is the only reliable way to stop reporting it.
        Stripping IS the intended contract — body history is deliberately
        not preserved.
        """
        if kind not in self._SUPPORTED_RELATION_KINDS:
            raise RelationKindUnsupported(
                kind, "github", self._SUPPORTED_RELATION_KINDS,
            )
        with _client(token) as client:
            target_repo, target_number = _parse_relation_target(target, project)
            try:
                target_internal_id, _ = _fetch_issue_internal_id(
                    client, target_repo, target_number,
                )
            except GitHubError as exc:
                if exc.status == 404:
                    raise RelationNotFound(
                        kind=kind,
                        ticket_id=ticket_id,
                        target=f"#{target_number}",
                    ) from exc
                raise
            if kind == "parent":
                # parent(A→B): A is B's parent → DELETE on A's own
                # endpoint, removing B (target_internal_id, already
                # fetched above) from A's sub-issues.
                try:
                    r = client.request(
                        "DELETE",
                        f"{_repo_path(project)}/issues/{ticket_id}/sub_issue",
                        json={"sub_issue_id": target_internal_id},
                    )
                    _check(r)
                except GitHubError as exc:
                    if exc.status == 404:
                        raise RelationNotFound(
                            kind=kind,
                            ticket_id=ticket_id,
                            target=f"#{target_number}",
                        ) from exc
                    raise
                return {"removed": True}
            if kind == "child":
                # child(A→B): A is B's child → DELETE on B's own
                # endpoint, removing A from B's sub-issues.
                try:
                    source_internal_id, _ = _fetch_issue_internal_id(
                        client, _repo_path(project), ticket_id,
                    )
                except GitHubError as exc:
                    if exc.status == 404:
                        raise RelationNotFound(
                            kind=kind,
                            ticket_id=ticket_id,
                            target=f"#{target_number}",
                        ) from exc
                    raise
                try:
                    r = client.request(
                        "DELETE",
                        f"{target_repo}/issues/{target_number}/sub_issue",
                        json={"sub_issue_id": source_internal_id},
                    )
                    _check(r)
                except GitHubError as exc:
                    if exc.status == 404:
                        raise RelationNotFound(
                            kind=kind,
                            ticket_id=ticket_id,
                            target=f"#{target_number}",
                        ) from exc
                    raise
                return {"removed": True}
            if kind == "blocked_by":
                # Pre-check existence so the documented "remove returns
                # error when nothing was removed" contract holds —
                # GitHub's Dependencies DELETE endpoint is silently
                # idempotent on its own (ticket #49 finding 8 / #48
                # finding 3).
                _github_assert_dependency_exists(
                    client, _repo_path(project), ticket_id,
                    target_internal_id=target_internal_id,
                    source_ref=f"#{ticket_id}",
                    target_ref=f"#{target_number}",
                    kind=kind,
                    ticket_id=ticket_id,
                )
                r = client.delete(
                    f"{_repo_path(project)}/issues/{ticket_id}"
                    f"/dependencies/blocked_by/{target_internal_id}",
                )
                _check(r)
                return {"removed": True}
            if kind == "blocks":
                try:
                    source_internal_id, _ = _fetch_issue_internal_id(
                        client, _repo_path(project), ticket_id,
                    )
                except GitHubError as exc:
                    if exc.status == 404:
                        raise RelationNotFound(
                            kind=kind,
                            ticket_id=ticket_id,
                            target=f"#{target_number}",
                        ) from exc
                    raise
                _github_assert_dependency_exists(
                    client, target_repo, target_number,
                    target_internal_id=source_internal_id,
                    source_ref=f"#{ticket_id}",
                    target_ref=f"#{target_number}",
                    kind=kind,
                    ticket_id=ticket_id,
                )
                r = client.delete(
                    f"{target_repo}/issues/{target_number}"
                    f"/dependencies/blocked_by/{source_internal_id}",
                )
                _check(r)
                return {"removed": True}
            if kind == "duplicate_of":
                # The read path detects duplicate_of solely from a
                # ``Duplicate of #N`` body line (body is the source of
                # truth regardless of state).  Removal must therefore:
                #   1. Strip the ``Duplicate of #N`` line from the body.
                #   2. Reopen the source issue.
                # After these two changes, the body scan no longer finds
                # the marker, so get_ticket stops reporting the relation.
                src = _fetch_issue_payload(
                    client, _repo_path(project), ticket_id,
                )
                current_body = src.get("body") or ""
                current_labels = {
                    lbl["name"] for lbl in (src.get("labels") or [])
                }
                markers = _marker_set(project)
                will_be_ai_generated = project.auto_labels.ai_generated in current_labels
                # Strip the AI marker first so we can work on the core body.
                body_core = strip_leading_ai_marker(current_body, markers=markers)
                # Remove the ``Duplicate of #N`` line (and any surrounding
                # blank lines it introduced) using a case-insensitive match.
                dup_pattern = re.compile(
                    r"(?i)^duplicate\s+of\s+(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?/"
                    r"[A-Za-z0-9._\-]+)?#"
                    + re.escape(target_number)
                    + r"\s*$",
                    re.MULTILINE,
                )
                if not dup_pattern.search(body_core):
                    raise RelationNotFound(
                        kind="duplicate_of",
                        ticket_id=ticket_id,
                        target=f"#{target_number}",
                    )
                # Remove the matching line and collapse resulting double
                # blank lines produced by the surrounding \n\n separators.
                body_stripped = dup_pattern.sub("", body_core)
                # Collapse multiple consecutive blank lines to a single one.
                body_stripped = re.sub(r"\n{3,}", "\n\n", body_stripped).strip()
                new_body = apply_body_marker(
                    body_stripped, will_be_ai_generated=will_be_ai_generated, markers=markers,
                )
                pr = client.patch(
                    f"{_repo_path(project)}/issues/{ticket_id}",
                    json={"state": "open", "body": new_body},
                )
                _check(pr)
                return {"removed": True}
            raise RelationKindUnsupported(
                kind, "github", self._SUPPORTED_RELATION_KINDS,
            )

    # ---------- pipelines / CI runs -----------------------------------------

    def list_runs_for_branch(
        self,
        project: ProjectConfig,
        token: str | None,
        branch: str,
        status: str = "all",
        limit: int = 10,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List Actions workflow runs filtered by branch.

        Returns ``(runs, resolved_refs)`` to mirror the tag/ticket shape:
        - ``([], [])`` — branch not found
        - ``([], [sha, "no-ci"])`` — branch exists, no runs, no workflows
        - ``([], [sha])`` — branch exists, no runs, CI is configured
        - ``(runs, [sha])`` — branch exists, runs found
        """
        _validate_limit(limit)
        with _client(token) as client:
            sha = _resolve_branch_sha(client, project, branch)
            if sha is None:
                return [], []
            raw_runs = _list_runs_for_branch(client, project, branch, status, limit)
            if raw_runs:
                return [_map_run(r) for r in raw_runs], [sha]
            if not _has_workflows(client, project):
                return [], [sha, "no-ci"]
            return [], [sha]

    def list_runs_for_commit(
        self,
        project: ProjectConfig,
        token: str | None,
        sha: str,
        status: str = "all",
        limit: int = 10,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List runs whose ``head_sha`` matches ``sha``.

        Returns ``(runs, resolved_refs)`` to mirror the tag/ticket shape:
        - ``([], [])`` — commit not found
        - ``(runs, [sha])`` — commit found (runs may be empty)
        """
        _validate_limit(limit)
        with _client(token) as client:
            if not _resolve_commit(client, project, sha):
                return [], []
            raw_runs = _list_runs_for_commit(client, project, sha, status, limit)
            return [_map_run(r) for r in raw_runs], [sha]

    def list_runs_for_tag(
        self,
        project: ProjectConfig,
        token: str | None,
        tag: str,
        status: str = "all",
        limit: int = 10,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Resolve `tag` -> commit SHA -> runs filtered by head_sha.

        Returns `(runs, resolved_refs)` where `resolved_refs` lists the
        single SHA we resolved to (handy for telling the caller which
        commit was actually queried).
        """
        with _client(token) as client:
            sha = _resolve_tag_sha(client, project, tag)
            if not sha:
                return [], []
            runs = _list_runs_for_commit(client, project, sha, status, limit)
            return [_map_run(r) for r in runs], [sha]

    def list_runs_for_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        status: str = "all",
        limit: int = 10,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Resolve a ticket -> linked PR head_shas -> runs.

        Returns `(runs, resolved_refs)`. `resolved_refs` is the de-duped
        list of head_shas we queried. When the ticket has no linked PR
        or branch reference, both lists are empty (the tool layer turns
        this into a `hint`).
        """
        with _client(token) as client:
            shas = _resolved_refs_for_ticket(client, project, ticket_id)
            if not shas:
                return [], []
            # Aggregate by run id so multiple SHAs that share a run don't
            # produce duplicates.
            by_id: dict[str, dict] = {}
            for sha in shas:
                for r in _list_runs_for_commit(
                    client, project, sha, status, limit
                ):
                    rid = str(r.get("id", ""))
                    if rid and rid not in by_id:
                        by_id[rid] = r
            # Sort by created_at desc and cap to `limit`.
            raws = sorted(
                by_id.values(),
                key=lambda r: r.get("created_at", ""),
                reverse=True,
            )[:limit]
            return [_map_run(r) for r in raws], shas

    def list_runs_recent(
        self,
        project: ProjectConfig,
        token: str | None,
        *,
        status: str = "all",
        limit: int = 10,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List the most recent Actions workflow runs, unfiltered by ref.

        Returns ``(runs, [])`` — the empty ``resolved_refs`` signals that
        no ref filter was applied.
        """
        _validate_limit(limit)
        with _client(token) as client:
            params = _runs_params(status, limit)
            r = client.get(f"{_repo_path(project)}/actions/runs", params=params)
            _check(r)
            raw_runs = (r.json() or {}).get("workflow_runs", [])
        return [_map_run(run) for run in raw_runs], []

    def get_run(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
        include_failure_excerpt: bool = True,
        *,
        tail_lines: int | None = None,
    ) -> PipelineRun:
        """Fetch a single workflow run, optionally with failure context.

        When `include_failure_excerpt` is True AND the run concluded as
        failed, populates `run.failure` with per-failing-job annotations
        and a small log excerpt. In-progress runs (`conclusion=None`)
        never trigger the failure-context fetch.

        ``tail_lines``, when a positive int, overrides the smart excerpt
        logic and returns the last *tail_lines* lines of each failing
        job's log verbatim.  Use this as a deterministic escape hatch
        when the automatic anchor heuristics are not sufficient.
        """
        if not str(run_id).strip().isdigit():
            raise GitHubError(
                404,
                f"pipeline '{project.id}#{run_id}' not found"
                f" — run_id must be numeric for GitHub Actions",
            )
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/actions/runs/{run_id}"
            )
            try:
                _check(r)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(
                        404, f"pipeline '{project.id}#{run_id}' not found"
                    ) from exc
                raise
            raw = r.json()
            run = _map_run(raw)
            if (
                include_failure_excerpt
                and run.conclusion == "failure"
                and run.status == "completed"
            ):
                run.failure = _get_failure_excerpt(
                    client, project, token, run_id, tail_lines=tail_lines
                )
            return run

    def get_step_log(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
        job_id: str,
    ) -> str:
        """Fetch the full, unbounded raw log for a single job.

        Reuses the 302-redirect signed-URL flow already used internally
        for failure-excerpt building, but with no `max_bytes` cap, so the
        caller gets the exact same log GitHub would show, untruncated.
        Raises `GitHubError(404, ...)` when the log is unavailable
        (403/404 on the underlying redirect, or a non-numeric `run_id`).
        """
        if not str(run_id).strip().isdigit():
            raise GitHubError(
                404,
                f"pipeline '{project.id}#{run_id}' not found"
                f" — run_id must be numeric for GitHub Actions",
            )
        log_text = _fetch_job_log(
            token,
            f"{_repo_path(project)}/actions/jobs/{job_id}/logs",
            max_bytes=None,
        )
        if log_text is None:
            raise GitHubError(
                404,
                f"log for job '{job_id}' in pipeline"
                f" '{project.id}#{run_id}' not found",
            )
        return log_text

    # ---------- label management ---------------------------------------------

    def list_labels(
        self,
        project: ProjectConfig,
        token: str | None,
    ) -> list[Label]:
        """List all labels on the repository.

        Uses `GET /repos/{owner}/{repo}/labels` with `per_page=100`.
        Returns one page (up to 100 labels). GitHub returns `color` as a
        6-hex string without `#` (e.g. ``"ededed"``); passed through as-is.
        """
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/labels",
                params={"per_page": 100},
            )
            _check(r)
            return [
                Label(
                    name=item.get("name") or "",
                    color=item.get("color") or "",
                    description=item.get("description") or "",
                )
                for item in r.json()
            ]

    def create_label(
        self,
        project: ProjectConfig,
        token: str | None,
        name: str,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Create a new label on the repository.

        Unlike the internal `_ensure_label` helper (which silently ignores
        422-already-exists), this public surface raises `GitHubError(422,
        …)` when the label already exists so callers get an explicit signal.

        For 422 responses, only the ``already_exists`` error code triggers
        the human-readable "already exists" message. Other 422 validation
        errors (e.g. invalid color) fall through to `_check(r)` so the
        original GitHub error text surfaces accurately.

        `color` is a 6-hex string without `#` (e.g. ``"ededed"``).
        Defaults to ``"ededed"`` when not supplied.
        """
        payload: dict[str, Any] = {
            "name": name,
            "color": color if color is not None else "ededed",
        }
        if description is not None:
            payload["description"] = description
        with _client(token) as client:
            r = client.post(f"{_repo_path(project)}/labels", json=payload)
            if r.status_code == 422:
                try:
                    err_code = r.json().get("errors", [{}])[0].get("code")
                except Exception:
                    err_code = None
                if err_code == "already_exists":
                    raise GitHubError(
                        422,
                        f"label {name!r} already exists in {project.id}",
                    )
                # Non-conflict 422 (e.g. invalid color) — surface GitHub's own message.
                _check(r)
            _check(r)
            raw = r.json()
            return Label(
                name=raw.get("name") or "",
                color=raw.get("color") or "",
                description=raw.get("description") or "",
            )

    def update_label(
        self,
        project: ProjectConfig,
        token: str | None,
        name: str,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Rename / recolour / redescribe an existing label.

        At least one of `new_name`, `color`, or `description` must be
        supplied; passing none raises `ValueError` without any HTTP call.

        Uses `PATCH /repos/{owner}/{repo}/labels/{name}`.
        404 → `GitHubError(404, "label '{name}' not found in {project.id}")`.
        """
        if new_name is None and color is None and description is None:
            raise ValueError(
                "update_label requires at least one of: new_name, color, description"
            )
        payload: dict[str, Any] = {}
        if new_name is not None:
            payload["new_name"] = new_name
        if color is not None:
            payload["color"] = color
        if description is not None:
            payload["description"] = description
        with _client(token) as client:
            r = client.patch(
                f"{_repo_path(project)}/labels/{name}",
                json=payload,
            )
            if r.status_code == 404:
                raise GitHubError(404, f"label {name!r} not found in {project.id}")
            _check(r)
            raw = r.json()
            return Label(
                name=raw.get("name") or "",
                color=raw.get("color") or "",
                description=raw.get("description") or "",
            )

    def delete_label(
        self,
        project: ProjectConfig,
        token: str | None,
        name: str,
    ) -> None:
        """Delete a label from the repository.

        Uses `DELETE /repos/{owner}/{repo}/labels/{name}`.
        GitHub returns 204 on success.
        404 → `GitHubError(404, "label '{name}' not found in {project.id}")`.
        """
        with _client(token) as client:
            r = client.delete(f"{_repo_path(project)}/labels/{name}")
            if r.status_code == 404:
                raise GitHubError(404, f"label {name!r} not found in {project.id}")
            _check(r)
        return None

    def discover_projects(
        self, token: str, *, limit: int
    ) -> ProjectDiscoveryResult:
        """Enumerate repositories visible to *token* via ``GET /user/repos``.

        Paginates through all pages (100 repos per page), maps each repo to a
        :class:`DiscoveredProject`, and stops once *limit* entries have been
        collected.  Returns a structured :class:`ProjectDiscoveryResult` rather
        than raising on expected failure modes (401, network error, unexpected
        HTTP status).
        """
        _validate_limit(limit)
        projects: list[DiscoveredProject] = []
        truncated = False
        url: str | None = "/user/repos"
        params: dict | None = {
            "affiliation": "owner,collaborator,organization_member",
            "per_page": 100,
        }

        try:
            with _client(token) as client:
                while url is not None:
                    r = client.get(url, params=params)
                    # After the first request, subsequent ones use the full
                    # next-page URL from the Link header (no extra params).
                    params = None

                    if r.status_code == 401:
                        return ProjectDiscoveryResult(
                            projects=[], reason="bad_credentials"
                        )
                    if not r.is_success:
                        return ProjectDiscoveryResult(
                            projects=[], reason=f"http_{r.status_code}"
                        )

                    page_repos: list[dict] = r.json()
                    for repo in page_repos:
                        if len(projects) >= limit:
                            # We still have repos to consume — the limit was hit
                            # mid-page.
                            truncated = True
                            break
                        projects.append(
                            DiscoveredProject(
                                provider="github",
                                path=repo["full_name"],
                                description=repo.get("description") or "",
                                permissions=_map_permissions_to_capabilities(
                                    repo.get("permissions") or {}
                                ),
                            )
                        )

                    if truncated:
                        break

                    next_url = _next_link_url(r.headers.get("link"))
                    if next_url is not None:
                        if len(projects) >= limit:
                            # Hit the limit exactly at a page boundary; more
                            # pages exist.
                            truncated = True
                            break
                        url = next_url
                    else:
                        # No more pages.
                        url = None

        except httpx.HTTPError:
            return ProjectDiscoveryResult(projects=[], reason="network_error")

        return ProjectDiscoveryResult(
            projects=projects, truncated=truncated, reason=None
        )


# ---------- pipeline helpers (module-level so providers can reuse) ----------


_RUN_STATUS_FILTERS = {"queued", "in_progress", "completed"}


def _map_run(raw: dict) -> PipelineRun:
    """Translate a GitHub `workflow_run` payload into a `PipelineRun`."""
    return PipelineRun(
        id=str(raw.get("id", "")),
        name=raw.get("name") or raw.get("display_title") or "",
        branch=raw.get("head_branch") or "",
        head_sha=raw.get("head_sha") or "",
        event=raw.get("event") or "",
        status=raw.get("status") or "",
        conclusion=raw.get("conclusion"),
        url=raw.get("html_url") or "",
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
        run_attempt=int(raw.get("run_attempt") or 1),
    )


def _runs_params(status: str, limit: int) -> dict[str, Any]:
    _validate_limit(limit)
    per_page = min(max(1, limit), 100)
    params: dict[str, Any] = {"per_page": per_page}
    if status and status != "all" and status in _RUN_STATUS_FILTERS:
        params["status"] = status
    return params


def _list_runs_for_branch(
    client: httpx.Client,
    project: ProjectConfig,
    branch: str,
    status: str,
    limit: int,
) -> list[dict]:
    params = _runs_params(status, limit)
    params["branch"] = branch
    r = client.get(f"{_repo_path(project)}/actions/runs", params=params)
    if r.status_code in (301, 302):
        return []
    _check(r)
    return (r.json() or {}).get("workflow_runs", [])


def _list_runs_for_commit(
    client: httpx.Client,
    project: ProjectConfig,
    sha: str,
    status: str,
    limit: int,
) -> list[dict]:
    params = _runs_params(status, limit)
    params["head_sha"] = sha
    r = client.get(f"{_repo_path(project)}/actions/runs", params=params)
    _check(r)
    return (r.json() or {}).get("workflow_runs", [])


def _resolve_branch_sha(
    client: httpx.Client,
    project: ProjectConfig,
    branch: str,
) -> str | None:
    """Return the HEAD commit SHA for `branch`, or ``None`` on 404/422."""
    r = client.get(f"{_repo_path(project)}/branches/{branch}")
    if r.status_code in (404, 422):
        return None
    _check(r)
    return ((r.json() or {}).get("commit") or {}).get("sha")


def _resolve_commit(
    client: httpx.Client,
    project: ProjectConfig,
    sha: str,
) -> bool:
    """Return ``True`` if `sha` resolves to a commit, ``False`` on 404/422."""
    r = client.get(f"{_repo_path(project)}/commits/{sha}")
    if r.status_code in (404, 422):
        return False
    _check(r)
    return True


def _has_workflows(
    client: httpx.Client,
    project: ProjectConfig,
) -> bool:
    """Return ``True`` if the repository has at least one Actions workflow.

    Only 404 (repository or endpoint not found) is treated as "no
    workflows / not applicable" and returns ``False``.  Auth failures
    (401/403) and server errors (5xx) are propagated via ``_check`` so
    real credential or infrastructure problems surface rather than being
    silently misclassified as the ``"no-ci"`` sentinel.
    """
    r = client.get(f"{_repo_path(project)}/actions/workflows")
    if r.status_code == 404:
        return False
    _check(r)
    return ((r.json() or {}).get("total_count") or 0) > 0


def _resolve_tag_sha(
    client: httpx.Client,
    project: ProjectConfig,
    tag: str,
) -> str | None:
    """Resolve a tag name to a commit SHA.

    Annotated tags point at a `tag` object whose `object.sha` is the
    commit; lightweight tags point directly at the commit. Both shapes
    use `object.sha` on the ref response — GitHub doesn't dereference
    annotated tags through this endpoint, so for annotated tags we
    follow the second hop via `/git/tags/{sha}`.
    """
    r = client.get(f"{_repo_path(project)}/git/refs/tags/{tag}")
    if r.status_code in (404, 422):
        return None
    _check(r)
    obj = (r.json() or {}).get("object") or {}
    sha = obj.get("sha")
    if not sha:
        return None
    if obj.get("type") == "tag":
        # Annotated tag — follow the second hop.
        r2 = client.get(f"{_repo_path(project)}/git/tags/{sha}")
        if r2.status_code in (404, 422):
            return sha
        _check(r2)
        inner = (r2.json() or {}).get("object") or {}
        return inner.get("sha") or sha
    return sha


_BRANCH_HINT_RE = None  # set lazily below


def _resolved_refs_for_ticket(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
) -> list[str]:
    """Collect unique head_shas from PRs linked to a ticket.

    Sources (deduped by SHA, in discovery order):
      1. Timeline `cross-referenced` events whose source is a PR — we
         fetch the PR to read its `head.sha`.
      2. `search/issues` for PRs in this repo whose body mentions the
         ticket number — same: fetch each PR's head.sha.
      3. Best-effort `branch:foo` regex in the ticket body — resolve
         the branch ref to a SHA.

    The timeline `source.issue` object only includes a marker
    (`pull_request`) that signals "this is a PR", NOT the head_sha —
    the SHA always requires the PR fetch.
    """
    global _BRANCH_HINT_RE
    if _BRANCH_HINT_RE is None:
        import re
        _BRANCH_HINT_RE = re.compile(
            r"(?:^|\s)branch:\s*([A-Za-z0-9._\-/]+)",
            re.IGNORECASE,
        )

    seen: set[str] = set()
    out: list[str] = []

    # Fetch the ticket once to read its body (for the `branch:foo` hint).
    # A genuine 404 here means the ticket itself doesn't exist, which is
    # distinct from "ticket exists but has nothing linked" — raise instead
    # of silently returning `[]` so callers can tell the two apart.
    issue_r = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
    _check(issue_r)

    # Early bail: if the ticket is itself a PR, use its own head.sha directly.
    # The `/issues/{id}` endpoint includes a `pull_request` key for PRs.
    if (issue_r.json() or {}).get("pull_request"):
        pr_r = client.get(f"{_repo_path(project)}/pulls/{ticket_id}")
        if pr_r.is_success:
            sha = ((pr_r.json() or {}).get("head") or {}).get("sha")
            if sha:
                return [sha]
        return []

    issue_body = (issue_r.json() or {}).get("body") or ""

    # Linked PR numbers we should fetch for their head.sha.
    pr_numbers: list[int] = []

    # (1) Timeline cross-references that point at PRs.
    tl = client.get(
        f"{_repo_path(project)}/issues/{ticket_id}/timeline",
        params={"per_page": 100},
        headers={"Accept": "application/vnd.github+json"},
    )
    _check(tl)
    for event in tl.json() or []:
        if event.get("event") not in ("cross-referenced", "connected"):
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if not source.get("pull_request"):
            continue
        # Same-repo only — cross-repo PRs would need a per-repo fetch,
        # which falls outside the scope of this plan.
        full = ((source.get("repository") or {}).get("full_name")) or ""
        url = source.get("html_url") or source.get("url") or ""
        same_repo = (
            full == f"{project.owner}/{project.repo}"
            or f"/{project.owner}/{project.repo}/" in url
            or f"/repos/{project.owner}/{project.repo}/" in url
        )
        if not same_repo:
            continue
        n = source.get("number")
        if isinstance(n, int):
            pr_numbers.append(n)

    # (2) `search/issues` for PRs in this repo that mention the ticket.
    try:
        ticket_n = int(ticket_id)
    except (TypeError, ValueError):
        ticket_n = None
    if ticket_n is not None:
        q = f"is:pr repo:{project.owner}/{project.repo} {ticket_n} in:body"
        sr = client.get("/search/issues", params={"q": q, "per_page": 30})
        # Search may rate-limit (403) — degrade silently.
        if sr.is_success:
            for it in (sr.json() or {}).get("items", []) or []:
                n = it.get("number")
                if isinstance(n, int) and n != ticket_n:
                    pr_numbers.append(n)

    # Dedup PR numbers, then fetch each to read head.sha.
    seen_pr: set[int] = set()
    for n in pr_numbers:
        if n in seen_pr:
            continue
        seen_pr.add(n)
        pr_r = client.get(f"{_repo_path(project)}/pulls/{n}")
        if pr_r.status_code == 404:
            continue
        if not pr_r.is_success:
            # Skip individual fetch failures rather than aborting.
            continue
        head = (pr_r.json() or {}).get("head") or {}
        sha = head.get("sha")
        if sha and sha not in seen:
            seen.add(sha)
            out.append(sha)

    # (3) `branch:foo` hint in the ticket body.
    m = _BRANCH_HINT_RE.search(issue_body) if issue_body else None
    if m:
        branch = m.group(1)
        ref_r = client.get(
            f"{_repo_path(project)}/git/refs/heads/{branch}"
        )
        if ref_r.is_success:
            obj = (ref_r.json() or {}).get("object") or {}
            sha = obj.get("sha")
            if sha and sha not in seen:
                seen.add(sha)
                out.append(sha)

    return out


def _extract_log_excerpt(
    text: str,
    *,
    max_lines: int = 30,
    failed_step: str | None = None,
    annotations: list[dict] | None = None,
) -> str:
    """Pick the most useful slice of a job log.

    Anchor strategy (in order):

    1. **Step-header anchor** — GitHub Actions logs contain
       ``##[group]Run <step-name>`` / ``##[endgroup]`` markers. When
       ``failed_step`` matches a group whose body fits this run, the
       excerpt is clamped to that group (``##[group]`` .. ``##[endgroup]``,
       inclusive). If the clamped block is shorter than ``max_lines``,
       trailing context from after ``##[endgroup]`` is appended up to
       ``max_lines``.
    2. **Annotation-line anchor** (fallback) — when at least one
       ``failure``-level annotation carries a ``start_line``, the
       excerpt is built around that line in the same way as the legacy
       substring scan. This helps composite-action / docker-in-runner
       jobs whose logs don't emit ``##[group]Run <name>`` markers.
    3. **Substring scan** (last resort) — first occurrence of
       ``error|failed|##[error]`` (case-insensitive) within the
       sub-sequence **after** the first ``##[group]`` marker (or, if
       none, the whole text). Restricting to the post-first-group
       region prevents template ``echo "::error::..."`` lines inside
       earlier setup steps from hijacking the anchor.
    4. **Tail** — last ``max_lines`` lines.

    The behaviour change vs. the previous implementation fixes
    `agent-project-issues#6` where an unexecuted template ``echo
    "::error::..."`` in a setup step's bash ``if`` block was matched
    by the naive substring scan and the excerpt was centred far away
    from the real failing step.
    """
    import re

    lines = text.splitlines()
    if not lines:
        return ""

    # --- 1) Step-header anchor ------------------------------------------------
    group_pattern = re.compile(r"^.*##\[group\]Run\s+(?P<name>.+?)\s*$")
    endgroup_pattern = re.compile(r"^.*##\[endgroup\]\s*$")
    # Build a list of (start_idx, name, end_idx) for every group block.
    groups: list[tuple[int, str, int]] = []
    open_idx: int | None = None
    open_name: str | None = None
    for idx, line in enumerate(lines):
        m = group_pattern.match(line)
        if m:
            # If a previous group never closed, record it ending here.
            if open_idx is not None and open_name is not None:
                groups.append((open_idx, open_name, idx - 1))
            open_idx = idx
            open_name = (m.group("name") or "").strip()
            continue
        if open_idx is not None and endgroup_pattern.match(line):
            groups.append((open_idx, open_name or "", idx))
            open_idx = None
            open_name = None
    # Unterminated trailing group: extend to end of log.
    if open_idx is not None and open_name is not None:
        groups.append((open_idx, open_name, len(lines) - 1))

    def _clamp(start: int, end: int) -> str:
        start = max(0, start)
        end = min(len(lines) - 1, end)
        block_len = end - start + 1
        if block_len < max_lines:
            # Pad with trailing context outside the group.
            end = min(len(lines) - 1, start + max_lines - 1)
        return "\n".join(lines[start : end + 1])

    if failed_step:
        target = failed_step.strip().casefold()
        # GitHub log group headers use ``##[group]Run <command>`` but the
        # Jobs API exposes the step name as ``"Run <command>"`` (or just
        # ``"<command>"`` for named steps).  Strip a leading ``"run "``
        # prefix so both representations match the captured group name.
        if target.startswith("run "):
            target = target[4:].strip()
        for start_idx, name, end_idx in groups:
            if name.casefold() == target:
                return _clamp(start_idx, end_idx)

    # --- 2) Annotation-line anchor -------------------------------------------
    for ann in annotations or []:
        if (ann.get("annotation_level") or "").lower() != "failure":
            continue
        line_no = ann.get("start_line") or ann.get("end_line")
        if not isinstance(line_no, int) or line_no <= 0:
            continue
        # GitHub annotation line numbers refer to a source file, not the
        # raw log — but when annotations carry an explicit position we
        # use it as a *log-relative* anchor only when the value lies
        # within the log range. Otherwise fall through.
        if 1 <= line_no <= len(lines):
            idx = line_no - 1
            start = max(0, idx - 2)
            end = min(len(lines), idx + max_lines)
            return "\n".join(lines[start:end])

    # --- 3) Substring scan, but only AFTER the first group header -----------
    # Two-pass: prefer the more specific ``##[error]`` marker first; only
    # fall back to the generic ``error|failed`` pattern when none is found.
    scan_offset = 0
    if groups:
        scan_offset = groups[0][0] + 1
    error_marker_pattern = re.compile(r"##\[error\]", re.IGNORECASE)
    generic_pattern = re.compile(r"(error|failed)", re.IGNORECASE)
    for pass_pattern in (error_marker_pattern, generic_pattern):
        for idx in range(scan_offset, len(lines)):
            if pass_pattern.search(lines[idx]):
                start = max(scan_offset, idx - 2)
                end = min(len(lines), idx + max_lines)
                return "\n".join(lines[start:end])

    # --- 4) Tail fallback ----------------------------------------------------
    return "\n".join(lines[-max_lines:])


def _fetch_job_log(
    token: str | None,
    log_url: str,
    *,
    max_bytes: int | None = 256 * 1024,
) -> str | None:
    """Fetch a job log via the 302-redirect signed-URL flow.

    GitHub responds with a 302 to a short-lived signed URL on a
    different host (typically blob.core.windows.net for hosted runners).
    httpx removes the Authorization header on cross-host redirects,
    which is the correct behavior — the signed URL carries its own auth.
    Returns `None` on 403/404 so the caller can mark logs as unavailable.

    Uses `follow_redirects=True` ONLY for this call (the default `_client`
    leaves it False, which is correct for the JSON API calls).

    ``max_bytes`` caps the number of bytes read from the response body.
    Pass ``None`` to read the full log without any cap (needed when the
    caller requires the true tail, e.g. for the ``tail_lines`` escape
    hatch).  The default of 256 KB is preserved for all ordinary callers.
    """
    headers = {
        "Accept": ACCEPT,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(
        base_url=API_BASE,
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
    ) as c:
        r = c.get(log_url)
        if r.status_code in (403, 404):
            return None
        if not r.is_success:
            return None
        # Cap the read to avoid blowing memory on runaway logs.
        # When max_bytes is None the full body is returned (used by the
        # tail_lines path which needs the real end of the log).
        content = r.content if max_bytes is None else r.content[:max_bytes]
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return None


def _normalize_gh_annotations(
    raw: list[dict], step: str,
) -> list[FailureAnnotation]:
    """Map raw GitHub Check-Run annotation JSON into `FailureAnnotation`s
    (ticket #152).

    `raw` is exactly the payload `_get_failure_excerpt` already fetches
    from `{check_run_url}/annotations` and continues to pass, unchanged,
    into `_extract_log_excerpt` for its annotation-anchor heuristic —
    this helper is a separate, parallel projection for the agent-facing
    `FailingJob.annotations` field and does not affect that log-excerpt
    logic. `step` is the job name, applied to every mapped annotation.

    Field mapping:
      - `path`                              -> `file`
      - `start_line` (falls back to `end_line`) -> `line`
      - `message` (default `""`)            -> `message`
      - `title`                             -> `title`
      - `annotation_level`                  -> `severity`

    Missing/malformed keys degrade gracefully (no `KeyError`); an empty
    or non-list `raw` yields `[]`.
    """
    out: list[FailureAnnotation] = []
    for ann in raw or []:
        if not isinstance(ann, dict):
            continue
        line = ann.get("start_line")
        if line is None:
            line = ann.get("end_line")
        out.append(
            FailureAnnotation(
                step=step,
                message=ann.get("message") or "",
                file=ann.get("path"),
                line=line if isinstance(line, int) else None,
                severity=ann.get("annotation_level"),
                title=ann.get("title"),
            )
        )
    return out


def _get_failure_excerpt(
    client: httpx.Client,
    project: ProjectConfig,
    token: str | None,
    run_id: str,
    *,
    tail_lines: int | None = None,
) -> PipelineFailure:
    """Build a `PipelineFailure` for a failed run.

    Walks the run's jobs, picks the failed ones, then for each:
      - reads check-run annotations (when `check_run_url` is present)
      - reads the job log via the 302 redirect flow and extracts an excerpt

    A 403/404 on either side leaves `annotations=[]` or
    `log_excerpt=None`; an overall `note` is set when at least one job
    had unavailable logs.
    """
    jobs_r = client.get(
        f"{_repo_path(project)}/actions/runs/{run_id}/jobs",
        params={"filter": "latest"},
    )
    _check(jobs_r)
    jobs = (jobs_r.json() or {}).get("jobs", []) or []
    failing: list[FailingJob] = []
    logs_missing = False
    for job in jobs:
        if (job.get("conclusion") or "") != "failure":
            continue
        # Pick the first failed step to surface as `failed_step`.
        failed_step = ""
        for step in job.get("steps") or []:
            if (step.get("conclusion") or "") == "failure":
                failed_step = step.get("name") or ""
                break

        # Annotations live on the check-run associated with the job.
        annotations: list[dict] = []
        check_url = job.get("check_run_url") or ""
        if check_url:
            # `check_run_url` is absolute; httpx accepts that when we pass it
            # directly (base_url is ignored for absolute URLs).
            ann_r = client.get(f"{check_url}/annotations")
            if ann_r.is_success:
                annotations = ann_r.json() or []
            elif ann_r.status_code not in (403, 404):
                _check(ann_r)

        # Log excerpt via the 302 redirect.
        log_excerpt: str | None = None
        job_id = job.get("id")
        if job_id is not None:
            # When tail_lines is set we need the real end of the log, so we
            # bypass the default 256 KB cap by passing max_bytes=None.
            fetch_max = None if (tail_lines is not None and tail_lines > 0) else 256 * 1024
            log_text = _fetch_job_log(
                token,
                f"{_repo_path(project)}/actions/jobs/{job_id}/logs",
                max_bytes=fetch_max,
            )
            if log_text is None:
                logs_missing = True
            else:
                if tail_lines is not None and tail_lines > 0:
                    log_excerpt = "\n".join(log_text.splitlines()[-tail_lines:])
                else:
                    log_excerpt = _extract_log_excerpt(
                        log_text,
                        failed_step=failed_step or None,
                        annotations=annotations,
                    )

        failing.append(
            FailingJob(
                name=job.get("name") or "",
                url=job.get("html_url") or "",
                failed_step=failed_step,
                annotations=_normalize_gh_annotations(
                    annotations, job.get("name") or "",
                ),
                log_excerpt=log_excerpt,
                job_id=str(job.get("id") or ""),
            )
        )
    note = "logs unavailable" if logs_missing else None
    return PipelineFailure(failing_jobs=failing, note=note)
