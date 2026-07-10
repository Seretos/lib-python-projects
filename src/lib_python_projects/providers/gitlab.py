"""GitLab provider — REST v4 implementation.

Counterpart to `providers/github.py`. Maps GitLab REST API v4 onto the
provider-agnostic dataclasses defined in `providers/base.py`. The
caller (`tools/*`) never sees GitLab-isms leak through.

GitLab vs GitHub — naming map:
  - GitHub "issue"        ↔ GitLab "issue" (uses `iid`, not `id`)
  - GitHub "pull request" ↔ GitLab "merge request" (`PullRequest`)
  - GitHub "comment"      ↔ GitLab "note" (`Comment`)
  - GitHub "workflow run" ↔ GitLab "pipeline" (`PipelineRun`)

GitLab does not split `state` and `state_reason` the way GitHub does;
closed issues are simply `closed`. The marker label
`ai-closed-not-planned` (from `markers.py`) is the stand-in agents can
use to express "won't do" semantics — applied by the caller, not by
this provider.

Auth: `PRIVATE-TOKEN: <pat>` header. OAuth-Bearer flow is not in the
initial pass — callers needing it can set `base_url` to a proxy that
rewrites the header.

Project addressing: the GitLab REST API accepts an URL-encoded project
path (`group/sub/project` → `group%2Fsub%2Fproject`) as the `:id`
segment everywhere. `_project_path()` centralises that.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from lib_python_projects.models import ProjectConfig
from lib_python_projects.markers import (
    AI_GENERATED_LABEL,
    AI_MODIFIED_LABEL,
    apply_body_marker,
    ensure_body_prefix,
    ensure_comment_prefix,
    has_ai_generated_marker,
    strip_leading_ai_marker,
)
from lib_python_projects.providers.base import (
    Comment,
    DiscoveredProject,
    FailingJob,
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
    ReviewComment,
    Status,
    StatusSpec,
    Ticket,
    TicketFilters,
    TokenCapabilities,
    TokenCapabilityProvider,
    TokenProjectDiscoveryProvider,
    _assert_not_self_relation,
    _validate_label_lists,
    _validate_limit,
)
from lib_python_projects.providers._http_cache import make_cached_transport

log = logging.getLogger("project-issues.gitlab")

USER_AGENT = "claude-code-project-issues-plugin/0.1.0"
DEFAULT_BASE_URL = "https://gitlab.com"

# GitLab sometimes returns {"message": "404 Not Found"} whose numeric prefix
# duplicates the HTTP status code already in GitLabError.__str__. Strip it.
_STATUS_PREFIX_RE = re.compile(r"^\d{3}\s+")

# When a GitLabError is re-raised with str(exc) as the message the "GitLab NNN: "
# prefix ends up duplicated. Strip it from the incoming message so the final
# __str__ form remains "GitLab NNN: <clean message>".
_GITLAB_PREFIX_RE = re.compile(r"^GitLab\s+\d+:\s*")


class GitLabError(ProviderError):
    """Raised on any non-success response from the GitLab REST API.

    Mirrors `GitHubError` so `tools/_providers.py::_safe` can translate
    both into the same `{"error": "<message>"}` shape.
    """

    def __init__(self, status: int, message: str):
        # Strip any leading "GitLab NNN: " prefix to avoid the message ending
        # up as "GitLab 404: GitLab 404: Not Found" when errors are re-wrapped.
        cleaned = _GITLAB_PREFIX_RE.sub("", message) if message else message
        RuntimeError.__init__(self, f"GitLab {status}: {cleaned}")
        self.status = status
        self.message = cleaned


# ---------- client / request helpers -----------------------------------------


def _base_url(project: ProjectConfig) -> str:
    """Resolve the GitLab REST root for a project.

    Honours `project.base_url` for self-hosted instances. Strips any
    trailing slash so concatenation stays predictable.
    """
    base = (project.base_url or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/api/v4"


def _client(project: ProjectConfig, token: str | None) -> httpx.Client:
    """Build a configured httpx client for a GitLab project.

    The token is sent via the `PRIVATE-TOKEN` header — GitLab's
    preferred form for PATs. Unset token is fine for public-project
    read calls; write calls error out at the API.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["PRIVATE-TOKEN"] = token
    return httpx.Client(
        base_url=_base_url(project),
        headers=headers,
        timeout=30.0,
        transport=make_cached_transport(),
    )


def _discovery_client(base_url: str, token: str) -> httpx.Client:
    """Build a configured httpx client for token-driven project discovery.

    Parallel to `_client` but accepts a raw base URL string instead of a
    `ProjectConfig`. Used by `GitLabProvider.discover_projects`.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "PRIVATE-TOKEN": token,
    }
    return httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=30.0,
        transport=make_cached_transport(),
    )


def _extract_access_level(permissions: dict) -> int | None:
    """Extract the effective access level from a GitLab project permissions dict.

    GitLab returns both ``project_access`` and ``group_access``; we take the
    maximum of whichever values are present so an inherited group role is not
    silently lost.

    Returns ``None`` when both fields are absent or null.
    """
    pa = (permissions.get("project_access") or {}).get("access_level")
    ga = (permissions.get("group_access") or {}).get("access_level")
    values = [v for v in (pa, ga) if isinstance(v, int)]
    return max(values) if values else None


def _capabilities_from_access_level(level: int | None) -> TokenCapabilities:
    """Map a GitLab numeric access level to ``TokenCapabilities``.

    GitLab access-level constants:
      - Owner / Maintainer: >= 40  → full write access
      - Developer: 30              → issues + MR create, no edit/merge
      - Reporter / Guest: < 30     → read-only
      - None (field missing)       → unknown, all False
    """
    if level is None:
        return TokenCapabilities(reason="permissions_field_missing")
    if level >= 40:
        return TokenCapabilities(
            issues_create=True,
            issues_modify=True,
            pulls_create=True,
            pulls_modify=True,
            pulls_merge=True,
            reason=None,
        )
    if level >= 30:
        return TokenCapabilities(
            issues_create=True,
            issues_modify=True,
            pulls_create=True,
            pulls_modify=False,
            pulls_merge=False,
            reason=None,
        )
    return TokenCapabilities(reason="insufficient_scope")


def _check(resp: httpx.Response) -> None:
    """Raise `GitLabError` (or `RateLimitError`) for any non-2xx response.

    GitLab error payloads come in three shapes:
      - `{"message": "..."}` for most errors
      - `{"error": "...", "error_description": "..."}` for OAuth
      - `{"message": {"field": ["err"]}}` for validation failures

    We collapse all three into one string so the caller gets a single
    consistent message.
    """
    if resp.status_code == 304:
        return
    if resp.is_success:
        return
    msg: str
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            raw = payload.get("message") or payload.get("error") or ""
            if isinstance(raw, dict):
                # Validation failures: {"message": {"field": ["err"]}}
                parts = [f"{k}: {v}" for k, v in raw.items()]
                msg = "; ".join(parts) or resp.reason_phrase
            else:
                msg = str(raw) or resp.reason_phrase
            # Strip leading "NNN " prefix (e.g. "404 Not Found") to avoid
            # "GitLab 404: 404 Not Found" double-status in the error string.
            if _STATUS_PREFIX_RE.match(msg):
                msg = _STATUS_PREFIX_RE.sub("", msg, count=1)
            extra = payload.get("error_description")
            if extra:
                msg = f"{msg} ({extra})"
        else:
            msg = resp.reason_phrase or "request failed"
    except Exception:
        msg = resp.reason_phrase or "request failed"
    if resp.status_code == 429:
        retry_after: int | None = None
        retry_after_hdr = resp.headers.get("Retry-After")
        if retry_after_hdr is not None:
            try:
                retry_after = int(retry_after_hdr)
            except (ValueError, TypeError):
                retry_after = None
        if retry_after is None:
            reset_hdr = resp.headers.get("RateLimit-Reset")
            if reset_hdr is not None:
                try:
                    retry_after = max(0, int(reset_hdr) - int(time.time()))
                except (ValueError, TypeError):
                    retry_after = None
        if retry_after is None:
            reset_time_hdr = resp.headers.get("RateLimit-ResetTime")
            if reset_time_hdr is not None:
                try:
                    retry_after = max(0, int(reset_time_hdr) - int(time.time()))
                except (ValueError, TypeError):
                    retry_after = None
        raise RateLimitError(429, msg, retry_after=retry_after)
    raise GitLabError(resp.status_code, msg)


def _project_path(project: ProjectConfig) -> str:
    """URL-encoded project identifier for use as the `:id` path segment.

    GitLab accepts either the numeric project id or the URL-encoded
    namespace path. We always use the path form because it round-trips
    cleanly from the YAML config.
    """
    if not project.path:
        raise ValueError(
            f"project '{project.id}' has no 'path' configured — "
            f"GitLab requires a namespace path (e.g. 'group/sub/project')"
        )
    # `safe=""` so slashes get encoded — GitLab requires that.
    return quote(project.path, safe="")


_CANONICAL_URL_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("/-/work_items/", "/-/issues/"),
)


def _canonical_url(url: str, project: ProjectConfig) -> str:
    """Canonicalise a GitLab `web_url` to a stable cross-tool form.

    Two transforms (ticket #49 findings 3 & 4):

      1. Rewrite `/-/work_items/N` → `/-/issues/N`. The Work Items beta
         URL family doesn't match comment / MR URL stems, so all
         downstream callers stay on the legacy `/-/issues/` path which
         the GitLab REST API also targets.
      2. Lowercase the project path segment (case-insensitive). GitLab
         returns inconsistent casing between endpoints — sometimes
         `Seredos/gitlab-tests`, sometimes `seredos/gitlab-tests`. We
         normalise to lowercase so an agent comparing URLs across
         tools sees a single canonical form.

    Returns the URL unchanged when `project.path` is empty or `url` is
    falsy. Anything off-host (no project-path segment match) is left
    untouched.
    """
    if not url or not project.path:
        return url
    for src, dst in _CANONICAL_URL_REPLACEMENTS:
        url = url.replace(src, dst)
    path_lower = project.path.lower()
    if project.path != path_lower:
        # Match `<scheme://host/><path><boundary>` so we don't accidentally
        # touch a substring that happens to coincide with the path.
        pattern = re.compile(
            r"(://[^/]+/)" + re.escape(project.path) + r"(?=/|$|\?|#)",
            re.IGNORECASE,
        )
        url = pattern.sub(rf"\1{path_lower}", url, count=1)
    return url


# ---------- state normalisation ----------------------------------------------


def _normalise_gl_state(state: str) -> str:
    """Normalise a raw GitLab state string to the canonical relation vocab.

    GitLab issues and MRs use ``"opened"`` where the provider-agnostic
    surface uses ``"open"``.  This helper is the single source of truth
    so the mapping is applied consistently at every ``Relation`` construction
    site (issue links, closing MRs, ``_map_issue``, ``_map_pr``).

    Returns:
      - ``"open"``   for ``opened`` / ``reopened``
      - ``"merged"`` for ``merged``
      - ``"closed"`` for ``closed``
      - ``""``       for anything else (e.g. empty / unknown)
    """
    if state in ("opened", "reopened"):
        return "open"
    if state == "merged":
        return "merged"
    if state == "closed":
        return "closed"
    return ""


# ---------- mappers ----------------------------------------------------------


def _map_issue(raw: dict, project: ProjectConfig | None = None) -> Ticket:
    """Translate a GitLab issue payload into a `Ticket`.

    Status mapping:
      - GitLab `opened`/`reopened` → `"open"`
      - GitLab `closed`            → `"closed"`

    GitLab does not have a `state_reason` equivalent; the
    `ai-closed-not-planned` LABEL is the agent-side convention for
    "won't do" semantics (see `markers.py`).

    `project` (optional) lets us canonicalise the returned `url`
    (lowercase the project path segment, rewrite `/-/work_items/N` to
    `/-/issues/N` — ticket #49 findings 3 & 4).
    """
    gl_state = raw.get("state", "opened")
    norm = _normalise_gl_state(gl_state)
    status: Status = norm if norm in ("open", "closed") else "closed"
    author = raw.get("author") or {}
    url = raw.get("web_url") or ""
    if project is not None:
        url = _canonical_url(url, project)
    return Ticket(
        id=str(raw["iid"]),  # IID — project-scoped; matches user-visible URL
        title=raw.get("title") or "",
        body=raw.get("description") or "",
        status=status,
        author=author.get("username", ""),
        assignees=[
            a.get("username", "") for a in (raw.get("assignees") or [])
        ],
        labels=list(raw.get("labels") or []),
        url=url,
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
    )


def _map_note(
    raw: dict,
    project: ProjectConfig | None = None,
    mr_iid: str | int | None = None,
) -> Comment:
    """Translate a GitLab note (comment) payload into a `Comment`.

    System notes (state changes, label edits, etc.) carry
    `"system": true`. They are NOT filtered here — callers that want
    to skip system notes do so at the list-comments call site.

    `url` handling (ticket #41 addendum A): GitLab note payloads do
    NOT include a `web_url` field. When `project` is supplied we
    synthesise the canonical anchor URL. URL precedence:

    1. ``raw.get("web_url")`` — used if non-empty.
    2. If `mr_iid` is not None — synthesise directly from
       ``{project.web_url}/-/merge_requests/{mr_iid}#note_{note_id}``,
       bypassing the ``noteable_iid``/``noteable_type`` payload fields
       which GitLab does not reliably include on MR-note write responses.
    3. Fallback: the existing ``noteable_iid``/``noteable_type`` payload-
       based synthesis (unchanged — used for issue-note and list paths).

    Falls back to empty string when `project` is omitted or the
    payload lacks the noteable hints (e.g. legacy responses).

    Args:
        raw: Raw GitLab note payload dict.
        project: The project config, used for ``web_url`` synthesis.
        mr_iid: When set, the MR iid is used for URL synthesis instead
            of relying on ``noteable_iid``/``noteable_type`` in the
            payload (which GitLab omits on MR-note POST responses).
    """
    author = raw.get("author") or {}
    raw_url = raw.get("web_url") or ""
    if not raw_url and project is not None and project.web_url:
        note_id = raw.get("id")
        if mr_iid is not None and note_id is not None:
            raw_url = (
                f"{project.web_url}/-/merge_requests/{mr_iid}"
                f"#note_{note_id}"
            )
        else:
            noteable_iid = raw.get("noteable_iid")
            noteable_type = (raw.get("noteable_type") or "").lower()
            if noteable_iid is not None and note_id is not None:
                segment = (
                    "merge_requests"
                    if noteable_type == "mergerequest"
                    else "issues"
                )
                raw_url = (
                    f"{project.web_url}/-/{segment}/{noteable_iid}"
                    f"#note_{note_id}"
                )
    if project is not None:
        raw_url = _canonical_url(raw_url, project)
    return Comment(
        id=str(raw["id"]),
        author=author.get("username", ""),
        body=raw.get("body") or "",
        url=raw_url,
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
    )


def _map_mergeable(raw: dict) -> bool | None:
    """Translate GitLab's merge-status field into a tri-state bool.

    GitLab returns one of `detailed_merge_status` (preferred, GitLab
    13.0+) or the legacy `merge_status`. Mapping:
      - `mergeable`, `can_be_merged`              → True
      - any `cannot_be_merged*` value             → False
      - `checking`, `unchecked`, missing, unknown → None
    """
    raw_status = raw.get("detailed_merge_status") or raw.get("merge_status")
    if not raw_status:
        return None
    if raw_status in ("mergeable", "can_be_merged"):
        return True
    if raw_status.startswith("cannot_be_merged"):
        return False
    return None


def _map_mr(
    raw: dict,
    project: ProjectConfig | None = None,
    approvals: dict | None = None,
) -> PullRequest:
    """Translate a GitLab merge-request payload into a `PullRequest`.

    Status mapping:
      - GitLab `opened`/`reopened` → `"open"`
      - GitLab `closed`            → `"closed"`
      - GitLab `merged`            → `"merged"`
      - GitLab `locked`            → `"closed"` (treat as terminal)

    `head` / `base` use GitLab's `source_branch` / `target_branch`. The
    SHA comes from `sha` on the MR root. `repo_full_name` is the
    target project path; cross-fork sources are not resolved into a
    full name here (would require an extra round-trip — defer).

    Approval state: GitLab's `/projects/:id/merge_requests/:iid` root
    payload does NOT include approval data — that lives behind a
    separate `/approvals` endpoint. When the caller has already fetched
    that payload, it can pass it via `approvals=...` and we derive
    `review_decision` + `approvals_received` + `approvals_required`
    from it. When `approvals` is `None` (the cheaper `list_prs` path),
    we fall back to whatever (usually `None`) the MR root happened to
    carry and leave `review_decision` at the dataclass default of
    `None`.

    Note on `diff_refs` / `base.sha` (Issue 7):
      GitLab only populates `diff_refs` (which carries `base_sha`) on MR
      payloads that have been through at least one pipeline or diff
      computation. On freshly-created MR payloads (e.g. the response from
      `create_pr`) `diff_refs` is absent and therefore `base.sha` is
      `None`. The field is reliably populated on a subsequent `get_pr` call.
    """
    state = raw.get("state", "opened")
    status: str = _normalise_gl_state(state) or "closed"
    merged = state == "merged" or bool(raw.get("merged_at"))
    author = raw.get("author") or {}
    # Reviewers: GitLab MR `reviewers` is the assigned list. There's no
    # "submitted vs requested" split — surface both under the same data.
    reviewer_usernames = [
        r.get("username", "") for r in (raw.get("reviewers") or [])
    ]
    source_project_id = raw.get("source_project_id")
    project_id = raw.get("project_id")
    if source_project_id and project_id and source_project_id != project_id:
        repo_full_name = raw.get("source_project_path") or None
    else:
        repo_full_name = (project.path if project is not None else None) or None
    head = {
        "ref": raw.get("source_branch", "") or "",
        "sha": raw.get("sha", "") or "",
        "repo_full_name": repo_full_name,
    }
    diff_refs = raw.get("diff_refs") or {}
    base = {
        "ref": raw.get("target_branch", "") or "",
        "sha": diff_refs.get("base_sha") or None,
    }
    head_pipeline = raw.get("head_pipeline") or raw.get("pipeline") or {}
    pipeline_status = head_pipeline.get("status") if head_pipeline else None
    mr_url = raw.get("web_url") or ""
    if project is not None:
        mr_url = _canonical_url(mr_url, project)

    # Approval state. Two paths:
    #   (a) caller passed an `/approvals` payload → derive
    #       review_decision + approvals_received from it.
    #   (b) caller passed nothing → preserve historical behavior
    #       (read whatever the MR root carries; review_decision stays
    #       at the dataclass default of None).
    if approvals is not None:
        approvals_required: int | None = int(
            approvals.get("approvals_required") or 0
        )
        approved_by = approvals.get("approved_by") or []
        approvals_received: int | None = len(approved_by)
        if "approved" in approvals:
            approved = bool(approvals.get("approved"))
        else:
            # Older GitLab editions don't surface `approved` directly;
            # derive from approvals_left when the gate is configured.
            approved = (
                approvals_required > 0
                and int(approvals.get("approvals_left") or 0) == 0
            )
        # Decision logic (#52 F9):
        #   - Gate configured (approvals_required > 0): trust the
        #     `approved` boolean. `approved=True` means the gate is
        #     satisfied → APPROVED. Anything else (including partial
        #     approvals like 1-of-2) → REVIEW_REQUIRED.
        #   - No gate (approvals_required == 0): an ad-hoc approve
        #     still counts. If `approved_by` is non-empty, surface
        #     APPROVED so consumers can tell "someone approved" apart
        #     from "no review yet". Empty list → None (truly nothing
        #     happened). This is the case the sandbox hit when the
        #     original F9 partial-fix surfaced approvals_received but
        #     left review_decision at None.
        review_decision: str | None
        if approvals_required > 0:
            review_decision = "APPROVED" if approved else "REVIEW_REQUIRED"
        elif approved_by:
            review_decision = "APPROVED"
        else:
            review_decision = None
    else:
        approvals_required = raw.get("approvals_required")
        approvals_received = raw.get("approvals_received")
        review_decision = None

    return PullRequest(
        id=str(raw["iid"]),
        number=int(raw["iid"]),
        title=_DRAFT_PREFIX_RE.sub("", raw.get("title") or ""),
        body=raw.get("description") or "",
        status=status,  # type: ignore[arg-type]
        draft=bool(raw.get("draft") or raw.get("work_in_progress")),
        author=author.get("username", ""),
        assignees=[
            a.get("username", "") for a in (raw.get("assignees") or [])
        ],
        reviewers=list(reviewer_usernames),
        requested_reviewers=list(reviewer_usernames),
        labels=list(raw.get("labels") or []),
        head=head,
        base=base,
        merged=merged,
        mergeable=_map_mergeable(raw),
        url=mr_url,
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or ""),
        merge_commit_sha=raw.get("merge_commit_sha"),
        detailed_merge_status=raw.get("detailed_merge_status"),
        pipeline_status=pipeline_status,
        approvals_required=approvals_required,
        approvals_received=approvals_received,
        review_decision=review_decision,
    )


# GitLab pipeline statuses we treat as terminal — anything else is
# in-flight and yields `conclusion=None` on the common dataclass.
_TERMINAL_PIPELINE_STATUSES = {"success", "failed", "canceled", "skipped"}


def _map_pipeline_run(raw: dict) -> PipelineRun:
    """Translate a GitLab pipeline payload into a `PipelineRun`.

    Mapping nuances vs GitHub `workflow_run`:
      - `name`: GitLab pipelines have no single "workflow name"; fall
        back to `f"pipeline-{id}"` so the field is always populated.
      - `event`: GitLab's `source` field is the closest equivalent.
      - `status` / `conclusion`: GitLab statuses that mean "terminal"
        (success/failed/canceled/skipped) are folded into
        `status="completed", conclusion=<that_value>` so the response
        shape matches GitHub. Non-terminal values pass through.
      - `run_attempt`: GitLab has no per-pipeline attempt counter.
        Defaults to 1; not retrievable from the REST root.
    """
    raw_status = raw.get("status") or ""
    if raw_status in _TERMINAL_PIPELINE_STATUSES:
        status = "completed"
        conclusion: str | None = raw_status
    else:
        status = raw_status or "unknown"
        conclusion = None
    pipeline_id = raw.get("id")
    return PipelineRun(
        id=str(pipeline_id) if pipeline_id is not None else "",
        name=f"pipeline-{pipeline_id}" if pipeline_id is not None else "",
        branch=raw.get("ref", "") or "",
        head_sha=raw.get("sha", "") or "",
        event=raw.get("source", "") or "",
        status=status,
        conclusion=conclusion,
        url=raw.get("web_url") or "",
        created_at=normalize_timestamp(raw.get("created_at") or ""),
        updated_at=normalize_timestamp(raw.get("updated_at") or raw.get("finished_at") or ""),
        run_attempt=1,
        failure=None,
    )


# ---------- helpers used by GitLabProvider methods ---------------------------


def _parse_gitlab_relation_target(
    target: str, project: ProjectConfig,
) -> tuple[str, str]:
    """Parse a relation target into (project_path, issue_iid_as_str).

    Accepts:
      - `"!N"` / `"#N"` / `"N"` — same-project as `project`.
      - `"group/project#N"` — cross-project (raises NotImplementedError
        for now; reserved surface).
    """
    raw = target.strip()
    if not raw:
        raise ValueError("relation target is empty")
    if "/" in raw and ("#" in raw or "!" in raw):
        raise NotImplementedError(
            "cross-project relation targets are not yet supported"
        )
    iid_part = raw.lstrip("#!")
    if not iid_part.isdigit():
        raise ValueError(
            f"invalid relation target {target!r}: expected '#N' / '!N' "
            f"(same-project issue iid)"
        )
    return _project_path(project), iid_part


def _gitlab_link_type(kind: str) -> str:
    """Map our kind vocabulary to GitLab's `link_type` string.

    The ``blocks`` / ``blocked_by`` branches below are retained for
    read-side symmetry (``_fetch_relations`` maps the raw ``link_type``
    values back to our vocabulary).  They are unreachable from
    ``add_relation`` / ``remove_relation`` because those methods guard
    on ``_SUPPORTED_RELATION_KINDS`` — which no longer includes
    ``"blocks"`` / ``"blocked_by"`` (ticket #20) — before reaching this
    helper.
    """
    if kind == "blocks":
        return "blocks"
    if kind == "blocked_by":
        return "is_blocked_by"
    if kind == "relates_to":
        return "relates_to"
    raise ValueError(f"unmappable kind {kind!r} for GitLab issue links")


def _resolve_gitlab_project_numeric_id(
    client: httpx.Client,
    project_path: str,
) -> int:
    """Resolve a GitLab project (by URL-encoded path) to its numeric id.

    Ticket #49 finding 2 root cause: the issue-links endpoint
    (`POST /projects/:id/issues/:iid/links`) accepts a URL-encoded
    path for the `target_project_id` body field, but rejects mixed
    case from path. The numeric id is unambiguous and round-trips
    cleanly, so we always resolve to the integer before posting.
    """
    r = client.get(f"/projects/{project_path}")
    _check(r)
    pid = r.json().get("id")
    if not isinstance(pid, int):
        raise GitLabError(
            500,
            f"GitLab returned no numeric id for project '{project_path}'",
        )
    return pid


def _gitlab_post_issue_link(
    client: httpx.Client,
    source_project_path: str,
    source_iid: str,
    *,
    target_project_path: str,
    target_issue_iid: str,
    link_type: str,
    relation_kind_for_caller: str,
    project: ProjectConfig,
) -> Relation:
    """POST to the Issue Links endpoint and return a `Relation`.

    Uses the numeric project id for the `target_project_id` body
    field (resolved via `_resolve_gitlab_project_numeric_id`). The
    path-based form was case-sensitive in practice and produced
    misleading `404 Project Not Found` responses for kinds other
    than `duplicate_of` — ticket #49 finding 2 follow-up.
    """
    target_numeric_id = _resolve_gitlab_project_numeric_id(
        client, target_project_path,
    )
    body: dict[str, Any] = {
        "target_project_id": target_numeric_id,
        "target_issue_iid": target_issue_iid,
        "link_type": link_type,
    }
    r = client.post(
        f"/projects/{source_project_path}/issues/{source_iid}/links",
        json=body,
    )
    try:
        _check(r)
    except GitLabError as exc:
        if exc.status == 409 or (
            exc.status in (400, 409, 422)
            and ("already assigned" in exc.message.lower() or "already exists" in exc.message.lower())
        ):
            raise RelationAlreadyExists(
                kind=relation_kind_for_caller,
                ticket_id=source_iid,
                target=f"#{target_issue_iid}",
            ) from exc
        raise
    raw = r.json()
    # The POST /issues/:iid/links response wraps the target issue inside
    # a "target_issue" key: {"source_issue": {...}, "target_issue": {...}}.
    # Read from that nested dict; fall back to top-level for compatibility
    # with older GitLab versions that may return the flat shape.
    target = raw.get("target_issue") or raw
    target_url = (
        target.get("web_url")
        or target.get("target_web_url")
        or raw.get("web_url")
        or raw.get("target_web_url")
        or ""
    )
    return Relation(
        kind=relation_kind_for_caller,
        ticket_id=f"#{target_issue_iid}",
        title=target.get("title") or "",
        url=_canonical_url(target_url, project),
        state=_normalise_gl_state(target.get("state") or ""),
        is_pull_request=False,
        resolved=True,
    )


def _gitlab_delete_issue_link(
    client: httpx.Client,
    source_project_path: str,
    source_iid: str,
    *,
    target_project_path: str,
    target_issue_iid: str,
    kind: str = "relates_to",
) -> None:
    """Find the link id between source and target, then DELETE it."""
    r = client.get(
        f"/projects/{source_project_path}/issues/{source_iid}/links",
    )
    _check(r)
    link_id: int | None = None
    for link in r.json() or []:
        # Each link entry exposes the OTHER issue's fields and an
        # `issue_link_id` we need for deletion.
        if (
            str(link.get("iid")) == str(target_issue_iid)
            and link.get("issue_link_id") is not None
        ):
            link_id = link["issue_link_id"]
            break
    if link_id is None:
        raise RelationNotFound(
            kind=kind,
            ticket_id=source_iid,
            target=f"#{target_issue_iid}",
        )
    r2 = client.delete(
        f"/projects/{source_project_path}/issues/{source_iid}"
        f"/links/{link_id}",
    )
    _check(r2)


def _gitlab_mark_duplicate_of(
    client: httpx.Client,
    project: ProjectConfig,
    source_iid: str,
    *,
    target_project_path: str,
    target_iid: str,
) -> Relation:
    """Mark `source` as duplicate of `target` on GitLab.

    No native duplicate-link type exists, so we emulate it with:
      1. a `relates_to` issue link as a structured spur,
      2. body-edit appending `Duplicate of #N`,
      3. `state_event=close`.

    Ordering matters (ticket #49 finding 2): the link write — the
    operation most likely to fail — happens FIRST. If it raises, the
    body and state stay untouched (atomic "either succeed or no-op"
    semantics, matching what an agent reasonably expects after seeing
    `{"error": ...}`).

    Sigil note: the body-prefix uses `#N` (issue sigil) not `!N` (MR
    sigil) — fixes finding 2's secondary nit.

    The body edit is run through `apply_body_marker` so the
    AI-attribution marker stays consistent.
    """
    path = _project_path(project)
    src_r = client.get(f"/projects/{path}/issues/{source_iid}")
    _check(src_r)
    src = src_r.json()
    current_body = src.get("description") or ""
    current_labels = set(src.get("labels") or [])
    will_be_ai_generated = AI_GENERATED_LABEL in current_labels

    # Step 1: write the structured link FIRST so any failure surfaces
    # before we mutate the body / close the issue. If the link already
    # exists (GitLab 409s), treat as "already linked" — fall through
    # to body+close which are idempotent on their own.
    relation: Relation | None = None
    try:
        relation = _gitlab_post_issue_link(
            client, path, source_iid,
            target_project_path=target_project_path,
            target_issue_iid=target_iid,
            link_type="relates_to",
            relation_kind_for_caller="duplicate_of",
            project=project,
        )
    except RelationAlreadyExists:
        # The relates_to link already exists (raised by _gitlab_post_issue_link
        # on a 409 response). Treat as "already linked" — fall through to
        # body+close which are idempotent.
        pass
    except GitLabError as exc:
        if exc.status != 409:
            raise  # Propagate — body / state stay untouched.

    # Step 2 + 3: body prefix + close.
    dup_line = f"Duplicate of #{target_iid}"
    body_without_marker = strip_leading_ai_marker(current_body)
    if dup_line not in body_without_marker:
        new_body_core = (
            f"{dup_line}\n\n{body_without_marker}"
            if body_without_marker
            else dup_line
        )
    else:
        new_body_core = body_without_marker
    new_body = apply_body_marker(
        new_body_core, will_be_ai_generated=will_be_ai_generated,
    )
    payload: dict[str, Any] = {
        "description": new_body,
        "state_event": "close",
    }
    pu = client.put(f"/projects/{path}/issues/{source_iid}", json=payload)
    _check(pu)

    if relation is not None:
        return relation
    # 409 path: synthesise a Relation from the target issue payload.
    tg = client.get(
        f"/projects/{target_project_path}/issues/{target_iid}",
    )
    _check(tg)
    tj = tg.json()
    target_url = _canonical_url(tj.get("web_url") or "", project)
    return Relation(
        kind="duplicate_of",
        ticket_id=f"#{target_iid}",
        title=tj.get("title") or "",
        url=target_url,
        state=_normalise_gl_state(tj.get("state") or ""),
        is_pull_request=False,
        resolved=True,
    )


def _split_composite_comment_id(
    comment_id: str, ticket_id: str | None,
) -> tuple[str, str]:
    """Resolve a (issue_iid, note_id) pair from the two accepted forms.

    Accepts (ticket #41 addendum B/C):
      - Composite `"<iid>/<note_id>"` in `comment_id` — `ticket_id` is
        ignored (composite wins).
      - Bare note id in `comment_id` + parent iid in `ticket_id` — the
        natural round-trip from `add_comment`'s bare-id response.

    Raises `GitLabError(400)` when neither form provides an iid.
    """
    if "/" in comment_id:
        issue_iid, note_id = comment_id.split("/", 1)
        return issue_iid, note_id
    if ticket_id:
        return ticket_id, comment_id
    raise GitLabError(
        400,
        "GitLab notes are scoped to a parent issue/MR; pass either "
        "comment_id='<issue_iid>/<note_id>' or supply ticket_id "
        "alongside a bare note id",
    )


def _status_to_state_event(status: Status) -> str:
    """Map common status string → GitLab `state_event`.

    GitLab issue/MR updates take `state_event=close|reopen`, not
    `state=closed`. The accepted vocabulary is exactly what
    `list_statuses` returns for GitLab (`["open", "closed"]`) so the
    discovery and write surfaces stay in sync (ticket #49 findings 5
    & 6). GitHub's `closed:completed` / `closed:not_planned` aliases
    are NO LONGER silently coerced — agents that previously passed
    them get a clear rejection pointing back to `list_ticket_statuses`.
    """
    if status == "open":
        return "reopen"
    if status == "closed":
        return "close"
    raise ValueError(
        f"unsupported status {status!r} for GitLab — "
        f"use list_ticket_statuses to discover valid values. "
        f"Accepted: open, closed."
    )


_DRAFT_PREFIX_RE = re.compile(
    r"^\s*(?:Draft:\s*|WIP:\s*|\[Draft\]\s*|\[WIP\]\s*|\(Draft\)\s*)",
    re.IGNORECASE,
)


def _apply_draft_prefix(title: str, draft: bool) -> str:
    """Add/remove GitLab's `Draft: ` title prefix.

    GitLab signals draft state via a title prefix rather than a flag.
    Modern GitLab canonicalises to `Draft: `; legacy values (`WIP: `,
    `[Draft]`, `[WIP]`, `(Draft)`) are stripped on the way out so the
    surface stays clean regardless of historic state.
    """
    stripped = _DRAFT_PREFIX_RE.sub("", title)
    return f"Draft: {stripped}" if draft else stripped


def _resolve_assignee_ids(
    client: httpx.Client, usernames: list[str],
) -> list[int]:
    """Resolve a list of usernames → integer user ids.

    GitLab issue/MR endpoints accept `assignee_ids` (integer list) but
    not usernames. Resolution uses `/users?username=<name>` which
    returns a list — we take the first match. Unknown usernames raise
    `GitLabError(422, ...)` so the agent learns which name was bad
    instead of seeing a silent drop (ticket #49 finding 7 — matches
    GitHub's clear-failure-beats-silent-success principle).
    """
    resolved: list[int] = []
    for name in usernames:
        if not name:
            continue
        r = client.get("/users", params={"username": name})
        _check(r)
        matches = r.json()
        if matches:
            uid = matches[0].get("id")
            if isinstance(uid, int):
                resolved.append(uid)
                continue
        raise GitLabError(
            422,
            f"assignee '{name}' was rejected by GitLab "
            "(user not found or not assignable on this project)",
        )
    return resolved


_MENTION_PATTERN = re.compile(r"(?:(?P<scope>[\w./-]+)?#)(?P<n>\d+)\b")
_CLOSE_PATTERN = re.compile(
    r"(?i)\b(?:closes?|fixes?|resolves?|implements?)\s+"
    r"(?P<ref>(?:[\w./-]+)?#\d+)\b"
)
_DUPLICATE_PATTERN = re.compile(
    r"(?i)\bduplicate\s+of\s+(?P<ref>(?:[\w./-]+)?#\d+)\b"
)


def _mentions_scan_depth() -> int:
    """Mirror the GitHub provider's `PROJECT_ISSUES_MENTIONS_SCAN_DEPTH`
    contract: `-1` = scan every comment, `0` = body only, `N` = first N.
    Default `0` (body only) so we don't fan out reads on big tickets."""
    raw = os.environ.get("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _make_relation(
    kind: str,
    ref: str,
    *,
    title: str = "",
    url: str = "",
    state: str = "",
    is_pull_request: bool = False,
    resolved: bool | None = None,
) -> Relation:
    """Build a `Relation` with the canonical ticket-id format.

    `ref` is either `#N` (same-project) or `group/project#N`. Strip
    leading `#` only when present standalone; otherwise pass through.
    `resolved` follows the ``Relation.resolved`` semantics: ``True`` for
    API-sourced relations, ``False`` for body-scan relations, ``None`` when
    not set.
    """
    return Relation(
        kind=kind,
        ticket_id=ref if ref.startswith("#") else ref,
        title=title,
        url=url,
        state=state,
        is_pull_request=is_pull_request,
        resolved=resolved,
    )


def _scan_refs(text: str, pattern: re.Pattern) -> list[str]:
    """Extract unique ticket references from a piece of text.

    Returns each match in its `[scope]#N` form, deduplicated, in
    source order. Used for outgoing relations (`mentions`, `closes`,
    `duplicate_of`).
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in pattern.finditer(text):
        # Pattern's `ref` group covers the full `[scope]#N` for the
        # close / duplicate scanners; the bare mention scanner exposes
        # `scope` and `n` separately.
        if "ref" in m.groupdict():
            ref = m.group("ref")
        else:
            scope = m.group("scope") or ""
            n = m.group("n")
            ref = f"{scope}#{n}" if scope else f"#{n}"
        if ref not in seen_set:
            seen.append(ref)
            seen_set.add(ref)
    return seen


def _fetch_relations(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
    *,
    ticket_body: str,
    comments: list[Comment],
) -> list[Relation]:
    """Build the relations list for a GitLab issue.

    Combines four sources:

    1. **Issue links** (`/projects/:id/issues/:iid/links`) — GitLab's
       first-class relation surface. Each link carries a `link_type`
       of `relates_to` / `blocks` / `is_blocked_by`; we map those into
       `relates_to` / `blocks` / `blocked_by` on the common
       `RelationKind` literal.

    2. **Closing MRs** (`/projects/:id/issues/:iid/closed_by`) — MRs
       that auto-closed this issue. Surfaced as `closed_by`.

    3. **Outgoing scans on body** — `closes`/`fixes`/`resolves` →
       `closes`; `duplicate of` → `duplicate_of`; plain `#N` references
       → `mentions`. Body-only by default; bump
       `PROJECT_ISSUES_MENTIONS_SCAN_DEPTH` to also scan comments.

    Returns the list in (kind, ticket_id) order for determinism.
    """
    relations: list[Relation] = []
    path = _project_path(project)

    # --- (1) issue links ---
    rl = client.get(f"/projects/{path}/issues/{ticket_id}/links")
    if rl.is_success:
        for link in rl.json():
            link_type = link.get("link_type") or "relates_to"
            kind_map = {
                "relates_to": "relates_to",
                "blocks": "blocks",
                "is_blocked_by": "blocked_by",
            }
            kind = kind_map.get(link_type, "relates_to")
            target_iid = link.get("iid")
            target_project = link.get("references", {}).get(
                "relative", f"#{target_iid}" if target_iid else ""
            )
            relations.append(_make_relation(
                kind=kind,
                ref=target_project,
                title=link.get("title", "") or "",
                # Canonicalise so relations[*].url uses the same
                # /-/issues/N form as ticket / comment URLs
                # (ticket #49 F4 side-finding from test-agent live-verify).
                url=_canonical_url(link.get("web_url", "") or "", project),
                state=_normalise_gl_state(link.get("state", "") or ""),
                resolved=True,
            ))

    # --- (2) closing MRs ---
    rc = client.get(f"/projects/{path}/issues/{ticket_id}/closed_by")
    if rc.is_success:
        for mr in rc.json():
            mr_iid = mr.get("iid")
            if mr_iid is None:
                continue
            relations.append(_make_relation(
                kind="closed_by",
                ref=f"#{mr_iid}",
                title=mr.get("title", "") or "",
                url=_canonical_url(mr.get("web_url", "") or "", project),
                state=_normalise_gl_state(mr.get("state", "") or ""),
                is_pull_request=True,
                resolved=True,
            ))

    # --- (3) outgoing scans ---
    scan_depth = _mentions_scan_depth()
    bodies_to_scan: list[str] = [ticket_body or ""]
    if scan_depth != 0 and comments:
        if scan_depth < 0:
            bodies_to_scan.extend(c.body for c in comments)
        else:
            bodies_to_scan.extend(c.body for c in comments[:scan_depth])
    full_text = "\n".join(bodies_to_scan)

    # Closing keywords → `closes`. Consume these refs first so the
    # plain-mention scanner doesn't double-count them.
    close_refs = _scan_refs(full_text, _CLOSE_PATTERN)
    close_ref_set = set(close_refs)
    for ref in close_refs:
        relations.append(_make_relation(kind="closes", ref=ref, resolved=False))

    # Duplicate-of detection.
    dup_refs = _scan_refs(full_text, _DUPLICATE_PATTERN)
    dup_ref_set = set(dup_refs)
    for ref in dup_refs:
        relations.append(_make_relation(kind="duplicate_of", ref=ref, resolved=False))

    # Plain mentions (filtered against the above two sets and self-ref).
    self_ref = f"#{ticket_id}"
    for ref in _scan_refs(full_text, _MENTION_PATTERN):
        if ref == self_ref or ref in close_ref_set or ref in dup_ref_set:
            continue
        relations.append(_make_relation(kind="mentions", ref=ref, resolved=False))

    # Dedup: the native issue-link written by _gitlab_mark_duplicate_of
    # comes back from the links API as "relates_to", while the body scan
    # above also emits "duplicate_of" for the same target — one target,
    # two relations.  Drop any "relates_to" whose ticket_id already has a
    # "duplicate_of" entry (the body-scan result is authoritative).
    dup_target_ids = {r.ticket_id for r in relations if r.kind == "duplicate_of"}
    if dup_target_ids:
        relations = [
            r for r in relations
            if not (r.kind == "relates_to" and r.ticket_id in dup_target_ids)
        ]

    return relations


def _gitlab_pipeline_scope(status: str) -> str | None:
    """Map our tool-surface `status` vocab to GitLab's pipeline `scope`.

    Tool surface (see `tools/pipelines.py`): `queued | in_progress |
    completed | all`. GitLab's `scope` accepts `running | pending |
    finished | branches | tags`. We map onto the closest equivalent:

      - `queued`       → `pending`
      - `in_progress`  → `running`
      - `completed`    → `finished`
      - `all`          → no filter
      - anything else  → no filter (avoid mis-mapping unknown agent input)

    Ticket #49 finding 1: previously the kwarg wasn't accepted at all,
    causing a TypeError crash.
    """
    mapping = {
        "queued": "pending",
        "in_progress": "running",
        "completed": "finished",
    }
    return mapping.get(status)


def _resolve_gitlab_branch_sha(
    client: httpx.Client,
    project_path: str,
    branch: str,
) -> str | None:
    """Return the commit SHA for `branch`, or ``None`` if not found (404)."""
    r = client.get(f"/projects/{project_path}/repository/branches/{branch}")
    if r.status_code == 404:
        return None
    _check(r)
    return (r.json() or {}).get("commit", {}).get("id")


def _resolve_gitlab_commit(
    client: httpx.Client,
    project_path: str,
    sha: str,
) -> bool:
    """Return ``True`` if `sha` exists as a commit, ``False`` on 404."""
    r = client.get(f"/projects/{project_path}/repository/commits/{sha}")
    if r.status_code == 404:
        return False
    _check(r)
    return True


def _list_pipelines(
    project: ProjectConfig,
    token: str | None,
    extra_params: dict[str, Any],
    limit: int,
) -> list[PipelineRun]:
    """Shared body for list_runs_for_branch/tag/commit.

    GitLab's `/projects/:id/pipelines` endpoint accepts `ref`, `sha`,
    `status`, `source`, etc. Callers pass the addressing param via
    `extra_params`; we add `per_page` and order.
    """
    _validate_limit(limit)
    per_page = min(max(1, limit), 100)
    params: dict[str, Any] = {
        "per_page": per_page,
        "order_by": "id",
        "sort": "desc",
        **extra_params,
    }
    with _client(project, token) as client:
        r = client.get(
            f"/projects/{_project_path(project)}/pipelines",
            params=params,
        )
        _check(r)
        return [_map_pipeline_run(it) for it in r.json()]


# Maximum trace tail size we surface in `FailingJob.log_excerpt`. Trace
# files can be megabytes; the agent only needs the last screenful or
# two to see the actual failure.
_TRACE_TAIL_LIMIT = 4096


def _fetch_pipeline_failure(
    client: httpx.Client,
    project: ProjectConfig,
    pipeline_id: str,
) -> PipelineFailure | None:
    """Build a `PipelineFailure` for a failed pipeline.

    Walks the pipeline's jobs, filters to `status == "failed"`, and
    fetches the trace (last `_TRACE_TAIL_LIMIT` bytes) for each. GitLab
    does not expose GitHub-style structured annotations; the
    `annotations` field is therefore always `[]`.

    Returns `None` if the jobs endpoint is unreachable — preserves
    the "best-effort" contract documented on `PipelineRun.failure`.
    """
    path = _project_path(project)
    r = client.get(
        f"/projects/{path}/pipelines/{pipeline_id}/jobs",
        params={"per_page": 100},
    )
    if not r.is_success:
        return PipelineFailure(failing_jobs=[], note="jobs endpoint unavailable")
    jobs = r.json()
    failing: list[FailingJob] = []
    note: str | None = None
    for job in jobs:
        if job.get("status") != "failed":
            continue
        job_id = job.get("id")
        if job_id is None:
            continue
        trace_excerpt: str | None = None
        tr = client.get(f"/projects/{path}/jobs/{job_id}/trace")
        if tr.is_success:
            text = tr.text
            if len(text) > _TRACE_TAIL_LIMIT:
                trace_excerpt = text[-_TRACE_TAIL_LIMIT:]
            else:
                trace_excerpt = text
        else:
            note = "trace endpoint unavailable"
        failing.append(FailingJob(
            name=job.get("name", "") or "",
            url=job.get("web_url", "") or "",
            failed_step=job.get("stage", "") or "",
            annotations=[],  # GitLab has no structured annotation surface
            log_excerpt=trace_excerpt,
        ))
    return PipelineFailure(failing_jobs=failing, note=note)


# ---------- provider ---------------------------------------------------------


class GitLabProvider(TokenCapabilityProvider, TokenProjectDiscoveryProvider):
    """GitLab REST v4 provider.

    Method bodies are filled in incrementally — see the task list in
    `~/.claude/plans/so-wir-haben-jetzt-snappy-deer.md`. Stubs raise
    `NotImplementedError` so the registry-level dispatch is exercised
    today even though individual operations aren't yet plumbed through.
    """

    # ---------- token capabilities (TokenCapabilityProvider) -----------------

    def probe_token_capabilities(
        self, project: ProjectConfig, token: str
    ) -> TokenCapabilities:
        """Probe a GitLab PAT's scopes via `/personal_access_tokens/self`.

        GitLab tokens don't split issues vs PR scopes the way GitHub's
        fine-grained PATs do. Coarsest mapping:
          - `api` scope (full read+write)              → all five flags True
          - `read_api` / read-only / unknown scopes    → all False (token
            still passes through for read, gated implicitly), reason set
          - 401                                        → "bad_credentials"
          - 404 on self-endpoint                       → "bad_credentials"
            (treat as invalid token rather than 404 from project, since
            `/self` succeeds on any valid token)
          - transport failure                          → "network_error"
          - response missing `scopes`                  → "permissions_field_missing"

        On any failure mode, all flags are False and `reason` is set so
        the caller can degrade gracefully (no operation granted on a
        failed probe).
        """
        try:
            with _client(project, token) as client:
                r = client.get("/personal_access_tokens/self")
        except httpx.HTTPError:
            return TokenCapabilities(reason="network_error")
        if r.status_code == 401:
            return TokenCapabilities(reason="bad_credentials")
        if r.status_code == 404:
            return TokenCapabilities(reason="bad_credentials")
        if not r.is_success:
            return TokenCapabilities(reason="network_error")
        try:
            payload = r.json()
        except Exception:
            return TokenCapabilities(reason="permissions_field_missing")
        scopes = payload.get("scopes")
        if not isinstance(scopes, list):
            return TokenCapabilities(reason="permissions_field_missing")
        if "api" in scopes:
            return TokenCapabilities(
                issues_create=True, issues_modify=True,
                pulls_create=True, pulls_modify=True, pulls_merge=True,
                reason=None,
            )
        return TokenCapabilities(reason="insufficient_scope")

    # ---------- project discovery (TokenProjectDiscoveryProvider) -------------

    def discover_projects(
        self, token: str, *, limit: int
    ) -> ProjectDiscoveryResult:
        """Enumerate GitLab projects the token is a member of.

        Calls ``GET /api/v4/projects?membership=true&per_page=100`` and
        paginates via the ``X-Next-Page`` response header until *limit*
        projects have been collected or all pages are exhausted.

        Never raises on expected failure modes (401, non-2xx, network
        error); returns an empty ``ProjectDiscoveryResult`` with ``reason``
        set instead.
        """
        _validate_limit(limit)
        collected: list[DiscoveredProject] = []
        params: dict = {"membership": "true", "per_page": 100}
        truncated = False

        try:
            with _discovery_client(DEFAULT_BASE_URL, token) as client:
                while True:
                    r = client.get("/api/v4/projects", params=params)

                    if r.status_code == 401:
                        return ProjectDiscoveryResult(
                            projects=[], reason="bad_credentials"
                        )
                    if not r.is_success:
                        return ProjectDiscoveryResult(
                            projects=[], reason=f"http_{r.status_code}"
                        )

                    page_items = r.json()
                    budget = limit - len(collected)
                    for item in page_items[:budget]:
                        collected.append(
                            DiscoveredProject(
                                provider="gitlab",
                                path=item["path_with_namespace"],
                                description=item.get("description") or "",
                                permissions=_capabilities_from_access_level(
                                    _extract_access_level(
                                        item.get("permissions") or {}
                                    )
                                ),
                            )
                        )

                    next_page = (r.headers.get("X-Next-Page") or "").strip()
                    if len(collected) >= limit:
                        truncated = bool(next_page)
                        break
                    if not next_page:
                        break
                    params = {"membership": "true", "per_page": 100, "page": next_page}
        except httpx.HTTPError:
            return ProjectDiscoveryResult(projects=[], reason="network_error")

        return ProjectDiscoveryResult(
            projects=collected,
            truncated=truncated,
            reason=None,
        )

    # ---------- issues -------------------------------------------------------

    def list_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> tuple[list[Ticket], bool]:
        """List issues in a project.

        Filter mapping (GitLab REST `/projects/:id/issues`):
          - `status`: `open`→`opened`, `closed`→`closed`, `any`→`all`.
          - `labels`: comma-joined `labels=` param. `not_labels` → `not[labels]`.
          - `assignee` → `assignee_username`. `author` → `author_username`.
          - `created_after/before` / `updated_after/before` pass through.
          - `search` passes through as the GitLab `search` param.
          - `sort_by`: `created`→`created_at`, `updated`→`updated_at`,
            `comments`→`user_notes_count`. `sort_order` → `sort`.
          - `limit` is `per_page`, capped at 100. Single page only.
        """
        _validate_limit(filters.limit)
        per_page = min(max(1, filters.limit), 100)
        sort_by_map = {
            "created": "created_at",
            "updated": "updated_at",
            "comments": "user_notes_count",
        }
        state_map = {"open": "opened", "closed": "closed", "any": "all"}
        params: dict[str, Any] = {
            "per_page": per_page,
            "state": state_map.get(filters.status, "opened"),
            "order_by": sort_by_map.get(filters.sort_by, "created_at"),
            "sort": filters.sort_order,
        }
        if filters.labels:
            params["labels"] = ",".join(filters.labels)
        if filters.not_labels:
            # GitLab's REST array syntax: `not[labels][]=foo` repeated, or
            # in a single comma-joined string. The single-string form is
            # supported on `/issues` since 12.x.
            params["not[labels]"] = ",".join(filters.not_labels)
        if filters.assignee:
            params["assignee_username"] = filters.assignee
        if filters.author:
            params["author_username"] = filters.author
        if filters.search:
            params["search"] = filters.search
        if filters.created_after:
            params["created_after"] = filters.created_after
        if filters.created_before:
            params["created_before"] = filters.created_before
        if filters.updated_after:
            params["updated_after"] = filters.updated_after
        if filters.updated_before:
            params["updated_before"] = filters.updated_before
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{_project_path(project)}/issues", params=params,
            )
            _check(r)
            items = r.json()
            has_more = len(items) >= per_page
            return [_map_issue(it, project) for it in items], has_more

    def get_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        *,
        include_relations: bool = True,
        include_custom_fields: bool = False,
    ) -> tuple[Ticket, list[Comment], list[Relation] | None, bool | None]:
        """Fetch a single issue plus its non-system notes.

        Returns `(ticket, comments, relations, relations_truncated)`.
        When `include_relations` is False, skips the extra relation API
        calls and returns `([], None)` for the relation fields.
        `truncated=None` signals "skipped"; `truncated=False` signals
        "fetched but empty".  `relations` is always a list (never `None`).

        System notes (state changes, label edits) are filtered out —
        they're not user-facing comments.

        `include_custom_fields` is accepted for cross-provider signature
        parity with Azure DevOps but is a no-op here: GitLab has no
        provider-native raw-field map, so `ticket.custom_fields` stays
        `None` regardless of this flag. Cross-provider support is
        deferred to ticket #123.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(f"/projects/{path}/issues/{ticket_id}")
            _check(r)
            ticket = _map_issue(r.json(), project)
            c = client.get(
                f"/projects/{path}/issues/{ticket_id}/notes",
                params={"per_page": 100, "sort": "asc", "order_by": "created_at"},
            )
            _check(c)
            comments = [
                _map_note(it, project) for it in c.json()
                if not it.get("system", False)
            ]
            if include_relations:
                relations: list[Relation] | None = _fetch_relations(
                    client, project, ticket_id, ticket_body=ticket.body,
                    comments=comments,
                )
                truncated: bool | None = False
            else:
                relations = []
                truncated = None
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
    ) -> Ticket:
        """Create a GitLab issue with the AI-generated marker.

        Marker policy mirrors `GitHubProvider.create_ticket`:
          - The `#ai-generated` body prefix is the canonical attribution
            and is always applied (idempotent).
          - The `ai-generated` LABEL is also applied. Unlike GitHub,
            GitLab allows any project member to apply labels by name
            (no pre-create step required) — but if the label doesn't
            exist yet, GitLab silently creates it.

        Assignees are passed as usernames; GitLab requires user IDs on
        the POST. We resolve usernames → IDs via `/users?username=` so
        the caller doesn't have to.

        Optional `status` (ticket #42) accepts the same vocabulary as
        `update_ticket.status`. The GitLab `POST /issues` endpoint
        creates in `opened` state; non-`open` requests are landed via
        a follow-up PUT with `state_event=close`. Validation is
        performed up-front (`_status_to_state_event`) so an invalid
        value rejects before the POST.

        `custom_fields` is accepted for cross-provider signature parity
        with Azure DevOps, but GitLab has no provider-native raw-field
        write path yet: a non-empty dict raises `ValueError` (cross-provider
        support is deferred to ticket #123). `None`/`{}` is a silent no-op
        so existing callers are unaffected.
        """
        if not title or not title.strip():
            raise ValueError("title must not be blank")
        if custom_fields:
            raise ValueError(
                "custom_fields is not supported on GitLab yet — "
                "cross-provider support is deferred to ticket #123"
            )
        # Validate `status` up-front. Pass None through; raise on
        # unknown values before POST commits an issue.
        state_event: str | None = None
        if status is not None:
            state_event = _status_to_state_event(status)
        merged_labels = list(dict.fromkeys([*labels, AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            assignee_ids = _resolve_assignee_ids(client, assignees)
            payload: dict[str, Any] = {
                "title": title,
                "description": prefixed_body,
            }
            if merged_labels:
                payload["labels"] = ",".join(merged_labels)
            if assignee_ids:
                payload["assignee_ids"] = assignee_ids
            r = client.post(f"/projects/{path}/issues", json=payload)
            _check(r)
            raw = r.json()
            # Follow-up PUT for non-`open` initial status (state_event
            # is only `close` here — `reopen` is a no-op on a freshly
            # created issue).
            if state_event == "close":
                iid = raw.get("iid")
                pu = client.put(
                    f"/projects/{path}/issues/{iid}",
                    json={"state_event": "close"},
                )
                _check(pu)
                raw = pu.json()
            return _map_issue(raw, project)

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
        """Update an issue.

        Status mapping:
          - `"open"` (or `"reopen"` legacy) → `state_event=reopen`.
          - `"closed"` (or `closed:completed` / `closed:not_planned`) →
            `state_event=close`. GitLab has no `state_reason`; the
            distinction is lost server-side. Agents wanting the
            "not planned" semantics apply the `ai-closed-not-planned`
            label via `labels_add` (see `markers.py`).

        Label add/remove use GitLab's dedicated `add_labels` /
        `remove_labels` params — no fetch+diff needed.

        Assignees: GitLab only accepts a final `assignee_ids` list, so
        we fetch current assignees, apply the delta, resolve usernames
        to ids, and send the result.

        `ai-modified` is added when the issue wasn't tagged
        `ai-generated` originally — same heuristic as the GitHub
        provider but implemented with the GitLab params.
        """
        _validate_label_lists(labels_add, labels_remove)
        path = _project_path(project)
        with _client(project, token) as client:
            # Always fetch current — needed for the ai-modified marker
            # decision, and for assignee delta resolution.
            r0 = client.get(f"/projects/{path}/issues/{ticket_id}")
            try:
                _check(r0)
            except GitLabError as exc:
                if exc.status == 404:
                    raise GitLabError(
                        404, f"ticket '{project.id}#{ticket_id}' not found"
                    ) from exc
                raise
            current = r0.json()
            current_labels = set(current.get("labels") or [])

            will_be_ai_generated = AI_GENERATED_LABEL in current_labels

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                # Ticket #44: re-stamp body marker to match label state.
                payload["description"] = apply_body_marker(
                    body, will_be_ai_generated=will_be_ai_generated,
                )
            if status is not None:
                payload["state_event"] = _status_to_state_event(status)

            add_set = set(labels_add or [])
            remove_set = set(labels_remove or [])
            if (
                not will_be_ai_generated
                and AI_MODIFIED_LABEL not in current_labels
            ):
                add_set.add(AI_MODIFIED_LABEL)
            if add_set:
                payload["add_labels"] = ",".join(sorted(add_set))
            if remove_set:
                payload["remove_labels"] = ",".join(sorted(remove_set))

            if assignees_add or assignees_remove:
                current_assignees = {
                    a.get("username", "")
                    for a in (current.get("assignees") or [])
                }
                final_usernames = set(current_assignees)
                if assignees_add:
                    final_usernames.update(assignees_add)
                if assignees_remove:
                    final_usernames.difference_update(assignees_remove)
                # GitLab accepts an empty list to mean "unassigned"; pass
                # it through so explicit removal works.
                payload["assignee_ids"] = _resolve_assignee_ids(
                    client, sorted(final_usernames),
                )

            if not payload:
                return _map_issue(current, project)
            r = client.put(
                f"/projects/{path}/issues/{ticket_id}", json=payload,
            )
            _check(r)
            return _map_issue(r.json(), project)

    def list_statuses(
        self,
        project: ProjectConfig,  # noqa: ARG002 — kept for provider-agnostic signature
        token: str | None,         # noqa: ARG002 — same
    ) -> StatusSpec:
        """Return the GitLab-static status spec.

        GitLab issues have a fixed state-space (`opened` ↔ `closed`).
        Unlike GitHub, GitLab has no `state_reason` field, so the
        distinction between "completed" and "not planned" is collapsed:
        both terminal hints point at the same `"closed"` value. Callers
        that need the distinction apply the `ai-closed-not-planned`
        label (see `markers.py`).
        """
        return StatusSpec(
            values=["open", "closed"],
            transitions={
                "open": ["closed"],
                "closed": ["open"],
            },
            hints={
                "default_open": "open",
                "terminal": ["closed"],
                "terminal_completed": "closed",
                "terminal_declined": "closed",
            },
        )

    def list_fields(
        self,
        project: ProjectConfig,  # noqa: ARG002 — kept for provider-agnostic signature
        token: str | None,         # noqa: ARG002 — same
        *,
        work_item_type: str | None = None,  # noqa: ARG002 — same
    ) -> list[FieldSpec]:
        """Return an empty list — GitLab issues have no structured field schema.

        GitLab does not expose a discoverable field vocabulary for issues.
        This stub satisfies the provider-agnostic surface so callers can
        iterate over all providers without special-casing GitLab.
        """
        return []

    # ---------- comments / notes --------------------------------------------

    def add_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        body: str,
    ) -> Comment:
        """Post a note on an issue. The AI-comment prefix is applied."""
        if not body or not body.strip():
            raise ValueError("body must not be empty")
        prefixed = ensure_comment_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.post(
                f"/projects/{path}/issues/{ticket_id}/notes",
                json={"body": prefixed},
            )
            try:
                _check(r)
            except GitLabError as exc:
                if exc.status == 404:
                    raise GitLabError(
                        404, f"ticket '{project.id}#{ticket_id}' not found"
                    ) from exc
                raise
            return _map_note(r.json(), project)

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
        """List user notes (non-system) on an issue.

        System notes (state changes, label edits, milestone moves) are
        filtered out — they aren't user-facing comments.

        Returns `(rows, has_more)`. `since` maps to GitLab's
        `created_after` query parameter (ISO-8601). `page` is 1-based.

        Tail-fetch (ticket #47 follow-up): when `order="desc"`,
        `page=1`, and no `since`, the implementation probes the
        `X-Total-Pages` header and fetches from the last page
        backwards until `limit` items are collected, returning
        newest-first. Without this special case the provider would
        just reverse page 1, which is the OLDEST N in reverse order
        rather than the newest.

        Filtering system notes happens client-side AFTER the API
        truncated to `per_page`, so the returned list can occasionally
        be shorter than `limit` even when more user notes exist on
        later pages — callers that need exactly `limit` user notes
        should walk `has_more`.
        """
        per_page = min(max(1, limit), 100)
        path = _project_path(project)
        with _client(project, token) as client:
            if order == "desc" and page == 1 and not since:
                return self._list_comments_tail(
                    client, project, path, ticket_id,
                    per_page=per_page, limit=limit,
                )
            params: dict[str, Any] = {
                "per_page": per_page,
                "page": page,
                "sort": "asc",
                "order_by": "created_at",
            }
            if since:
                params["created_after"] = since
            r = client.get(
                f"/projects/{path}/issues/{ticket_id}/notes",
                params=params,
            )
            _check(r)
            rows = [
                _map_note(it, project) for it in r.json()
                if not it.get("system", False)
            ]
            # GitLab's `created_after` server hint is not reliably
            # honoured for notes — apply the filter client-side.
            if since:
                rows = [c for c in rows if c.created_at and c.created_at >= since]
            next_page = (r.headers.get("X-Next-Page") or "").strip()
            has_more = bool(next_page)
            return rows, has_more

    def _list_comments_tail(
        self,
        client: httpx.Client,
        project: ProjectConfig,
        path: str,
        ticket_id: str,
        *,
        per_page: int,
        limit: int,
    ) -> tuple[list[Comment], bool]:
        """Smart-fetch the last `limit` user notes newest-first.

        Same shape as `GitHubProvider._list_comments_tail` — probe
        page 1 for the total page count (`X-Total-Pages`), walk
        backwards from the last page collecting items until at least
        `limit` are gathered (or pages run out), reverse + slice.
        """
        url = f"/projects/{path}/issues/{ticket_id}/notes"
        base_params: dict[str, Any] = {
            "per_page": per_page,
            "sort": "asc",
            "order_by": "created_at",
        }
        probe = client.get(url, params={**base_params, "page": 1})
        _check(probe)
        total_pages_header = (probe.headers.get("X-Total-Pages") or "").strip()
        try:
            last_page = int(total_pages_header) if total_pages_header else 1
        except ValueError:
            last_page = 1
        if last_page <= 1:
            rows = [
                _map_note(it, project) for it in probe.json()
                if not it.get("system", False)
            ]
            rows.reverse()
            return rows, False

        collected_oldest_first: list[Comment] = []
        cur = last_page
        while cur >= 1 and len(collected_oldest_first) < limit:
            r = client.get(url, params={**base_params, "page": cur})
            _check(r)
            page_rows = [
                _map_note(it, project) for it in r.json()
                if not it.get("system", False)
            ]
            collected_oldest_first = page_rows + collected_oldest_first
            cur -= 1

        tail = collected_oldest_first[-limit:]
        tail.reverse()
        has_more = cur >= 1
        return tail, has_more

    def get_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        ticket_id: str | None = None,
    ) -> Comment:
        """Fetch a single note by id.

        GitLab notes are scoped to their parent issue/MR — unlike GitHub
        comment ids which are repo-wide. The note id alone is not enough
        to address a note; we also need the parent issue's iid.

        Two input forms are accepted (ticket #41 addendum B/C):
          - Composite key in `comment_id`: `"<iid>/<note_id>"`. Used by
            agents that previously stitched the IDs themselves.
          - Bare note id in `comment_id` + parent iid in `ticket_id`.
            This is the natural round-trip after `add_comment` (which
            returns a bare note id) and gives `ticket_id` consistent
            semantics across GitHub and GitLab.

        At least one of the two forms must supply the parent iid; a
        bare note id with no `ticket_id` raises `GitLabError(400)`.

        Library-level composite-ID support is intentional and identical to
        `update_comment` — both route through `_split_composite_comment_id`
        (ticket #50). Tests: `test_get_comment_composite_key` and
        `test_get_comment_bare_id_with_ticket_id_works` in
        `tests/test_gitlab_issues.py`.
        """
        path = _project_path(project)
        issue_iid, note_id = _split_composite_comment_id(
            comment_id, ticket_id,
        )
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{path}/issues/{issue_iid}/notes/{note_id}",
            )
            _check(r)
            return _map_note(r.json(), project)

    def update_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        body: str,
        ticket_id: str | None = None,
    ) -> Comment:
        """Edit a note, re-stamping the AI-marker.

        Marker policy (ticket #44): same as `GitHubProvider.update_comment`
        — if the existing note carries `#ai-generated`, the edit
        preserves that marker; otherwise it stamps `#ai-modified`.

        Accepts the same two comment-id forms as `get_comment` (ticket
        #41 addendum B/C): composite `"<iid>/<note_id>"` in `comment_id`,
        or bare note id in `comment_id` plus parent iid in `ticket_id`.
        """
        if not body or not body.strip():
            raise ValueError("body must not be empty")
        path = _project_path(project)
        issue_iid, note_id = _split_composite_comment_id(
            comment_id, ticket_id,
        )
        with _client(project, token) as client:
            r0 = client.get(
                f"/projects/{path}/issues/{issue_iid}/notes/{note_id}",
            )
            try:
                _check(r0)
            except GitLabError as exc:
                if exc.status == 404:
                    raise GitLabError(
                        404, f"comment '{project.id}#{comment_id}' not found"
                    ) from exc
                raise
            current_body = r0.json().get("body") or ""
            will_be_ai_generated = has_ai_generated_marker(current_body)
            prefixed = apply_body_marker(
                body, will_be_ai_generated=will_be_ai_generated,
            )
            r = client.put(
                f"/projects/{path}/issues/{issue_iid}/notes/{note_id}",
                json={"body": prefixed},
            )
            _check(r)
            return _map_note(r.json(), project)

    def delete_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        ticket_id: str | None = None,
    ) -> None:
        """Delete a note by its id.

        Accepts the same two comment-id forms as `get_comment` (ticket
        #41 addendum B/C): composite `"<iid>/<note_id>"` in `comment_id`,
        or bare note id in `comment_id` plus parent iid in `ticket_id`.

        Raises `GitLabError(404, ...)` when the note does not exist.
        Returns `None` on success (GitLab responds with 204 No Content).
        """
        path = _project_path(project)
        issue_iid, note_id = _split_composite_comment_id(
            comment_id, ticket_id,
        )
        with _client(project, token) as client:
            r = client.delete(
                f"/projects/{path}/issues/{issue_iid}/notes/{note_id}",
            )
            if r.status_code == 404:
                raise GitLabError(
                    404, f"comment '{project.id}#{comment_id}' not found"
                )
            _check(r)

    # ---------- merge requests (PR surface) ----------------------------------

    def list_prs(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: PRFilters,
    ) -> tuple[list[PullRequest], bool]:
        """List merge requests for a project.

        Filter mapping (GitLab REST `/projects/:id/merge_requests`):
          - `status`: `open`→`opened`, `closed`→`closed`, `any`→`all`.
            Note: GitLab can't filter MRs by `merged` via `state`;
            agents wanting only merged MRs filter post-fetch on
            `status == "merged"`.
          - `labels` → comma-joined `labels` param.
          - `assignee` → `assignee_username`.
          - `head` → `source_branch`. `base` → `target_branch`.
          - `search` → `search` (matches title + description).
          - `limit` → `per_page`, capped at 100.

        Returns `(prs, has_more)`. `has_more` is True when the API returned
        exactly `per_page` results, indicating more pages may exist.
        """
        per_page = min(max(1, filters.limit), 100)
        state_map = {"open": "opened", "closed": "closed", "any": "all"}
        params: dict[str, Any] = {
            "per_page": per_page,
            "state": state_map.get(filters.status, "opened"),
            "order_by": "created_at",
            "sort": "desc",
        }
        if filters.labels:
            params["labels"] = ",".join(filters.labels)
        if filters.assignee:
            params["assignee_username"] = filters.assignee
        if filters.head:
            params["source_branch"] = filters.head
        if filters.base:
            params["target_branch"] = filters.base
        if filters.search:
            params["search"] = filters.search
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{_project_path(project)}/merge_requests",
                params=params,
            )
            _check(r)
            items = r.json()
            has_more = len(items) >= per_page
            # Note: base.sha may be None for freshly-created MRs because
            # diff_refs is absent until GitLab runs a pipeline/diff computation.
            # See _map_mr docstring (the "diff_refs / base.sha" note) for details.
            return [_map_mr(it, project) for it in items], has_more

    def get_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> tuple[PullRequest, list[Comment]]:
        """Fetch a single MR plus its non-system notes.

        Performs three round-trips against GitLab:

          1. `GET /projects/:id/merge_requests/:iid` — the MR root.
          2. `GET /projects/:id/merge_requests/:iid/approvals` — the
             approval state (Premium / self-hosted editions). GitLab's
             MR root does not include approval data, so this is a
             dedicated endpoint. Used to derive `review_decision` and
             `approvals_received`. If the endpoint returns 403
             (restricted scope) or 404 (self-hosted edition without
             the approvals API), we fall back to the historical
             behavior: `_map_mr` is called without the approvals
             payload and `review_decision`/`approvals_received` stay
             `None` rather than raising.
          3. `GET /projects/:id/merge_requests/:iid/notes` — the
             discussion notes.

        `list_prs` deliberately skips step (2) to avoid an N+1 round
        trip across the listing. Callers that need accurate approval
        state on a single MR should always use `get_pr`.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(f"/projects/{path}/merge_requests/{pr_id}")
            _check(r)
            raw_mr = r.json()
            ar = client.get(
                f"/projects/{path}/merge_requests/{pr_id}/approvals"
            )
            if ar.status_code in (403, 404):
                # No approvals data accessible on this edition / with
                # this token's scope — degrade gracefully.
                pr = _map_mr(raw_mr, project)
            else:
                _check(ar)
                pr = _map_mr(raw_mr, project, approvals=ar.json())
            c = client.get(
                f"/projects/{path}/merge_requests/{pr_id}/notes",
                params={"per_page": 100, "sort": "asc", "order_by": "created_at"},
            )
            _check(c)
            # Positional (diff-anchored) notes are surfaced via
            # `list_pr_review_comments` as `ReviewComment`. Keep this
            # list to true discussion-only notes — mirrors GitHub where
            # the issue-comments and review-comments endpoints are
            # physically separate.
            comments = [
                _map_note(it, project) for it in c.json()
                if not it.get("system", False) and not it.get("position")
            ]
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
        """Create a merge request with the AI-generated marker.

        Body prefix + `ai-generated` label applied. `draft` translates
        to the GitLab `draft` param (supported 14.x+) AND mirrored as
        a `Draft: ` title prefix. The `draft` param is silently ignored
        on some GitLab setups (observed during ticket #43 live-verify);
        the title prefix is the canonical signal GitLab itself uses
        for `detailed_merge_status="draft_status"`, so it always sticks.
        """
        merged_labels = list(dict.fromkeys([*(labels or []), AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            assignee_ids = _resolve_assignee_ids(client, assignees or [])
            reviewer_ids = _resolve_assignee_ids(
                client, requested_reviewers or [],
            )
            payload: dict[str, Any] = {
                "title": title,
                "description": prefixed_body,
                "source_branch": head,
                "target_branch": base,
            }
            if draft:
                payload["draft"] = True
                payload["title"] = _apply_draft_prefix(
                    payload["title"], draft=True,
                )
            if merged_labels:
                payload["labels"] = ",".join(merged_labels)
            if assignee_ids:
                payload["assignee_ids"] = assignee_ids
            if reviewer_ids:
                payload["reviewer_ids"] = reviewer_ids
            r = client.post(f"/projects/{path}/merge_requests", json=payload)
            _check(r)
            return _map_mr(r.json(), project)

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
        """Update an MR's metadata, status, base branch, labels, assignees, reviewers.

        `status` accepts only `"open"` / `"closed"`. Use `merge_pr` to
        merge — `status="merged"` is rejected. Reopening a merged MR is
        not possible in GitLab; the API rejects the call.

        `draft` toggles draft state via title-prefix manipulation, which
        is how GitLab models drafts. Any combination of explicit `title`
        change + `draft` flip is supported: the prefix is applied to
        whichever title ends up being sent.

        `ai-modified` is added when the MR wasn't tagged `ai-generated`
        — mirrors `update_ticket`.
        """
        path = _project_path(project)
        if status not in (None, "open", "closed"):
            raise ValueError(
                f"unsupported PR status {status!r} — use merge_pr() to "
                f"merge; accepted: open, closed"
            )
        _validate_label_lists(labels_add, labels_remove)
        with _client(project, token) as client:
            r0 = client.get(f"/projects/{path}/merge_requests/{pr_id}")
            _check(r0)
            current = r0.json()
            current_labels = set(current.get("labels") or [])

            will_be_ai_generated = AI_GENERATED_LABEL in current_labels

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if draft is not None:
                base_title = payload.get("title", current.get("title", ""))
                payload["title"] = _apply_draft_prefix(base_title, draft)
            if body is not None:
                # Ticket #44: re-stamp body marker to match label state.
                payload["description"] = apply_body_marker(
                    body, will_be_ai_generated=will_be_ai_generated,
                )
            if status == "open":
                payload["state_event"] = "reopen"
            elif status == "closed":
                payload["state_event"] = "close"
            if base is not None:
                payload["target_branch"] = base

            add_set = set(labels_add or [])
            remove_set = set(labels_remove or [])
            if (
                not will_be_ai_generated
                and AI_MODIFIED_LABEL not in current_labels
            ):
                add_set.add(AI_MODIFIED_LABEL)
            if add_set:
                payload["add_labels"] = ",".join(sorted(add_set))
            if remove_set:
                payload["remove_labels"] = ",".join(sorted(remove_set))

            if assignees_add or assignees_remove:
                current_assignees = {
                    a.get("username", "")
                    for a in (current.get("assignees") or [])
                }
                final_usernames = set(current_assignees)
                if assignees_add:
                    final_usernames.update(assignees_add)
                if assignees_remove:
                    final_usernames.difference_update(assignees_remove)
                payload["assignee_ids"] = _resolve_assignee_ids(
                    client, sorted(final_usernames),
                )

            if reviewers_add or reviewers_remove:
                current_reviewers = {
                    r.get("username", "")
                    for r in (current.get("reviewers") or [])
                }
                final_reviewer_names = set(current_reviewers)
                if reviewers_add:
                    final_reviewer_names.update(reviewers_add)
                if reviewers_remove:
                    final_reviewer_names.difference_update(reviewers_remove)
                payload["reviewer_ids"] = _resolve_assignee_ids(
                    client, sorted(final_reviewer_names),
                )

            if not payload:
                return _map_mr(current, project)
            r = client.put(
                f"/projects/{path}/merge_requests/{pr_id}", json=payload,
            )
            _check(r)
            return _map_mr(r.json(), project)

    def add_pr_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        body: str,
    ) -> Comment:
        """Post a note on a merge request. AI-comment prefix applied."""
        prefixed = ensure_comment_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.post(
                f"/projects/{path}/merge_requests/{pr_id}/notes",
                json={"body": prefixed},
            )
            _check(r)
            return _map_note(r.json(), project, mr_iid=pr_id)

    def list_pr_review_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> list[ReviewComment]:
        """List inline diff-anchored notes on an MR.

        Fetches `GET /projects/:id/merge_requests/:iid/discussions` and
        flattens it to one `ReviewComment` per note. Diff notes have a
        `position` object; non-positional notes (the regular discussion
        thread) are skipped so this surface stays focused on inline
        code-review comments.

        Threading: the first note in a discussion has `in_reply_to=None`;
        replies share the same `discussion.id` and carry it as their
        `in_reply_to` value. This mirrors the GitHub model where replies
        carry the parent comment id.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{path}/merge_requests/{pr_id}/discussions",
                params={"per_page": 100},
            )
            _check(r)
            out: list[ReviewComment] = []
            for disc in r.json():
                notes = disc.get("notes") or []
                if not notes:
                    continue
                # Only surface diff-anchored discussions — the first
                # note's position tells us whether this thread lives on
                # the diff or is a plain MR conversation.
                first_position = notes[0].get("position")
                if not first_position:
                    continue
                discussion_id = str(disc.get("id", ""))
                for idx, note in enumerate(notes):
                    pos = note.get("position") or first_position or {}
                    if pos.get("new_line") is not None:
                        side = "RIGHT"
                    elif pos.get("old_line") is not None:
                        side = "LEFT"
                    else:
                        side = None
                    out.append(ReviewComment(
                        id=str(note.get("id", "")),
                        author=(note.get("author") or {}).get("username", ""),
                        body=note.get("body") or "",
                        path=pos.get("new_path") or pos.get("old_path"),
                        line=pos.get("new_line"),
                        original_line=pos.get("old_line"),
                        side=side,
                        commit_sha=pos.get("head_sha")
                        or pos.get("base_sha")
                        or None,
                        in_reply_to=None if idx == 0 else discussion_id,
                        created_at=normalize_timestamp(note.get("created_at") or ""),
                        updated_at=normalize_timestamp(note.get("updated_at") or ""),
                        url=note.get("web_url") or None,
                        discussion_id=discussion_id,
                    ))
            return out

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
        """Add an inline review comment.

        Routing:
          - **Reply** (`in_reply_to=<discussion_id>`): POST
            `.../discussions/{discussion_id}/notes` with the body only.
          - **New thread**: POST `.../discussions` with a `position`
            object carrying `base_sha` (read from the MR), `start_sha`
            (same), `head_sha` (= `commit_sha`), `new_path`/`old_path`
            (= `path`), `new_line` (= `line`), and `position_type=text`.

        New-thread mode requires `path`, `line`, and `commit_sha` to be
        set; the caller (tool layer) validates that.
        """
        prefixed = ensure_comment_prefix(body)
        repo_path = _project_path(project)
        with _client(project, token) as client:
            if in_reply_to is not None:
                r = client.post(
                    f"/projects/{repo_path}/merge_requests/{pr_id}"
                    f"/discussions/{in_reply_to}/notes",
                    json={"body": prefixed},
                )
                _check(r)
                note_raw = r.json()
                note_id = note_raw.get("id")
                if (
                    project is not None
                    and project.web_url
                    and note_id is not None
                ):
                    _reply_url = _canonical_url(
                        f"{project.web_url}/-/merge_requests/{pr_id}"
                        f"#note_{note_id}",
                        project,
                    )
                else:
                    _reply_url = note_raw.get("web_url") or ""
                return ReviewComment(
                    id=str(note_raw.get("id", "")),
                    author=(note_raw.get("author") or {}).get("username", ""),
                    body=note_raw.get("body") or "",
                    path=None,
                    line=None,
                    commit_sha=None,
                    in_reply_to=in_reply_to,
                    created_at=normalize_timestamp(note_raw.get("created_at") or ""),
                    updated_at=normalize_timestamp(note_raw.get("updated_at") or ""),
                    url=_reply_url or None,
                    # Thread anchor is the discussion the reply joined.
                    discussion_id=in_reply_to,
                )

            # New thread — GitLab needs base_sha and start_sha alongside
            # head_sha; fetch them from the MR's diff_refs.
            mr_r = client.get(
                f"/projects/{repo_path}/merge_requests/{pr_id}",
            )
            _check(mr_r)
            diff_refs = mr_r.json().get("diff_refs") or {}
            base_sha = diff_refs.get("base_sha") or commit_sha
            start_sha = diff_refs.get("start_sha") or commit_sha
            position = {
                "base_sha": base_sha,
                "start_sha": start_sha,
                "head_sha": commit_sha,
                "position_type": "text",
                "new_path": path,
                "old_path": path,
                "new_line": line,
            }
            r = client.post(
                f"/projects/{repo_path}/merge_requests/{pr_id}/discussions",
                json={"body": prefixed, "position": position},
            )
            _check(r)
            disc_raw = r.json()
            note_raw = (disc_raw.get("notes") or [{}])[0]
            # disc_raw["id"] is the discussion anchor — surface it so
            # callers can reply via `in_reply_to=<discussion_id>` without
            # a second GET. Without this the discussion id is unreachable
            # on a freshly-created thread (live-verify bug from #43).
            discussion_id = str(disc_raw.get("id", ""))
            note_id = note_raw.get("id")
            if (
                project is not None
                and project.web_url
                and note_id is not None
            ):
                _new_thread_url = _canonical_url(
                    f"{project.web_url}/-/merge_requests/{pr_id}"
                    f"#note_{note_id}",
                    project,
                )
            else:
                _new_thread_url = note_raw.get("web_url") or ""
            return ReviewComment(
                id=str(note_raw.get("id", "")),
                author=(note_raw.get("author") or {}).get("username", ""),
                body=note_raw.get("body") or "",
                path=path,
                line=line,
                side=None,
                commit_sha=commit_sha or None,
                in_reply_to=None,
                created_at=normalize_timestamp(note_raw.get("created_at") or ""),
                updated_at=normalize_timestamp(note_raw.get("updated_at") or ""),
                url=_new_thread_url or None,
                discussion_id=discussion_id or None,
            )

    def submit_pr_review(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        state: str,
        body: str | None = None,
        commit_sha: str | None = None,
    ) -> Review:
        """Submit an MR review.

        GitLab models review state via separate endpoints rather than
        an enum, so we translate `state` into the matching call:

          - `"approve"`         → POST `.../approve` (no body required).
            If `body` is provided, it is posted as a note as well so
            the rationale survives.
          - `"comment"`         → POST `.../notes` (body required).
          - `"request_changes"` → POST `.../unapprove` (best-effort —
            ignored if the user wasn't approved) followed by
            POST `.../notes`. Body is required so the request is
            actionable.

        `commit_sha` is accepted for surface symmetry with GitHub but
        not used; GitLab MR reviews aren't pinned to a commit.
        """
        if state not in ("approve", "request_changes", "comment"):
            raise ValueError(
                f"unsupported review state {state!r} — accepted: "
                f"['approve', 'comment', 'request_changes']"
            )
        if state in ("comment", "request_changes") and not body:
            raise ValueError(
                f"a review body is required when state={state!r}"
            )
        path = _project_path(project)
        with _client(project, token) as client:
            if state == "approve":
                r = client.post(
                    f"/projects/{path}/merge_requests/{pr_id}/approve",
                )
                _check(r)
                note_raw: dict | None = None
                if body:
                    prefixed = ensure_comment_prefix(body)
                    rn = client.post(
                        f"/projects/{path}/merge_requests/{pr_id}/notes",
                        json={"body": prefixed},
                    )
                    _check(rn)
                    note_raw = rn.json()
                mr_raw = r.json()
                if note_raw is not None:
                    _approve_note_id = note_raw.get("id")
                    if (
                        project is not None
                        and project.web_url
                        and _approve_note_id is not None
                    ):
                        _approve_url = _canonical_url(
                            f"{project.web_url}/-/merge_requests/{pr_id}"
                            f"#note_{_approve_note_id}",
                            project,
                        )
                    else:
                        _approve_url = note_raw.get("web_url") or ""
                else:
                    _approve_url = _canonical_url(
                        mr_raw.get("web_url") or "", project
                    )
                return Review(
                    id=str((note_raw or {}).get("id") or mr_raw.get("iid", "")),
                    state="approve",
                    author=((note_raw or {}).get("author") or {}).get(
                        "username", ""
                    )
                    or (mr_raw.get("user") or {}).get("username", ""),
                    body=(note_raw or {}).get("body", "") if note_raw else "",
                    url=_approve_url,
                    submitted_at=(note_raw or {}).get("created_at")
                    or mr_raw.get("updated_at")
                    or "",
                    commit_sha=None,
                )

            if state == "request_changes":
                # Best-effort unapprove (404 means we weren't approved
                # — fine to ignore; any other error must propagate).
                ru = client.post(
                    f"/projects/{path}/merge_requests/{pr_id}/unapprove",
                )
                if ru.status_code not in (200, 201, 204, 404, 409):
                    _check(ru)
                prefixed = ensure_comment_prefix(body or "")
                rn = client.post(
                    f"/projects/{path}/merge_requests/{pr_id}/notes",
                    json={"body": prefixed},
                )
                _check(rn)
                note_raw = rn.json()
                _rc_note_id = note_raw.get("id")
                if (
                    project is not None
                    and project.web_url
                    and _rc_note_id is not None
                ):
                    _rc_url = _canonical_url(
                        f"{project.web_url}/-/merge_requests/{pr_id}"
                        f"#note_{_rc_note_id}",
                        project,
                    )
                else:
                    _rc_url = note_raw.get("web_url") or ""
                return Review(
                    id=str(note_raw.get("id", "")),
                    state="request_changes",
                    author=(note_raw.get("author") or {}).get("username", ""),
                    body=note_raw.get("body", ""),
                    url=_rc_url,
                    submitted_at=note_raw.get("created_at") or "",
                    commit_sha=None,
                )

            # state == "comment"
            prefixed = ensure_comment_prefix(body or "")
            rn = client.post(
                f"/projects/{path}/merge_requests/{pr_id}/notes",
                json={"body": prefixed},
            )
            _check(rn)
            note_raw = rn.json()
            _comment_note_id = note_raw.get("id")
            if (
                project is not None
                and project.web_url
                and _comment_note_id is not None
            ):
                _comment_url = _canonical_url(
                    f"{project.web_url}/-/merge_requests/{pr_id}"
                    f"#note_{_comment_note_id}",
                    project,
                )
            else:
                _comment_url = note_raw.get("web_url") or ""
            return Review(
                id=str(note_raw.get("id", "")),
                state="comment",
                author=(note_raw.get("author") or {}).get("username", ""),
                body=note_raw.get("body", ""),
                url=_comment_url,
                submitted_at=note_raw.get("created_at") or "",
                commit_sha=None,
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
        """Merge a merge request.

        `merge_method` mapping (unified with GitHub — see #52 F1):
          - `"merge"` → POST `.../merge` with no `squash` flag (true
            merge commit).
          - `"squash"` → POST `.../merge` with `squash=true`.
          - `"rebase"` → rejected. GitLab's rebase flow is a separate
            `PUT .../rebase` endpoint that doesn't perform the merge
            itself; agents wanting a rebase-first merge should call
            the rebase endpoint and then `merge_pr(merge_method="merge")`.
            We surface a clear error here rather than silently doing
            something different.

        `commit_title` / `commit_message` are joined into the GitLab-side
        `merge_commit_message` (for `"merge"`) or `squash_commit_message`
        (for `"squash"`):
          - both set → `"<title>\\n\\n<message>"`
          - only `commit_title` set → title used as the whole message
          - only `commit_message` set → message used unchanged
          - neither set → no commit message override sent

        After the merge call, the MR is re-fetched so the response
        carries `merged_at`, `merge_commit_sha`, and the final
        `state="merged"`.
        """
        if merge_method == "rebase":
            raise ValueError(
                "GitLab does not support 'rebase' as a merge_method. "
                "Use a separate rebase flow (PUT .../rebase) then call "
                "merge_pr(merge_method='merge')."
            )
        if merge_method not in ("merge", "squash"):
            raise ValueError(
                f"unsupported merge_method {merge_method!r} — accepted: "
                f"merge, squash"
            )
        path = _project_path(project)
        payload: dict[str, Any] = {}
        if merge_method == "squash":
            payload["squash"] = True
        # Join commit_title + commit_message into the appropriate
        # GitLab-side field. GitLab has no separate title/body split
        # for merge commits.
        if commit_title is not None and commit_message is not None:
            joined_message: str | None = f"{commit_title}\n\n{commit_message}"
        elif commit_title is not None:
            joined_message = commit_title
        elif commit_message is not None:
            joined_message = commit_message
        else:
            joined_message = None
        if joined_message is not None:
            # GitLab uses `merge_commit_message` for merge, and
            # `squash_commit_message` for squash. Send the appropriate one.
            if merge_method == "squash":
                payload["squash_commit_message"] = joined_message
            else:
                payload["merge_commit_message"] = joined_message
        with _client(project, token) as client:
            r = client.put(
                f"/projects/{path}/merge_requests/{pr_id}/merge", json=payload,
            )
            try:
                _check(r)
            except GitLabError as exc:
                if exc.status == 405:
                    raise GitLabError(
                        405, f"PR '{project.id}#{pr_id}' is already merged"
                    ) from exc
                raise
            # Re-fetch so the response captures the post-merge state
            # (merged_at, merge_commit_sha, state=merged). The merge
            # endpoint returns the MR, but mirror GitHub's pattern of
            # an explicit re-fetch so any server-side post-merge
            # mutations (e.g. webhook-driven label edits) are reflected.
            r2 = client.get(f"/projects/{path}/merge_requests/{pr_id}")
            _check(r2)
            return _map_mr(r2.json(), project)

    # ---------- relations (write side) ---------------------------------------

    _SUPPORTED_RELATION_KINDS: tuple[str, ...] = ("relates_to", "duplicate_of")

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
          - `blocks` / `blocked_by` / `relates_to` → Issue Links REST
            (`POST /projects/:id/issues/:iid/links` with link_type
            `blocks` / `is_blocked_by` / `relates_to`).
          - `duplicate_of` → body-edit (append `Duplicate of !N`) plus
            close the source plus add a `relates_to` issue link so the
            duplicate is reachable through the structured-link surface
            too. The body edit is re-marked via `apply_body_marker` so
            the AI-attribution marker stays consistent.
          - `parent` / `child` → GitLab Work Items GraphQL (planned;
            see follow-up). Currently raises `RelationKindUnsupported`
            so callers don't silently fall through.

        `target` is parsed via `_parse_gitlab_relation_target`;
        currently same-project only.
        """
        if kind == "parent" or kind == "child":
            # Work Items GraphQL hierarchyWidget is non-trivial — left
            # as a follow-up so the rest of this surface lands.
            raise RelationKindUnsupported(
                kind, "gitlab", self._SUPPORTED_RELATION_KINDS,
            )
        if kind not in self._SUPPORTED_RELATION_KINDS:
            raise RelationKindUnsupported(
                kind, "gitlab", self._SUPPORTED_RELATION_KINDS,
            )
        target_project, target_iid = _parse_gitlab_relation_target(
            target, project,
        )
        _assert_not_self_relation(ticket_id, target_iid)
        path = _project_path(project)
        with _client(project, token) as client:
            if kind == "relates_to":
                link_type = _gitlab_link_type(kind)
                return _gitlab_post_issue_link(
                    client, path, ticket_id,
                    target_project_path=target_project,
                    target_issue_iid=target_iid,
                    link_type=link_type,
                    relation_kind_for_caller=kind,
                    project=project,
                )
            if kind == "duplicate_of":
                return _gitlab_mark_duplicate_of(
                    client, project, ticket_id,
                    target_project_path=target_project,
                    target_iid=target_iid,
                )
            raise RelationKindUnsupported(  # pragma: no cover
                kind, "gitlab", self._SUPPORTED_RELATION_KINDS,
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

        For `duplicate_of`, removal reopens the source (state_event
        =reopen), deletes the auxiliary `relates_to` link, and strips
        the `Duplicate of #N` line from the body so that a subsequent
        `get_ticket` no longer surfaces the relation.
        """
        if kind == "parent" or kind == "child":
            raise RelationKindUnsupported(
                kind, "gitlab", self._SUPPORTED_RELATION_KINDS,
            )
        if kind not in self._SUPPORTED_RELATION_KINDS:
            raise RelationKindUnsupported(
                kind, "gitlab", self._SUPPORTED_RELATION_KINDS,
            )
        target_project, target_iid = _parse_gitlab_relation_target(
            target, project,
        )
        path = _project_path(project)
        with _client(project, token) as client:
            if kind == "relates_to":
                _gitlab_delete_issue_link(
                    client, path, ticket_id,
                    target_project_path=target_project,
                    target_issue_iid=target_iid,
                    kind=kind,
                )
                return {"removed": True}
            if kind == "duplicate_of":
                # Tear down the relates_to link (best-effort) and
                # reopen. Also strip the "Duplicate of #N" body line so
                # a subsequent get_ticket no longer resurfaces the relation.
                try:
                    _gitlab_delete_issue_link(
                        client, path, ticket_id,
                        target_project_path=target_project,
                        target_issue_iid=target_iid,
                        kind=kind,
                    )
                except (GitLabError, RelationNotFound):
                    # Link may already be gone; reopen anyway.
                    pass
                # GET current body so we can strip the dup line.
                src_r = client.get(f"/projects/{path}/issues/{ticket_id}")
                _check(src_r)
                src = src_r.json()
                current_body = src.get("description") or ""
                current_labels = set(src.get("labels") or [])
                will_be_ai_generated = AI_GENERATED_LABEL in current_labels
                # Strip the exact "Duplicate of #<target_iid>" line and
                # any blank line that immediately follows it. Leave any
                # other "Duplicate of #M" lines (different target) intact.
                dup_line = f"Duplicate of #{target_iid}"
                body_core = strip_leading_ai_marker(current_body)
                # Remove the dup line plus optional trailing blank line.
                # (?!\d) is a negative-lookahead that prevents a partial
                # iid match, e.g. "Duplicate of #7" must not eat the "7"
                # prefix of "Duplicate of #70".
                body_core = re.sub(
                    rf"^{re.escape(dup_line)}(?!\d)\n?(?:\n)?",
                    "",
                    body_core,
                    flags=re.MULTILINE,
                ).strip("\n")
                new_body = apply_body_marker(
                    body_core, will_be_ai_generated=will_be_ai_generated,
                )
                pr = client.put(
                    f"/projects/{path}/issues/{ticket_id}",
                    json={"description": new_body, "state_event": "reopen"},
                )
                _check(pr)
                return {"removed": True}
            raise RelationKindUnsupported(  # pragma: no cover
                kind, "gitlab", self._SUPPORTED_RELATION_KINDS,
            )

    # ---------- pipelines / CI runs ------------------------------------------

    def list_runs_for_branch(
        self,
        project: ProjectConfig,
        token: str | None,
        branch: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List pipelines for `branch`.

        Returns ``(runs, resolved_refs)`` to mirror the tag/ticket shape:
        - ``([], [])`` — branch not found
        - ``(runs, [sha])`` — branch exists (runs may be empty)
        """
        _validate_limit(limit)
        path = _project_path(project)
        with _client(project, token) as client:
            sha = _resolve_gitlab_branch_sha(client, path, branch)
        if sha is None:
            return [], []
        params: dict[str, Any] = {"ref": branch}
        scope = _gitlab_pipeline_scope(status)
        if scope:
            params["scope"] = scope
        runs = _list_pipelines(project, token, params, limit)
        return runs, [sha]

    def list_runs_for_commit(
        self,
        project: ProjectConfig,
        token: str | None,
        sha: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List pipelines whose ``sha`` matches.

        Returns ``(runs, resolved_refs)`` to mirror the tag/ticket shape:
        - ``([], [])`` — commit not found
        - ``(runs, [sha])`` — commit exists (runs may be empty)
        """
        _validate_limit(limit)
        path = _project_path(project)
        with _client(project, token) as client:
            exists = _resolve_gitlab_commit(client, path, sha)
        if not exists:
            return [], []
        params: dict[str, Any] = {"sha": sha}
        scope = _gitlab_pipeline_scope(status)
        if scope:
            params["scope"] = scope
        runs = _list_pipelines(project, token, params, limit)
        return runs, [sha]

    def list_runs_for_tag(
        self,
        project: ProjectConfig,
        token: str | None,
        tag: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """GitLab does not distinguish branch/tag refs in the pipelines
        query — both go through the `ref` parameter. We pass through
        and document the gap rather than synthesize a tag filter that
        the API doesn't support.

        Returns `(runs, resolved_refs)` to match the GitHub signature
        (`resolved_refs` lists the single ref string we queried with).
        """
        params: dict[str, Any] = {"ref": tag}
        scope = _gitlab_pipeline_scope(status)
        if scope:
            params["scope"] = scope
        return _list_pipelines(project, token, params, limit), [tag]

    def list_runs_for_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        status: str = "all",  # noqa: ARG002 — accepted for cross-provider symmetry
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Issues do not trigger pipelines directly. Strategy:
        1. Fetch MRs linked to the issue (`.../issues/:iid/related_merge_requests`).
        2. For each MR, fetch its pipelines (`.../merge_requests/:iid/pipelines`).
        3. Concatenate, sort by created_at desc, cap at `limit`.

        Returns `(runs, resolved_refs)` — `resolved_refs` lists the
        MR iids (prefixed with `!`) we walked, mirroring the GitHub
        signature so `tools/pipelines.py` can unpack both providers
        the same way (#49 finding 1 fix).

        `status` is accepted for surface symmetry but not applied —
        the per-MR pipelines endpoint doesn't expose a usable scope
        filter for this aggregation path; client-side filtering would
        be misleading here.
        """
        _validate_limit(limit)
        path = _project_path(project)
        per_page = min(max(1, limit), 100)
        resolved_refs: list[str] = []
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{path}/issues/{ticket_id}/related_merge_requests",
            )
            _check(r)
            related = r.json()
            collected: list[dict] = []
            for mr in related:
                mr_iid = mr.get("iid")
                if mr_iid is None:
                    continue
                resolved_refs.append(f"!{mr_iid}")
                pr = client.get(
                    f"/projects/{path}/merge_requests/{mr_iid}/pipelines",
                    params={"per_page": per_page},
                )
                if pr.is_success:
                    collected.extend(pr.json())
        # Sort newest first, mirror GitHub's default.
        collected.sort(
            key=lambda r: r.get("created_at", ""), reverse=True,
        )
        runs = [_map_pipeline_run(it) for it in collected[:per_page]]
        return runs, resolved_refs

    def list_runs_recent(
        self,
        project: ProjectConfig,
        token: str | None,
        *,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List the most recent pipelines, unfiltered by ref.

        Returns ``(runs, [])`` — the empty ``resolved_refs`` signals that
        no ref filter was applied.
        """
        params: dict[str, Any] = {}
        scope = _gitlab_pipeline_scope(status)
        if scope:
            params["scope"] = scope
        return _list_pipelines(project, token, params, limit), []

    def get_run(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
        *,
        include_failure_excerpt: bool = False,
    ) -> PipelineRun:
        """Fetch a single pipeline.

        When `include_failure_excerpt=True` and the pipeline concluded
        as failed, also fetch the failing jobs and a trace excerpt for
        each. GitLab does not expose GitHub-style annotations; the
        `annotations` field on each `FailingJob` is therefore `[]`.
        """
        if not str(run_id).strip().isdigit():
            raise GitLabError(
                404,
                f"pipeline '{project.id}#{run_id}' not found"
                f" — run_id must be a numeric pipeline id",
            )
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(f"/projects/{path}/pipelines/{run_id}")
            _check(r)
            run = _map_pipeline_run(r.json())
            if include_failure_excerpt and run.conclusion == "failed":
                run.failure = _fetch_pipeline_failure(
                    client, project, run_id,
                )
        return run

    # ---------- label management ---------------------------------------------

    def list_labels(
        self,
        project: ProjectConfig,
        token: str | None,
    ) -> list[Label]:
        """List all labels on the project.

        Uses `GET /projects/{id}/labels` with `per_page=100`.
        GitLab returns `color` as `#RRGGBB`; passed through as-is.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{path}/labels",
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
        """Create a new label on the project.

        `color` is a `#RRGGBB` string. If the caller passes a bare 6-hex
        string without `#`, this method prefixes it automatically.
        Defaults to ``"#ededed"`` when `color` is ``None``.

        409 conflict → `GitLabError(409, "label '{name}' already exists")`.
        """
        payload: dict[str, Any] = {
            "name": name,
            "color": _normalize_gitlab_color(color),
        }
        if description is not None:
            payload["description"] = description
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.post(f"/projects/{path}/labels", json=payload)
        if r.status_code == 409:
            raise GitLabError(409, f"label {name!r} already exists")
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

        Uses `PUT /projects/{id}/labels/{name}` (GitLab 14+).
        Color normalization same as `create_label`.
        404 → `GitLabError(404, "label '{name}' not found in {project.id}")`.
        """
        if new_name is None and color is None and description is None:
            raise ValueError(
                "update_label requires at least one of: new_name, color, description"
            )
        payload: dict[str, Any] = {}
        if new_name is not None:
            payload["new_name"] = new_name
        if color is not None:
            payload["color"] = _normalize_gitlab_color(color)
        if description is not None:
            payload["description"] = description
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.put(
                f"/projects/{path}/labels/{name}",
                json=payload,
            )
        if r.status_code == 404:
            raise GitLabError(404, f"label {name!r} not found in {project.id}")
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
        """Delete a label from the project.

        Uses `DELETE /projects/{id}/labels?name={name}`.
        GitLab returns 204 on success.
        404 → `GitLabError(404, "label '{name}' not found in {project.id}")`.

        Note: The GitLab REST API does NOT support the label name as a
        path segment for DELETE. The name must be passed as a query
        parameter: `DELETE /projects/{id}/labels?name={name}`.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.delete(f"/projects/{path}/labels", params={"name": name})
        if r.status_code == 404:
            raise GitLabError(404, f"label {name!r} not found in {project.id}")
        _check(r)
        return None


# ---------- module-level helpers ---------------------------------------------


def _normalize_gitlab_color(color: str | None) -> str:
    """Normalise a GitLab label colour to the required `#RRGGBB` form.

    - ``None``         → ``"#ededed"`` (default colour)
    - ``"ff0000"``     → ``"#ff0000"`` (bare hex → prefixed)
    - ``"#ff0000"``    → ``"#ff0000"`` (already correct, unchanged)
    """
    if not color:
        return "#ededed"
    if not color.startswith("#"):
        return f"#{color}"
    return color


__all__ = [
    "GitLabError",
    "GitLabProvider",
    "DEFAULT_BASE_URL",
    "USER_AGENT",
]
