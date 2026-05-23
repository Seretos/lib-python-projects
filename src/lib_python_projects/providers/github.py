"""GitHub provider — REST v3 implementation."""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from lib_python_projects.models import ProjectConfig
from lib_python_projects.markers import (
    AI_GENERATED_LABEL,
    AI_MODIFIED_LABEL,
    apply_body_marker,
    has_ai_generated_marker,
    LABEL_COLORS,
    LABEL_DESCRIPTIONS,
    ensure_body_prefix,
    ensure_comment_prefix,
    strip_leading_ai_marker,
)
from lib_python_projects.providers.base import (
    Comment,
    FailingJob,
    normalize_timestamp,
    PipelineFailure,
    PipelineRun,
    PRFilters,
    PullRequest,
    Relation,
    RelationKindUnsupported,
    RelationNotFound,
    Review,
    ReviewComment,
    Status,
    StatusSpec,
    Ticket,
    TicketFilters,
    TokenCapabilities,
)

log = logging.getLogger("project-issues.github")

USER_AGENT = "claude-code-project-issues-plugin/0.1.0"
ACCEPT = "application/vnd.github+json"
API_BASE = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message


def _client(token: str | None) -> httpx.Client:
    headers = {
        "Accept": ACCEPT,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API_BASE, headers=headers, timeout=30.0)


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
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "?")
        msg = f"rate-limited (reset unix={reset}); {msg}"
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


def _map_comment(raw: dict) -> Comment:
    return Comment(
        id=str(raw["id"]),
        author=(raw.get("user") or {}).get("login", ""),
        body=raw.get("body") or "",
        url=raw.get("html_url") or "",
        created_at=normalize_timestamp(raw.get("created_at") or ""),
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
        commit_sha=raw.get("commit_id") or raw.get("original_commit_id") or "",
        in_reply_to=str(in_reply_to) if in_reply_to is not None else None,
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
        url=raw.get("html_url") or "",
        discussion_id=discussion_id,
    )


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
) -> Relation:
    """POST to the Sub-Issues endpoint and return a `Relation` for the agent."""
    r = client.post(
        f"{parent_repo_path}/issues/{parent_issue_number}/sub_issues",
        json={"sub_issue_id": sub_issue_internal_id},
    )
    _check(r)
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
) -> Relation:
    """POST to the Dependencies endpoint (api 2026-03-10)."""
    r = client.post(
        f"{repo_path}/issues/{source_issue_number}/dependencies/{dep_endpoint}",
        json={"issue_id": target_internal_id},
    )
    _check(r)
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


def _github_mark_duplicate_of(
    client: httpx.Client,
    project: ProjectConfig,
    source_issue_number: str,
    *,
    target_number: str,
    target_raw: dict,
) -> Relation:
    """Mark `source` as duplicate of `target` via body edit + state change.

    GitHub has no native typed `duplicate_of` link surface; the read
    path detects duplicates from `state=closed AND
    state_reason="duplicate"` on the queried ticket (`_fetch_relations`
    at github.py:653-672), optionally cross-checked against a
    `Duplicate of #N` body regex. We replicate that here:

      1. GET the source issue to read its current body + labels.
      2. Append a `Duplicate of #N` line to the body (after the AI
         marker, so `apply_body_marker` keeps the marker correct).
      3. PATCH with state=closed, state_reason="duplicate", body=new.
    """
    src = _fetch_issue_payload(
        client, _repo_path(project), source_issue_number,
    )
    current_body = src.get("body") or ""
    current_labels = {lbl["name"] for lbl in (src.get("labels") or [])}
    will_be_ai_generated = AI_GENERATED_LABEL in current_labels

    dup_line = f"Duplicate of #{target_number}"
    # Insert the duplicate-of line AFTER the AI marker (re-stamped) but
    # at the start of the body so it's the first prose the reader
    # sees. apply_body_marker strips any existing leading #ai-* line.
    # Pre-strip marker so we can splice cleanly, then re-stamp via the
    # canonical helper.
    body_without_marker = strip_leading_ai_marker(current_body)
    if dup_line not in body_without_marker:
        if body_without_marker:
            new_body_core = f"{dup_line}\n\n{body_without_marker}"
        else:
            new_body_core = dup_line
    else:
        new_body_core = body_without_marker
    new_body = apply_body_marker(
        new_body_core, will_be_ai_generated=will_be_ai_generated,
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
    return Relation(
        kind=kind,
        ticket_id=ticket_id,
        title="",
        url=url,
        state="",
        is_pull_request=False,
        resolved=False,
    )


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
      - `duplicate_of` — outgoing, from `state == closed` and
        `state_reason == "duplicate"` on the queried ticket itself.
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

    if (
        issue_payload.get("state") == "closed"
        and issue_payload.get("state_reason") == "duplicate"
    ):
        m = re.search(
            r"duplicate\s+of\s+(?:(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)"
            r"/(?P<repo>[A-Za-z0-9._\-]+))?#(?P<num>\d+)",
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
                    _ref_to_relation(
                        m.group("owner"), m.group("repo"), num,
                        project, "duplicate_of",
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

    1. Drop exact (kind, ticket_id) duplicates — first occurrence wins.
    2. When a target has a stronger label, drop the generic
       `mentioned_by` / `mentions` for that same target.
    """
    seen: dict[tuple[str, str], Relation] = {}
    strong_kinds_by_target: dict[str, set[str]] = {}
    for r in rels:
        key = (r.kind, r.ticket_id)
        if key in seen:
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


def _ensure_label(client: httpx.Client, project: ProjectConfig, name: str) -> None:
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
        "color": LABEL_COLORS.get(name, "ededed"),
        "description": LABEL_DESCRIPTIONS.get(name, ""),
    }
    resp = client.post(f"{_repo_path(project)}/labels", json=payload)
    if resp.status_code in (200, 201):
        return
    if resp.status_code == 422:
        return  # already exists
    _check(resp)


def _ensure_label_best_effort(
    client: httpx.Client, project: ProjectConfig, name: str
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
        _ensure_label(client, project, name)
        return True
    except GitHubError as exc:
        log.warning(
            "could not ensure label '%s' on %s/%s: %s; falling back to "
            "body-prefix marker only",
            name, project.owner, project.repo, exc,
        )
        return False


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
    if filters.status in ("open", "closed"):
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


class GitHubProvider:
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

    def list_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> list[Ticket]:
        per_page = min(max(1, filters.limit), 100)
        # Normalize `not_labels=[]` (truthy-but-empty containers) to "not set".
        if not filters.not_labels:
            filters.not_labels = []
        with _client(token) as client:
            if filters.search or _requires_search(filters):
                items = _list_via_search(client, project, filters)
            else:
                params: dict[str, Any] = {
                    "per_page": per_page,
                    "state": filters.status if filters.status in ("open", "closed") else "all",
                    "sort": filters.sort_by,
                    "direction": filters.sort_order,
                }
                if filters.labels:
                    params["labels"] = ",".join(filters.labels)
                if filters.assignee:
                    params["assignee"] = filters.assignee
                r = client.get(f"{_repo_path(project)}/issues", params=params)
                _check(r)
                items = r.json()
            # The /issues endpoints include PRs; filter them out.
            return [_map_issue(it) for it in items if "pull_request" not in it]

    def get_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        *,
        include_relations: bool = True,
    ) -> tuple[Ticket, list[Comment], list[Relation] | None, bool | None]:
        """Fetch a single ticket with its comments and (optionally) relations.

        Returns `(ticket, comments, relations, relations_truncated)`.
        When `include_relations` is False, returns `(None, None)` for the
        relation fields and skips the extra API calls — callers must
        distinguish `None` (skipped) from `[]` (fetched but empty).
        """
        with _client(token) as client:
            r = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
            _check(r)
            issue_raw = r.json()
            ticket = _map_issue(issue_raw)
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
            else:
                relations, truncated = None, None
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
    ) -> Ticket:
        """Create an issue with the `ai-generated` AI-attribution marker.

        Marker policy (ticket Seretos/agent-marketplace#15):
          - Body prefix `#ai-generated\\n\\n` is the canonical source of
            truth and is always applied (idempotent).
          - The `ai-generated` LABEL is best-effort. If the caller cannot
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
        """
        # Deduplicate while preserving order, ensure ai-generated is present.
        merged = list(dict.fromkeys([*labels, AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        # Validate `status` up-front so an invalid value rejects before
        # the POST commits an issue we'd then have to delete or close.
        patch_state, patch_state_reason = _split_github_status(status)
        with _client(token) as client:
            label_ok = _ensure_label_best_effort(
                client, project, AI_GENERATED_LABEL
            )
            payload: dict[str, Any] = {
                "title": title,
                "body": prefixed_body,
            }
            if label_ok:
                payload["labels"] = merged
            else:
                # Drop only the AI marker; keep any caller-supplied labels
                # so an unrelated `bug` / `enhancement` label still lands.
                other = [lbl for lbl in merged if lbl != AI_GENERATED_LABEL]
                if other:
                    payload["labels"] = other
            if assignees:
                payload["assignees"] = assignees
            r = client.post(f"{_repo_path(project)}/issues", json=payload)
            _check(r)
            raw = r.json()
            if label_ok and not _label_present(raw, AI_GENERATED_LABEL):
                log.warning(
                    "ticket #%s created on %s/%s without '%s' label "
                    "(GitHub silently dropped it — caller likely lacks "
                    "triage permission); body-prefix marker remains",
                    raw.get("number"), project.owner, project.repo,
                    AI_GENERATED_LABEL,
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
            return _map_issue(raw)

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
    ) -> Ticket:
        with _client(token) as client:
            r0 = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
            _check(r0)
            current = r0.json()
            current_labels = {lbl["name"] for lbl in (current.get("labels") or [])}
            current_assignees = {a["login"] for a in (current.get("assignees") or [])}

            new_labels = set(current_labels)
            if labels_add:
                new_labels.update(labels_add)
            if labels_remove:
                new_labels.difference_update(labels_remove)

            # If this ticket wasn't created by us, mark it as AI-modified.
            # Label application is best-effort (see ticket #15): if the
            # caller can't create or apply the label, proceed without it
            # rather than blocking the legitimate update.
            will_be_ai_generated = AI_GENERATED_LABEL in current_labels
            if not will_be_ai_generated:
                if AI_MODIFIED_LABEL not in new_labels:
                    if _ensure_label_best_effort(
                        client, project, AI_MODIFIED_LABEL
                    ):
                        new_labels.add(AI_MODIFIED_LABEL)
                else:
                    new_labels.add(AI_MODIFIED_LABEL)

            new_assignees = set(current_assignees)
            if assignees_add:
                new_assignees.update(assignees_add)
            if assignees_remove:
                new_assignees.difference_update(assignees_remove)

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                # Re-stamp the body's `#ai-*` marker so it matches the
                # resource's label state after this update (ticket #44).
                # Caller should NOT prepend the marker themselves; if
                # they do, we strip + re-add the correct one.
                payload["body"] = apply_body_marker(
                    body, will_be_ai_generated=will_be_ai_generated,
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

            if not payload:
                # Nothing to do — return the current state.
                return _map_issue(current)

            r = client.patch(f"{_repo_path(project)}/issues/{ticket_id}", json=payload)
            _check(r)
            return _map_issue(r.json())

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
                "closed:completed": ["open"],
                "closed:not_planned": ["open"],
            },
            hints={
                "default_open": "open",
                "terminal": ["closed:completed", "closed:not_planned"],
                "terminal_completed": "closed:completed",
                "terminal_declined": "closed:not_planned",
            },
        )

    def add_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        body: str,
    ) -> Comment:
        prefixed = ensure_comment_prefix(body)
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                json={"body": prefixed},
            )
            _check(r)
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
            _check(r)
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
        _check(probe)
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
        has_more = cur >= 1
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
          - If the existing comment body carries `#ai-generated`, the
            edited body is re-stamped with `#ai-generated` (we wrote it
            originally, this is just another AI edit).
          - Otherwise the comment was human-authored — the edited body
            is stamped with `#ai-modified` to mirror the label
            distinction `update_ticket` makes between `ai-generated` and
            `ai-modified` resources.

        Comments don't carry labels, so the body marker is the only
        signal a reader has of authorship — getting it right here is
        important. Costs one extra GET before the PATCH.
        """
        with _client(token) as client:
            r0 = client.get(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
            )
            _check(r0)
            current_body = r0.json().get("body") or ""
            will_be_ai_generated = has_ai_generated_marker(current_body)
            prefixed = apply_body_marker(
                body, will_be_ai_generated=will_be_ai_generated,
            )
            r = client.patch(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
                json={"body": prefixed},
            )
            _check(r)
            return _map_comment(r.json())

    # ---------- pull requests ------------------------------------------------

    def list_prs(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: PRFilters,
    ) -> list[PullRequest]:
        """List pull requests for a project.

        Routing mirrors `list_tickets`: when `labels`, `assignee`, or
        `search` are set, switch from the cheap `/pulls` endpoint to
        `/search/issues` with `is:pr` so the additional filters can be
        expressed as Search qualifiers. The `head`/`base` filters work on
        both paths (Search via the `head:`/`base:` qualifiers).
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
                return [_map_pr(it) for it in full_items]
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
                    params["head"] = filters.head
                if filters.base:
                    params["base"] = filters.base
                r = client.get(f"{_repo_path(project)}/pulls", params=params)
                _check(r)
                items = r.json()
        return [_map_pr(it) for it in items]

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
    ) -> PullRequest:
        """Create a pull request, applying the AI-generated marker.

        Marker policy mirrors `create_ticket` (see ticket
        Seretos/agent-marketplace#15): body prefix is the canonical
        source of truth; the `ai-generated` LABEL is best-effort. When
        the caller lacks permission to create or apply the label, the
        PR is still created and the follow-up labels POST is skipped (or
        restricted to caller-supplied labels). Mode A silent-drop on the
        labels POST is detected and logged.

        Labels, assignees, and reviewer requests are applied in
        follow-up calls because the `POST /pulls` endpoint doesn't
        accept them inline.
        """
        merged_labels = list(dict.fromkeys([*(labels or []), AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        with _client(token) as client:
            label_ok = _ensure_label_best_effort(
                client, project, AI_GENERATED_LABEL
            )
            payload: dict[str, Any] = {
                "title": title,
                "body": prefixed_body,
                "head": head,
                "base": base,
                "draft": draft,
            }
            r = client.post(f"{_repo_path(project)}/pulls", json=payload)
            _check(r)
            pr_raw = r.json()
            pr_number = pr_raw["number"]
            # Apply labels via the issues endpoint (PRs share it). Skip
            # the call entirely when there's nothing to apply — including
            # the case where `ai-generated` couldn't be ensured and the
            # caller didn't supply any other labels.
            labels_to_apply = (
                merged_labels
                if label_ok
                else [lbl for lbl in merged_labels if lbl != AI_GENERATED_LABEL]
            )
            if labels_to_apply:
                lbl_resp = client.post(
                    f"{_repo_path(project)}/issues/{pr_number}/labels",
                    json={"labels": labels_to_apply},
                )
                _check(lbl_resp)
                applied_raw = lbl_resp.json()
                # Reflect the new labels back into the PR payload so the
                # returned dataclass advertises them.
                pr_raw["labels"] = applied_raw
                if label_ok and not _label_present(
                    {"labels": applied_raw}, AI_GENERATED_LABEL
                ):
                    log.warning(
                        "PR #%s created on %s/%s without '%s' label "
                        "(GitHub silently dropped it — caller likely "
                        "lacks triage permission); body-prefix marker "
                        "remains",
                        pr_number, project.owner, project.repo,
                        AI_GENERATED_LABEL,
                    )
            if assignees:
                a_resp = client.post(
                    f"{_repo_path(project)}/issues/{pr_number}/assignees",
                    json={"assignees": assignees},
                )
                _check(a_resp)
                # The /assignees endpoint returns the issue payload with
                # the updated assignee list; mirror it.
                pr_raw["assignees"] = a_resp.json().get("assignees") or []
            if requested_reviewers:
                rv_resp = client.post(
                    f"{_repo_path(project)}/pulls/{pr_number}/requested_reviewers",
                    json={"reviewers": requested_reviewers},
                )
                _check(rv_resp)
                pr_raw["requested_reviewers"] = (
                    rv_resp.json().get("requested_reviewers") or []
                )
            return _map_pr(pr_raw)

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

        Applies the `ai-modified` label (mirroring `update_ticket`) when
        the PR wasn't originally created by us.
        """
        with _client(token) as client:
            r0 = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            _check(r0)
            current = r0.json()
            current_labels = {lbl["name"] for lbl in (current.get("labels") or [])}
            current_assignees = {a["login"] for a in (current.get("assignees") or [])}

            new_labels = set(current_labels)
            if labels_add:
                new_labels.update(labels_add)
            if labels_remove:
                new_labels.difference_update(labels_remove)
            # `ai-modified` is best-effort (see ticket #15): if we can't
            # ensure the label exists, proceed without it rather than
            # failing the legitimate update.
            will_be_ai_generated = AI_GENERATED_LABEL in current_labels
            if not will_be_ai_generated:
                if AI_MODIFIED_LABEL not in new_labels:
                    if _ensure_label_best_effort(
                        client, project, AI_MODIFIED_LABEL
                    ):
                        new_labels.add(AI_MODIFIED_LABEL)
                else:
                    new_labels.add(AI_MODIFIED_LABEL)

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
                    body, will_be_ai_generated=will_be_ai_generated,
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
        prefixed = ensure_comment_prefix(body)
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/issues/{pr_id}/comments",
                json={"body": prefixed},
            )
            _check(r)
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
        prefixed = ensure_comment_prefix(body)
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
            _check(r)
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
            payload["body"] = ensure_comment_prefix(body)
        if commit_sha:
            payload["commit_id"] = commit_sha
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/pulls/{pr_id}/reviews",
                json=payload,
            )
            _check(r)
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
            r = client.put(
                f"{_repo_path(project)}/pulls/{pr_id}/merge", json=payload
            )
            _check(r)
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
            Canonical wire form is `child`: `add_relation(A, kind=parent,
            target=B)` is internally treated as
            `add_relation(B, kind=child, target=A)`.
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
        with _client(token) as client:
            # Resolve target to (cross_repo, issue_number, internal_id).
            target_repo, target_number = _parse_relation_target(target, project)
            target_internal_id, target_raw = _fetch_issue_internal_id(
                client, target_repo, target_number,
            )
            if kind == "parent":
                # parent(A→B): A's parent is B → POST /issues/B/sub_issues
                # with sub_issue_id=A. Source/target swap on the wire.
                return _github_post_sub_issue(
                    client, target_repo, target_number,
                    sub_issue_internal_id=_fetch_issue_internal_id(
                        client, _repo_path(project), ticket_id,
                    )[0],
                    relation_kind_for_caller="parent",
                    target_raw=_fetch_issue_payload(
                        client, target_repo, target_number,
                    ),
                    project=project,
                )
            if kind == "child":
                return _github_post_sub_issue(
                    client, _repo_path(project), ticket_id,
                    sub_issue_internal_id=target_internal_id,
                    relation_kind_for_caller="child",
                    target_raw=target_raw,
                    project=project,
                )
            if kind == "blocked_by":
                return _github_post_dependency(
                    client, _repo_path(project), ticket_id,
                    dep_endpoint="blocked_by",
                    target_internal_id=target_internal_id,
                    relation_kind_for_caller="blocked_by",
                    target_raw=target_raw,
                    project=project,
                )
            if kind == "blocks":
                # blocks(A→B): A blocks B → on B's endpoint, add A as
                # blocked_by. Swap source/target on the wire.
                source_internal_id, _ = _fetch_issue_internal_id(
                    client, _repo_path(project), ticket_id,
                )
                return _github_post_dependency(
                    client, target_repo, target_number,
                    dep_endpoint="blocked_by",
                    target_internal_id=source_internal_id,
                    relation_kind_for_caller="blocks",
                    target_raw=target_raw,
                    project=project,
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

        For `duplicate_of`, removal reopens the source issue (state →
        open, state_reason → cleared) but does NOT strip the `Duplicate
        of #N` line from the body — body history is preserved
        deliberately so a reader can see the historic intent. Removing
        the body line is the caller's job via `update_ticket(body=...)`.
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
            if kind == "child":
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
                    source_ref=f"#{target_number}",
                    target_ref=f"#{ticket_id}",
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
                # Reopen source — the read path's duplicate_of detection
                # is gated on `state=closed AND state_reason=duplicate`.
                pr = client.patch(
                    f"{_repo_path(project)}/issues/{ticket_id}",
                    json={"state": "open"},
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
    ) -> list[PipelineRun]:
        """List Actions workflow runs filtered by branch."""
        with _client(token) as client:
            runs = _list_runs_for_branch(client, project, branch, status, limit)
            return [_map_run(r) for r in runs]

    def list_runs_for_commit(
        self,
        project: ProjectConfig,
        token: str | None,
        sha: str,
        status: str = "all",
        limit: int = 10,
    ) -> list[PipelineRun]:
        """List runs whose `head_sha` matches `sha`."""
        with _client(token) as client:
            runs = _list_runs_for_commit(client, project, sha, status, limit)
            return [_map_run(r) for r in runs]

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

    def get_run(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
        include_failure_excerpt: bool = True,
    ) -> PipelineRun:
        """Fetch a single workflow run, optionally with failure context.

        When `include_failure_excerpt` is True AND the run concluded as
        failed, populates `run.failure` with per-failing-job annotations
        and a small log excerpt. In-progress runs (`conclusion=None`)
        never trigger the failure-context fetch.
        """
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/actions/runs/{run_id}"
            )
            _check(r)
            raw = r.json()
            run = _map_run(raw)
            if (
                include_failure_excerpt
                and run.conclusion == "failure"
                and run.status == "completed"
            ):
                run.failure = _get_failure_excerpt(client, project, token, run_id)
            return run


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

    # Fetch the ticket once to read its body (for the `branch:foo` hint)
    # and to bail early on a 404.
    issue_r = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
    if issue_r.status_code == 404:
        return []
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
    pattern = re.compile(r"(error|failed|##\[error\])", re.IGNORECASE)
    scan_offset = 0
    if groups:
        scan_offset = groups[0][0] + 1
    for idx in range(scan_offset, len(lines)):
        if pattern.search(lines[idx]):
            start = max(scan_offset, idx - 2)
            end = min(len(lines), idx + max_lines)
            return "\n".join(lines[start:end])

    # --- 4) Tail fallback ----------------------------------------------------
    return "\n".join(lines[-max_lines:])


def _fetch_job_log(token: str | None, log_url: str) -> str | None:
    """Fetch a job log via the 302-redirect signed-URL flow.

    GitHub responds with a 302 to a short-lived signed URL on a
    different host (typically blob.core.windows.net for hosted runners).
    httpx removes the Authorization header on cross-host redirects,
    which is the correct behavior — the signed URL carries its own auth.
    Returns `None` on 403/404 so the caller can mark logs as unavailable.

    Uses `follow_redirects=True` ONLY for this call (the default `_client`
    leaves it False, which is correct for the JSON API calls).
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
        # Cap the read to ~256 KB so a runaway log doesn't blow memory.
        content = r.content[: 256 * 1024]
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return None


def _get_failure_excerpt(
    client: httpx.Client,
    project: ProjectConfig,
    token: str | None,
    run_id: str,
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
            log_text = _fetch_job_log(
                token, f"{_repo_path(project)}/actions/jobs/{job_id}/logs"
            )
            if log_text is None:
                logs_missing = True
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
                annotations=annotations,
                log_excerpt=log_excerpt,
            )
        )
    note = "logs unavailable" if logs_missing else None
    return PipelineFailure(failing_jobs=failing, note=note)
