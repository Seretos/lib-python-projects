"""Azure DevOps provider — REST 7.1 implementation (ticket #40).

Mirrors the surface area of `providers/github.py` and `providers/gitlab.py`
so the tool layer can dispatch through `_provider_for(project)` unchanged.

Key Azure DevOps quirks (vs GitHub/GitLab) this module handles:

  * Auth: PAT via HTTP Basic with empty username (`":{PAT}"`).
  * Identifier scopes: work items live at `organization/project`; pull
    requests + threads live at `organization/project/repository`. The
    YAML `path` field carries all three; the provider splits it via
    `project.organization` / `project.ado_project` / `project.repository`.
  * Repository id: PR endpoints want a GUID, not a name. We resolve
    once per `(org, project, repo_name)` and cache.
  * Process templates: each ADO project has its own state vocabulary
    discoverable via `/_apis/wit/workitemtypes/{type}/states`. We cache
    per `(org, project, type)` for the same 1h that the `tools/tickets`
    response cache uses.
  * Bodies & comments: stored as HTML on ADO, raw markdown on GitHub.
    A minimal stdlib-only MD↔HTML converter keeps the agent-visible
    body in markdown so the `markers.py` machinery works unchanged.
  * Relations: JSON-Patch on the work item's `/relations` array. The
    array index for removal is computed by reading the current state.
  * Pull-request comments: ADO has no flat "issue comments" vs "review
    comments" distinction — everything is a *thread* hanging off the
    PR. We surface threads with no `threadContext` as `Comment`s and
    threads with `threadContext` as `ReviewComment`s.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from html import escape as html_escape, unescape as html_unescape
from typing import Any
from urllib.parse import quote

import httpx

from lib_python_projects.models import ProjectConfig
from lib_python_projects.markers import (
    MarkerSet,
    apply_body_marker,
    ensure_body_prefix,
    ensure_comment_prefix,
    has_ai_generated_marker,
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
    LabelOperationUnsupported,
    PRFilters,
    PipelineFailure,
    PipelineRun,
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
    TokenCapabilityProvider,
    TokenProjectDiscoveryProvider,
    WRITABLE_RELATION_KINDS,
    normalize_timestamp,
    _assert_not_self_relation,
    _extract_parent_id,
    _validate_label_lists,
    _validate_limit,
)
from lib_python_projects.providers._http_cache import make_cached_transport
from lib_python_projects.providers import _idempotency

log = logging.getLogger("project-issues.azuredevops")

USER_AGENT = "claude-code-project-issues-plugin/0.1.0"
API_VERSION = "7.1"
# Comments live on the preview-marked endpoint; the GA version drops the
# suffix once Microsoft promotes it.
API_VERSION_COMMENTS = "7.1-preview.4"

# Sentinel distinguishing "milestone kwarg not passed" from "explicitly
# clear the milestone" (`milestone=None`) on `create_ticket` /
# `update_ticket` (ticket #151).
_UNSET: Any = object()


# ---------- error type -------------------------------------------------------


class AzureDevOpsError(ProviderError):
    def __init__(self, status: int, message: str):
        RuntimeError.__init__(self, f"Azure DevOps {status}: {message}")
        self.status = status
        self.message = message


# ---------- client + error mapping ------------------------------------------


def _basic_auth_header(token: str) -> str:
    raw = f":{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _client(
    project: ProjectConfig,
    token: str | None,
    *,
    base_url: str | None = None,
) -> httpx.Client:
    """Build an httpx.Client targeted at the ADO REST root.

    Base URL resolution order:
    1. ``base_url`` kwarg (used by discovery calls to VSSPS).
    2. ``project.base_url`` (covers self-hosted Azure DevOps Server).
    3. ``https://dev.azure.com`` (cloud default).
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = _basic_auth_header(token)
    base = (base_url or project.base_url or "https://dev.azure.com").rstrip("/")
    return httpx.Client(
        base_url=base,
        headers=headers,
        timeout=30.0,
        transport=make_cached_transport(),
    )


def _marker_set(project: ProjectConfig) -> MarkerSet:
    return MarkerSet(project.auto_labels.ai_generated, project.auto_labels.ai_modified)


# ---------- discovery sentinel + helpers ------------------------------------

# A throwaway ProjectConfig used solely as the first argument to `_client`
# for discovery calls. ADO validation requires exactly 2 slashes and no
# empty segments in the path, so we use three non-empty dummy segments.
_DISCOVERY_PROJECT_SENTINEL = None  # populated after ProjectConfig is imported


def _get_discovery_sentinel() -> "ProjectConfig":
    """Return (and lazily create) the module-level discovery sentinel."""
    global _DISCOVERY_PROJECT_SENTINEL
    if _DISCOVERY_PROJECT_SENTINEL is None:
        from lib_python_projects.models import ProjectConfig as _PC
        _DISCOVERY_PROJECT_SENTINEL = _PC(
            id="_disc",
            provider="azuredevops",
            path="_disc/_disc/_disc",
        )
    return _DISCOVERY_PROJECT_SENTINEL


def _org_hint(base_url: str | None) -> str | None:
    """Return an organisation name from *base_url* or the environment.

    Resolution order:
    1. Parse *base_url*: strip scheme + host, take the first non-empty
       path segment when a real org path is present beyond the bare
       ``https://dev.azure.com`` host (e.g. ``https://dev.azure.com/myorg``
       → ``"myorg"``).
    2. ``AZURE_DEVOPS_ORG`` environment variable.

    Returns ``None`` when neither source yields a value.
    """
    if base_url:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        segments = [s for s in parsed.path.split("/") if s]
        if segments:
            return segments[0]
    return os.environ.get("AZURE_DEVOPS_ORG") or None


def _discover_orgs_via_api(
    token: str,
) -> tuple[list[str], str | None]:
    """Discover ADO organisations accessible to *token* via the VSSPS API.

    Returns ``(org_names, reason_or_none)``.  On any failure the org list
    is empty and ``reason`` carries one of the taxonomy strings defined on
    ``ProjectDiscoveryResult``.
    """
    sentinel = _get_discovery_sentinel()
    vssps = "https://app.vssps.visualstudio.com"
    try:
        with _client(sentinel, token, base_url=vssps) as c:
            profile_resp = c.get(
                "/_apis/profile/profiles/me",
                params={"api-version": "7.1"},
            )
    except httpx.HTTPError:
        return [], "network_error"

    if profile_resp.status_code == 401:
        return [], "bad_credentials"
    if not profile_resp.is_success:
        return [], f"http_{profile_resp.status_code}"

    member_id = profile_resp.json().get("id", "")

    try:
        with _client(sentinel, token, base_url=vssps) as c:
            accounts_resp = c.get(
                "/_apis/accounts",
                params={"memberId": member_id, "api-version": "7.1"},
            )
    except httpx.HTTPError:
        return [], "network_error"

    if accounts_resp.status_code == 401:
        return [], "bad_credentials"
    if not accounts_resp.is_success:
        return [], f"http_{accounts_resp.status_code}"

    orgs = [
        entry["accountName"]
        for entry in accounts_resp.json().get("value", [])
        if entry.get("accountName")
    ]
    if not orgs:
        return [], "repo_invisible_to_token"
    return orgs, None


_NOT_FOUND_TYPE_KEYS: frozenset[str] = frozenset(
    {
        "WorkItemNotFoundException",
        "RelatedArtifactNotFoundException",
        "GitItemNotFoundException",
        "GitPullRequestNotFoundException",
        "WorkItemTypeNotFoundException",
        "CommentNotFoundException",
        "WorkItemCommentNotFoundException",
        "ItemNotFoundException",
        "BuildNotFoundException",
    }
)

_TRANSITION_TYPE_KEYS: frozenset[str] = frozenset(
    {
        "WorkItemTransitionDeniedException",
        "WorkItemSaveFailedException",
        "RuleValidationException",
        "WorkItemRuleException",
        # InvalidArgumentValueException intentionally excluded: it fires on
        # Title-empty, Assignee-format, and other non-state errors, causing
        # the list_ticket_statuses hint to appear in unrelated error messages.
        # Genuine state-value errors still receive the hint via
        # _TRANSITION_MSG_FRAGMENTS message matching below.
    }
)

_TRANSITION_MSG_FRAGMENTS: tuple[str, ...] = (
    "transition",
    "is not a valid state",
    "not in the allowed",
    "allowed list",
    "allowed values",
)

# When typeKey is in this set the transition hint fires ONLY when the message
# also mentions a state-related term.  This prevents the hint appearing on
# non-state InvalidArgumentValueException errors (e.g. Title-empty, bad
# assignee format) whose messages happen to contain words like "allowed values".
_FRAGMENT_GATED_TYPE_KEYS: frozenset[str] = frozenset(
    {"InvalidArgumentValueException"}
)

# ADO occasionally returns HTTP 500 for "work item does not exist" instead
# of the expected 404 / 400.  Narrow-match these so genuine server errors
# with unrelated messages are not reclassified.
_500_NOT_FOUND_MSG_FRAGMENTS: tuple[str, ...] = (
    "does not exist",
    "could not be found",
    "was not found",
)


def _is_area_path_not_found(resp: httpx.Response) -> bool:
    """True when *resp* is a WIQL 400/404 signalling an unknown area path.

    ADO's WIQL endpoint 404s with ``TF51011: The specified area path does
    not exist.`` (sometimes surfaced as 400) instead of the documented
    "invalid area path yields zero matches" behaviour. Anchors on the
    ``TF51011`` code the same way the `TF401181` PR-review check does
    elsewhere in this module, with a message-fragment fallback for
    phrasings that omit the TF code. Parses the envelope the same way
    `_check` does (message + innerException).
    """
    if resp.status_code not in (400, 404):
        return False
    try:
        payload = resp.json()
        msg = payload.get("message") or ""
        inner = payload.get("innerException")
        if isinstance(inner, dict) and inner.get("message"):
            msg = f"{msg}: {inner['message']}"
    except Exception:
        return False
    msg_lower = msg.lower()
    return "tf51011" in msg_lower or (
        "area path" in msg_lower and "does not exist" in msg_lower
    )


def _check(resp: httpx.Response) -> None:
    """Translate a non-success ADO response into an `AzureDevOpsError`.

    Three extra translations on top of the raw envelope:
      - ADO returns 400 for several "not found" classes (deleted work
        items, missing PR refs, unknown work-item types). We re-tag
        those as 404 so the tool-layer `_rewrap_404` adds the
        `kind 'project#id' not found` context.
      - ADO returns 500 for some "work item does not exist" conditions
        (e.g. add_comment on a missing work item). Message fragments in
        `_500_NOT_FOUND_MSG_FRAGMENTS` trigger a 500→404 remap; genuine
        server errors with unrelated messages are NOT reclassified.
      - Work-item state-transition 400s get a hint pointing at
        `list_ticket_statuses`, mirroring the GitHub/GitLab providers.
    """
    if resp.status_code == 304:
        return
    if resp.is_success:
        return
    type_key: str = ""
    try:
        payload = resp.json()
        # Azure DevOps error envelopes:
        #   {"message": "...", "typeKey": "...", ...}
        #   {"$id":"1","innerException":null,"message":"...","typeKey":"..."}
        #   {"value": {...}, "count": 0}  (rare; only when a 200 wrapper)
        msg = payload.get("message") or resp.reason_phrase
        type_key = payload.get("typeKey") or ""
        inner = payload.get("innerException")
        if isinstance(inner, dict) and inner.get("message"):
            msg = f"{msg}: {inner['message']}"
    except Exception:
        msg = resp.reason_phrase or "request failed"

    status = resp.status_code
    msg_lower = (msg or "").lower()

    # 429 rate-limit: raise RateLimitError before any status remapping.
    if status == 429:
        retry_after: int | None = None
        retry_after_hdr = resp.headers.get("Retry-After")
        if retry_after_hdr is not None:
            try:
                retry_after = int(retry_after_hdr)
            except (ValueError, TypeError):
                retry_after = None
        if retry_after is None:
            reset_hdr = resp.headers.get("X-RateLimit-Reset")
            if reset_hdr is not None:
                try:
                    retry_after = max(0, int(reset_hdr) - int(time.time()))
                except (ValueError, TypeError):
                    retry_after = None
        raise RateLimitError(429, msg, retry_after=retry_after)

    # 503 with Retry-After → rate limit; 503 without → plain server error.
    if status == 503:
        retry_after_hdr_503 = resp.headers.get("Retry-After")
        if retry_after_hdr_503 is not None:
            retry_after_503: int | None = None
            try:
                retry_after_503 = int(retry_after_hdr_503)
            except (ValueError, TypeError):
                retry_after_503 = None
            raise RateLimitError(503, msg, retry_after=retry_after_503)
        raise AzureDevOpsError(503, msg)

    # 400-but-actually-404 normalization.
    if status == 400 and (
        type_key in _NOT_FOUND_TYPE_KEYS
        or "does not exist" in msg_lower
        or "could not be found" in msg_lower
        or "was not found" in msg_lower
        # int32-overflow ids surface as ".NET Int32 overflow" 400s
        # rather than 404s; treat as not-found so `_rewrap_404` adds
        # the id-echoing context. Two phrasings ADO uses:
        or "too large or too small for an int32" in msg_lower
        or "value was either too large" in msg_lower
    ):
        status = 404

    # 500-but-actually-404 normalization.  ADO returns HTTP 500 for
    # operations on missing work items in some comment endpoints.  Only
    # reclassify when the message text unambiguously signals "not found";
    # genuine server errors keep their 500 status.
    if status == 500 and any(
        frag in msg_lower for frag in _500_NOT_FOUND_MSG_FRAGMENTS
    ):
        status = 404

    # Status-transition hint parity with GitHub/GitLab. ADO surfaces
    # invalid System.State values under a handful of typeKeys with
    # message fragments that all boil down to "your value isn't in the
    # allowed list" — match any of them so the hint reliably fires.
    #
    # Exception: for typeKeys in _FRAGMENT_GATED_TYPE_KEYS (specifically
    # InvalidArgumentValueException) the fragment match is only honoured
    # when the message also contains the word "state". This prevents
    # Title-empty or Assignee-format errors from triggering the
    # list_ticket_statuses hint just because their messages happen to
    # contain "allowed values" or similar fragments.
    _frag_match = any(frag in msg_lower for frag in _TRANSITION_MSG_FRAGMENTS)
    if type_key in _FRAGMENT_GATED_TYPE_KEYS:
        _frag_match = _frag_match and "state" in msg_lower

    if (
        status in (400, 409)
        and (
            type_key in _TRANSITION_TYPE_KEYS
            or _frag_match
        )
        and "list_ticket_statuses" not in msg
    ):
        msg = f"{msg} — use list_ticket_statuses to discover valid values"

    raise AzureDevOpsError(status, msg)


# ---------- scope helpers ----------------------------------------------------


def _project_scope(project: ProjectConfig) -> str:
    """Return the `/{org}/{project}` URL prefix used by work-item endpoints."""
    org, proj = project.organization, project.ado_project
    if not org or not proj:
        raise AzureDevOpsError(
            400,
            f"project '{project.id}': missing organization/project in "
            f"path {project.path!r}",
        )
    return f"/{quote(org, safe='')}/{quote(proj, safe='')}"


def _org_scope(project: ProjectConfig) -> str:
    """Return the `/{org}` URL prefix used by org-wide endpoints."""
    org = project.organization
    if not org:
        raise AzureDevOpsError(
            400,
            f"project '{project.id}': missing organization in path "
            f"{project.path!r}",
        )
    return f"/{quote(org, safe='')}"


def _area_path_clause(area: str | None, recursive: bool) -> str | None:
    """Build the `[System.AreaPath]` WIQL clause for *area*, or `None`.

    `recursive=True` emits `UNDER` (includes sub-areas); `False` emits an
    exact `=` match. Returns `None` when `area` is falsy so callers can
    unconditionally append the result without an extra `if`.
    """
    if not area:
        return None
    if recursive:
        return f"[System.AreaPath] UNDER '{_escape_wiql(area)}'"
    return f"[System.AreaPath] = '{_escape_wiql(area)}'"


def _effective_area_path(
    project: ProjectConfig, filters: TicketFilters
) -> tuple[str | None, bool]:
    """Resolve the area path (+ recursion flag) actually in effect.

    An explicit `filters.area_path` always wins outright — including its
    own `filters.area_path_recursive` flag. When no per-call filter is
    given, `project.area_path` (ticket #172) is used as a config-level
    default scope, always with `UNDER` (recursive) semantics — an exact
    config-level default would silently drop work items filed under
    sub-areas, which is rarely what operators configuring a static
    default want. Returns `(None, True)` when neither is set.
    """
    if filters.area_path:
        return filters.area_path, filters.area_path_recursive
    if project.area_path:
        return project.area_path, True
    return None, True


def _api_version_params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the common query-param dict, defaulting `api-version` to 7.1."""
    params: dict[str, Any] = {"api-version": API_VERSION}
    if extra:
        params.update(extra)
    return params


# ---------- caches (org/project-scoped, with TTL) ----------------------------


_CACHE_TTL_SECONDS = 60 * 60  # 1 hour, matching tools/tickets list_ticket_statuses cache
# Depth passed to the classification-nodes API's $depth param. Comfortably
# deeper than any realistic Area/Iteration tree so the full hierarchy comes
# back in a single call.
_CLASSIFICATION_DEPTH = 20

# ADO's classification-node `path` always inserts a synthetic structure
# label as the second segment (e.g. `\Project\Area\Child`); it is not part
# of a valid System.AreaPath/System.IterationPath value and must be
# stripped. Maps the `structure` argument to that singular label.
_CLASSIFICATION_STRUCTURE_LABEL: dict[str, str] = {
    "Areas": "Area",
    "Iterations": "Iteration",
}
_cache_lock = threading.Lock()
_repo_id_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_state_cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_field_cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_field_type_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
_default_type_cache: dict[tuple[str, str], tuple[float, str]] = {}
_classification_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
_field_values_cache: dict[tuple[str, str, str, str], tuple[float, list[str] | None]] = {}


def _cache_get(store: dict, key: tuple) -> Any | None:
    with _cache_lock:
        hit = store.get(key)
    if hit is None:
        return None
    expires_at, value = hit
    if time.monotonic() >= expires_at:
        with _cache_lock:
            store.pop(key, None)
        return None
    return value


def _cache_put(store: dict, key: tuple, value: Any) -> None:
    with _cache_lock:
        store[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)


def _cache_clear_all() -> None:
    """Test hook: drop all module-level caches."""
    with _cache_lock:
        _repo_id_cache.clear()
        _state_cache.clear()
        _field_cache.clear()
        _field_type_cache.clear()
        _default_type_cache.clear()
        _classification_cache.clear()
        _field_values_cache.clear()


# ---------- Markdown <-> HTML (minimal, stdlib-only) ------------------------
#
# ADO stores work-item bodies and work-item / PR-thread comments as HTML
# (the `Microsoft.VSTS.WorkItemTypes.*` fields are HTML, and the threads
# API returns HTML in `content`). GitHub stores raw markdown. To keep the
# agent-visible body in the same shape across providers — and to keep
# the `markers.py` machinery working unchanged — we convert MD→HTML on
# write and HTML→MD on read.
#
# The converter is intentionally small. It handles the elements ADO is
# likely to round-trip cleanly (headings, bold/italic, links, code, lists,
# paragraphs, line breaks). Anything richer survives via HTML escape on
# write and tag-stripping on read.

_FENCED_CODE_RE = re.compile(r"^```(\w*)\n(.*?)\n```", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_UL_ITEM_RE = re.compile(r"^[-*]\s+(.+)$")
_OL_ITEM_RE = re.compile(r"^\d+\.\s+(.+)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")


def _markdown_to_html(body: str | None) -> str:
    """Convert a small subset of CommonMark markdown to HTML.

    Empty / None input yields `""` so callers can pipe through optional
    fields unchecked. The leading AI marker line (`#ai-generated` /
    `#ai-modified`) is emitted as a literal `<p>` so it round-trips
    back to itself rather than getting reinterpreted as a heading.
    """
    if not body:
        return ""

    text = body

    # Pull leading AI marker out of the body so it isn't mis-parsed as
    # an `<h1>` heading. We re-prepend it as a plain paragraph at the end.
    marker_line: str | None = None
    m_marker = re.match(r"\A\s*(#ai-[a-z][a-z0-9-]*)\s*\n+", text)
    if m_marker:
        marker_line = m_marker.group(1)
        text = text[m_marker.end():]

    # Extract fenced code blocks first and replace them with placeholders
    # so the inline rewrites below don't touch them.
    placeholders: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        lang = match.group(1)
        content = html_escape(match.group(2))
        cls = f' class="language-{lang}"' if lang else ""
        block = f"<pre><code{cls}>{content}</code></pre>"
        placeholders.append(block)
        return f"\x00CODE{len(placeholders) - 1}\x00"

    text = _FENCED_CODE_RE.sub(_stash_code, text)

    lines = text.split("\n")
    out: list[str] = []
    para: list[str] = []
    in_ul = False
    in_ol = False
    in_bq = False

    def _flush_para() -> None:
        nonlocal para
        if para:
            # Use "<br>" without a trailing newline so the HTMLParser
            # doesn't deliver the newline as a separate data event,
            # which would produce a spurious blank line between the
            # `<br>` and the next line's content on readback.
            joined = "<br>".join(_inline_md(line) for line in para)
            out.append(f"<p>{joined}</p>")
            para = []

    def _close_lists() -> None:
        nonlocal in_ul, in_ol, in_bq
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False
        if in_bq:
            out.append("</blockquote>")
            in_bq = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            _flush_para()
            _close_lists()
            continue

        m_h = _HEADING_RE.match(line)
        if m_h:
            _flush_para()
            _close_lists()
            level = len(m_h.group(1))
            out.append(f"<h{level}>{_inline_md(m_h.group(2))}</h{level}>")
            continue

        m_ul = _UL_ITEM_RE.match(line)
        if m_ul:
            _flush_para()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if in_bq:
                out.append("</blockquote>")
                in_bq = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_md(m_ul.group(1))}</li>")
            continue

        m_ol = _OL_ITEM_RE.match(line)
        if m_ol:
            _flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if in_bq:
                out.append("</blockquote>")
                in_bq = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline_md(m_ol.group(1))}</li>")
            continue

        m_bq = _BLOCKQUOTE_RE.match(line)
        if m_bq:
            _flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_bq:
                out.append("<blockquote>")
                in_bq = True
            out.append(f"<p>{_inline_md(m_bq.group(1))}</p>")
            continue

        # Placeholder lines (fenced-code stashes) — emit directly.
        if line.startswith("\x00CODE") and line.endswith("\x00"):
            _flush_para()
            _close_lists()
            out.append(line)
            continue

        # Plain paragraph continuation.
        if in_ul or in_ol or in_bq:
            _close_lists()
        para.append(line)

    _flush_para()
    _close_lists()

    html = "\n".join(out)

    # Restore fenced code blocks.
    for idx, block in enumerate(placeholders):
        html = html.replace(f"\x00CODE{idx}\x00", block)

    if marker_line is not None:
        html = f"<p>{html_escape(marker_line)}</p>\n{html}" if html else (
            f"<p>{html_escape(marker_line)}</p>"
        )

    return html


def _inline_md(text: str) -> str:
    """Apply bold/italic/inline-code/link transforms + HTML-escape the rest."""
    # We can't simply escape first because the regexes operate on raw MD
    # markers. Instead we extract spans, escape what's between them.
    parts: list[str] = []
    i = 0

    def _emit_escaped(start: int, end: int) -> None:
        if end > start:
            parts.append(html_escape(text[start:end]))

    # Pull out inline code first — its contents are NOT further parsed.
    pos = 0
    while True:
        m = _INLINE_CODE_RE.search(text, pos)
        if not m:
            break
        _emit_escaped(pos, m.start())
        parts.append(f"<code>{html_escape(m.group(1))}</code>")
        pos = m.end()
    rest = text[pos:]

    # Now run the rest through link → bold → italic, using sentinels so the
    # angle brackets we emit aren't double-escaped.
    # IMPORTANT: do NOT html_escape inside the lambdas — the outer pass below
    # escapes only the literal (non-sentinel) regions, so span content must
    # remain unescaped here to avoid double-escaping (e.g. & → &amp; → &amp;amp;).
    rest = _LINK_RE.sub(
        lambda mm: f"\x01A\x01{mm.group(2)}\x01B\x01{mm.group(1)}\x01C\x01",
        rest,
    )
    rest = _BOLD_RE.sub(lambda mm: f"\x01D\x01{mm.group(1)}\x01E\x01", rest)
    rest = _ITALIC_RE.sub(lambda mm: f"\x01F\x01{mm.group(1)}\x01G\x01", rest)

    # Escape only the literal text regions between sentinel-wrapped spans.
    # Split on the sentinel characters (\x01); odd-indexed tokens are span
    # bodies (already to be rendered as HTML content), even-indexed tokens are
    # literal text that needs escaping.
    _SENTINEL = "\x01"
    tokens = rest.split(_SENTINEL)
    # tokens[0], tokens[2], tokens[4], … are literal text (escape them).
    # tokens[1], tokens[3], tokens[5], … are span-type markers or content.
    escaped_tokens: list[str] = []
    for idx, tok in enumerate(tokens):
        if idx % 2 == 0:
            escaped_tokens.append(html_escape(tok))
        else:
            # This is inside a sentinel pair — it's either a marker letter
            # (e.g. "A", "D") or the raw content between two sentinels.
            # Content between an opening marker (e.g. "A\x01...") and a
            # closing marker must be HTML-escaped too (it's raw MD text).
            escaped_tokens.append(html_escape(tok))

    rest = (
        _SENTINEL.join(escaped_tokens)
        .replace("\x01A\x01", '<a href="')
        .replace("\x01B\x01", '">')
        .replace("\x01C\x01", "</a>")
        .replace("\x01D\x01", "<strong>")
        .replace("\x01E\x01", "</strong>")
        .replace("\x01F\x01", "<em>")
        .replace("\x01G\x01", "</em>")
    )
    parts.append(rest)
    return "".join(parts)


class _MarkdownExtractor(HTMLParser):
    """Walk an HTML tree, emitting a markdown-shaped reconstruction.

    Inverse of `_markdown_to_html`. Unknown elements drop their wrapping
    and emit their text content; the goal is "no information loss" for
    elements we know how to map and "no garbage" for everything else.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._list_stack: list[str] = []  # "ul" / "ol"
        self._ol_counters: list[int] = []
        self._in_pre = 0
        self._in_code = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._in_link = 0
        # Index into `_out` of the opening fence emitted by the `pre`
        # handler, waiting for the `code` tag to append a language tag.
        # None means we're not in a pending-language-fence state.
        self._pre_fence_idx: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._emit("\n" + "#" * level + " ")
        elif tag == "p":
            if self._out and not self._out[-1].endswith("\n\n"):
                self._emit("\n\n")
        elif tag == "br":
            self._emit("\n")
        elif tag == "strong" or tag == "b":
            self._emit("**")
        elif tag == "em" or tag == "i":
            self._emit("*")
        elif tag == "code":
            self._in_code += 1
            if self._in_pre:
                # The `<pre>` handler already emitted the opening fence
                # as "\n```" (without a trailing newline) and recorded
                # its index. Now we know the language from the class
                # attribute (`class="language-X"`). Append the language
                # tag (if present) and close the fence line with "\n".
                if self._pre_fence_idx is not None:
                    cls_attr = attr.get("class") or ""
                    if cls_attr.startswith("language-"):
                        lang = cls_attr[len("language-"):]
                        self._out[self._pre_fence_idx] += lang
                    self._out[self._pre_fence_idx] += "\n"
                    self._pre_fence_idx = None
            else:
                self._emit("`")
        elif tag == "pre":
            self._in_pre += 1
            # Emit the opening fence without a trailing newline so the
            # `code` handler can append the language tag first. The
            # index is stored in `_pre_fence_idx`; the `code` handler
            # will append "\n" (with or without a language tag) when it
            # runs, completing the opening fence line.
            self._emit("\n```")
            self._pre_fence_idx = len(self._out) - 1
        elif tag == "ul":
            self._list_stack.append("ul")
            self._emit("\n")
        elif tag == "ol":
            self._list_stack.append("ol")
            self._ol_counters.append(0)
            self._emit("\n")
        elif tag == "li":
            if self._list_stack and self._list_stack[-1] == "ol":
                self._ol_counters[-1] += 1
                self._emit(f"{self._ol_counters[-1]}. ")
            else:
                self._emit("- ")
        elif tag == "blockquote":
            self._emit("\n> ")
        elif tag == "a":
            self._in_link += 1
            self._link_href = attr.get("href")
            self._link_text = []
        elif tag == "div":
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n\n")
        elif tag == "p":
            self._emit("\n\n")
        elif tag == "strong" or tag == "b":
            self._emit("**")
        elif tag == "em" or tag == "i":
            self._emit("*")
        elif tag == "code":
            self._in_code = max(0, self._in_code - 1)
            if not self._in_pre:
                self._emit("`")
        elif tag == "pre":
            self._in_pre = max(0, self._in_pre - 1)
            self._emit("\n```\n")
        elif tag == "ul":
            if self._list_stack:
                self._list_stack.pop()
            self._emit("\n")
        elif tag == "ol":
            if self._list_stack:
                self._list_stack.pop()
            if self._ol_counters:
                self._ol_counters.pop()
            self._emit("\n")
        elif tag == "li":
            self._emit("\n")
        elif tag == "blockquote":
            self._emit("\n")
        elif tag == "a":
            text = "".join(self._link_text)
            href = self._link_href or ""
            self._in_link = max(0, self._in_link - 1)
            self._link_text = []
            self._link_href = None
            if href:
                self._emit(f"[{text}]({href})")
            else:
                self._emit(text)
        elif tag == "div":
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_text.append(data)
            return
        if self._in_pre:
            self._emit(data)
            return
        # The HTMLParser hands us the literal whitespace between block
        # elements (e.g. the `\n` between `</li>` and `<li>`); inside a
        # list that compounds with the `\n` we emit on `</li>` end and
        # produces a blank line between bullets. Drop whitespace-only
        # data while we're inside a list to keep bullets adjacent.
        if self._list_stack and not data.strip():
            return
        self._emit(data)

    def _emit(self, s: str) -> None:
        self._out.append(s)

    def result(self) -> str:
        text = "".join(self._out)
        # Collapse runs of three+ blank lines and strip leading/trailing
        # whitespace so the markdown matches what a human would write.
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Defensive: strip trailing whitespace per line so ADO's HTML
        # editor / round-trip artefacts (e.g. a trailing space after the
        # `#ai-generated` marker line) don't leak into the agent-facing
        # markdown.
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        return text.strip()


def _html_to_markdown(html: str | None) -> str:
    if not html:
        return ""
    parser = _MarkdownExtractor()
    parser.feed(html)
    parser.close()
    return parser.result()


# ---------- payload mappers --------------------------------------------------


def _label_list_from_tags(tags: str | None) -> list[str]:
    """ADO stores tags as a single `; `-joined string. Normalise + sort."""
    if not tags:
        return []
    return sorted({t.strip() for t in tags.split(";") if t.strip()})


def _tags_string_from_labels(labels: list[str]) -> str:
    """Inverse of `_label_list_from_tags` — deduplicates, sorts, joins.

    Validate-and-reject (ticket #172): raises `ValueError` for any label
    that is `None`, empty, or whitespace-only, and for any label
    containing ``";"`` (ADO's tag separator — a literal ``;`` inside a
    label would silently split into extra tags on round-trip through
    `_label_list_from_tags`). The previous behaviour silently dropped
    such labels instead of rejecting them, desyncing the caller's view
    of a ticket's labels from what actually got persisted.

    This is the single serialization chokepoint for `System.Tags`,
    called from `create_ticket` and `update_ticket` while they're still
    building the JSON-Patch body — before either issues its mutating
    HTTP call — so the raise happens before any write.
    """
    seen: list[str] = []
    for lbl in labels:
        if lbl is None or not lbl.strip():
            raise ValueError(
                f"invalid label {lbl!r}: labels must be non-empty, "
                f"non-whitespace strings"
            )
        if ";" in lbl:
            raise ValueError(
                f"invalid label {lbl!r}: Azure DevOps uses ';' as the "
                f"System.Tags separator, so labels cannot contain ';'"
            )
        if lbl not in seen:
            seen.append(lbl)
    return "; ".join(sorted(seen))


def _identity_display_name(field: dict | str | None) -> str:
    """Pull a display name out of an ADO `IdentityRef` payload.

    ADO's identity fields can be either a string (legacy short form,
    `"Display Name <user@example.com>"`) or a dict with `displayName`,
    `uniqueName`, `id`. Returns the most user-friendly form available.
    """
    if not field:
        return ""
    if isinstance(field, str):
        m = re.match(r"^(.*?)\s*<([^>]+)>\s*$", field)
        return m.group(1).strip() if m else field
    if isinstance(field, dict):
        return (
            field.get("displayName")
            or field.get("uniqueName")
            or field.get("id")
            or ""
        )
    return ""


def _identity_login_or_display(field: dict | str | None) -> str:
    """Pull a login-shaped identifier out of an ADO `IdentityRef`.

    Used where the GitHub provider would surface `user.login` — for
    Azure that's whichever email-style field the payload exposes.
    ADO uses inconsistent field names across endpoints:
      - WorkItem / PR identity refs:  `uniqueName`
      - `connectionData.authenticatedUser`:  `principalName` /
        `mailAddress` (no `uniqueName`)
      - Reviewer payloads:  sometimes `uniqueName`, sometimes just `id`

    Try the email-shaped fields first, then displayName, then id —
    so the returned string is never empty when ADO returned any
    identity payload at all, and never a bare GUID when a human-
    readable login exists.
    """
    if not field:
        return ""
    if isinstance(field, str):
        m = re.match(r"^(.*?)\s*<([^>]+)>\s*$", field)
        return m.group(2).strip() if m else field
    if isinstance(field, dict):
        return (
            field.get("uniqueName")
            or field.get("mailAddress")
            or field.get("principalName")
            or field.get("displayName")
            or field.get("id")
            or ""
        )
    return ""


def _open_state_categories() -> frozenset[str]:
    """State categories that count as 'still open' for ListStatus filtering."""
    return frozenset({"Proposed", "InProgress", "Resolved"})


def _closed_state_categories() -> frozenset[str]:
    return frozenset({"Completed", "Removed"})


def _default_open_state(states: list[dict]) -> str:
    """Pick the state name to use when a ticket should be (re)opened.

    Picks the first state whose ``category`` is one of the "still open"
    categories (`_open_state_categories`); if none qualify, falls back
    to the first state's name; if the work-item type has no states at
    all, falls back to the literal `"New"` (ADO's universal starting
    state). Shared by `list_statuses`'s `default_open` hint and
    `remove_relation`'s duplicate_of reopen so the two call sites can't
    drift apart.
    """
    for s in states:
        if s.get("category") in _open_state_categories():
            return s.get("name") or "New"
    if states:
        return states[0].get("name") or "New"
    return "New"


def _build_work_item_url(project: ProjectConfig, work_item_id: int | str) -> str:
    base = (project.base_url or "https://dev.azure.com").rstrip("/")
    org, proj = project.organization, project.ado_project
    return f"{base}/{org}/{proj}/_workitems/edit/{work_item_id}"


def _build_pr_url(project: ProjectConfig, pr_id: int | str) -> str:
    base = (project.base_url or "https://dev.azure.com").rstrip("/")
    org, proj, repo = (
        project.organization,
        project.ado_project,
        project.repository,
    )
    return f"{base}/{org}/{proj}/_git/{repo}/pullrequest/{pr_id}"


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


def _map_work_item(raw: dict, project: ProjectConfig) -> Ticket:
    """Translate a work-item REST payload into a `Ticket`."""
    fields = raw.get("fields") or {}
    state = fields.get("System.State") or "open"
    labels = _label_list_from_tags(fields.get("System.Tags"))
    assignees: list[str] = []
    assigned_to = fields.get("System.AssignedTo")
    if assigned_to:
        name = _identity_display_name(assigned_to)
        if name:
            assignees.append(name)
    raw_id = raw.get("id")
    return Ticket(
        id=str(raw_id),
        title=fields.get("System.Title") or "",
        body=_html_to_markdown(fields.get("System.Description")),
        acceptance_criteria=_html_to_markdown(
            fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
        ),
        status=state,
        author=_identity_display_name(fields.get("System.CreatedBy")),
        assignees=assignees,
        labels=labels,
        url=_build_work_item_url(project, raw_id) if raw_id is not None else "",
        created_at=normalize_timestamp(fields.get("System.CreatedDate") or ""),
        updated_at=normalize_timestamp(fields.get("System.ChangedDate") or ""),
        milestone=fields.get("System.IterationPath") or None,
    )


def _map_work_item_comment(raw: dict, project: ProjectConfig, work_item_id: str) -> Comment:
    """Translate an entry from `/_apis/wit/workItems/{id}/comments` into Comment."""
    return Comment(
        id=str(raw.get("id", "")),
        author=_identity_display_name(raw.get("createdBy")),
        body=_html_to_markdown(raw.get("text") or ""),
        url=_build_work_item_url(project, work_item_id) + f"?commentId={raw.get('id', '')}",
        created_at=normalize_timestamp(raw.get("createdDate") or ""),
        updated_at=normalize_timestamp(raw.get("modifiedDate") or ""),
    )


def _map_thread_comment_for_review(
    thread: dict,
    raw: dict,
    project: ProjectConfig,
    pr_id: str,
) -> ReviewComment:
    """Translate one comment inside a PR thread into a `ReviewComment`.

    Threads carry `threadContext` (file + line span) and a list of
    `comments`. Top-of-thread comments are their own discussion anchor;
    replies (`parentCommentId != 0`) point at the first comment in the
    thread.
    """
    ctx = thread.get("threadContext") or {}
    file_path = ctx.get("filePath")
    right_start = ctx.get("rightFileStart") or {}
    left_start = ctx.get("leftFileStart") or {}
    if right_start:
        line = right_start.get("line")
        side = "RIGHT"
    elif left_start:
        line = left_start.get("line")
        side = "LEFT"
    else:
        line = None
        side = None

    # `original_line` reflects the line as anchored on the original
    # iteration of the PR — when ADO has reattached the thread across
    # rebases, `trackingCriteria.origRightFileStart/origLeftFileStart`
    # carries the pre-reattachment line. For fresh threads tracking is
    # absent and we fall back to the current line (matching GitHub's
    # behavior on a brand-new review comment).
    tracking = (thread.get("pullRequestThreadContext") or {}).get(
        "trackingCriteria"
    ) or {}
    if side == "RIGHT":
        orig_anchor = tracking.get("origRightFileStart") or right_start
    elif side == "LEFT":
        orig_anchor = tracking.get("origLeftFileStart") or left_start
    else:
        orig_anchor = {}
    original_line = (
        orig_anchor.get("line") if isinstance(orig_anchor, dict) else None
    )

    comment_id = raw.get("id") or 0
    parent_id = raw.get("parentCommentId") or 0
    thread_id = thread.get("id")
    # The first comment in a thread has id 1 (per ADO docs). The
    # discussion anchor — what callers pass back as in_reply_to — is the
    # thread id itself, which is consistent across all comments in the
    # same thread.
    discussion_id = str(thread_id) if thread_id is not None else None
    in_reply_to = str(thread_id) if parent_id else None

    # `commit_sha` is persisted as a thread-level property
    # (`REVIEW_COMMIT_SHA_PROPERTY_KEY`) by `add_pr_review_comment`'s
    # new-thread branch (ticket #175): a subsequent `get_pr`/
    # `list_pr_review_comments` re-read goes through this function and
    # reads that property back via `_thread_property_value`, which
    # handles both the `{"$value": ...}` envelope and flat shapes ADO
    # returns inconsistently. Threads created before this fix (or
    # created without a caller-supplied `commit_sha`) carry no such
    # property and resolve to `None`, same as before. The property is
    # stored at the thread level, not per-comment, so a reply
    # (`in_reply_to`) inherits whatever `commit_sha` is stored on its
    # parent thread rather than carrying its own.
    commit_sha = _thread_property_value(thread, REVIEW_COMMIT_SHA_PROPERTY_KEY)

    return ReviewComment(
        id=_format_thread_comment_id(thread_id, comment_id),
        author=_identity_login_or_display(raw.get("author")),
        body=_html_to_markdown(raw.get("content") or ""),
        path=file_path,
        line=line,
        original_line=original_line,
        side=side,
        commit_sha=commit_sha,
        in_reply_to=in_reply_to,
        created_at=normalize_timestamp(raw.get("publishedDate") or ""),
        updated_at=normalize_timestamp(raw.get("lastUpdatedDate") or ""),
        url=_build_pr_url(project, pr_id) + f"?discussionId={thread_id}",
        discussion_id=discussion_id,
    )


def _map_thread_comment(
    thread: dict,
    raw: dict,
    project: ProjectConfig,
    pr_id: str,
) -> Comment:
    """Translate a top-of-thread comment (no file context) into a `Comment`."""
    thread_id = thread.get("id")
    comment_id = raw.get("id") or 0
    return Comment(
        id=_format_thread_comment_id(thread_id, comment_id),
        author=_identity_login_or_display(raw.get("author")),
        body=_html_to_markdown(raw.get("content") or ""),
        url=_build_pr_url(project, pr_id) + f"?discussionId={thread_id}",
        created_at=normalize_timestamp(raw.get("publishedDate") or ""),
        updated_at=normalize_timestamp(raw.get("lastUpdatedDate") or ""),
    )


def _format_thread_comment_id(thread_id: int | str | None, comment_id: int | str) -> str:
    """Build a PR-comment id matching GitHub's `id == discussion_id`
    invariant for top-of-thread comments.

    - top-of-thread (`comment_id == 1`): bare `thread_id` so callers
      can round-trip the id as `in_reply_to` without surprises.
    - replies (`comment_id > 1`): `f"{thread_id}.{comment_id}"`.

    The composite form is intentionally non-numeric so callers don't
    confuse it with a GitHub-style flat id.
    """
    if thread_id is None:
        return str(comment_id or "")
    try:
        cid_int = int(comment_id)
    except (TypeError, ValueError):
        cid_int = 0
    if cid_int <= 1:
        return str(thread_id)
    return f"{thread_id}.{cid_int}"


REVIEW_BODY_PROPERTY_KEY = "projectIssues.kind"
REVIEW_BODY_PROPERTY_VALUE = "review_body"
# Thread-level property carrying a caller-supplied `commit_sha` for a new
# diff-anchored review-comment thread (ticket #175) — mirrors
# `REVIEW_BODY_PROPERTY_KEY`'s envelope shape so it round-trips through the
# same `_thread_property_value` reader.
REVIEW_COMMIT_SHA_PROPERTY_KEY = "projectIssues.commitSha"

# Work-item and comment ids are .NET Int32; anything beyond
# `2_147_483_647` triggers an opaque ADO 400 (System.OverflowException).
# Reject those at the provider entry-point so the agent gets a clean
# "kind 'project#id' not found" via `_safe` instead of the raw 400.
_ADO_INT32_MAX = 2_147_483_647


def _validate_int32_id(raw: str | int, kind: str) -> None:
    """Raise `LookupError` when `raw` is a numeric id beyond Int32 range.

    `kind` is a short noun ("comment", "ticket", "work item") used in
    the error message so the agent sees which surface rejected the id.
    Non-numeric input passes through unchanged — ADO will surface its
    own (already-curated by `_check`) error for those.
    """
    if isinstance(raw, int):
        candidate = raw
    elif isinstance(raw, str):
        stripped = raw.strip().lstrip("#")
        if not stripped.isdigit():
            return
        candidate = int(stripped)
    else:
        return
    if candidate > _ADO_INT32_MAX:
        raise LookupError(
            f"{kind} '{raw}' not found — id exceeds the Azure DevOps "
            f"32-bit integer range (max {_ADO_INT32_MAX})"
        )


def _thread_property_value(thread: dict, key: str) -> str | None:
    """Read a thread `properties.{key}` value, handling both envelope and
    flat shapes ADO returns inconsistently:
      - envelope: `{key: {"$type": "System.String", "$value": "x"}}`
      - flat:     `{key: "x"}`
    """
    props = thread.get("properties") or {}
    entry = props.get(key)
    if entry is None:
        return None
    if isinstance(entry, dict):
        v = entry.get("$value")
        return v if isinstance(v, str) else None
    if isinstance(entry, str):
        return entry
    return None


def _is_review_body_thread(thread: dict) -> bool:
    """True iff this thread was posted as the body of a `submit_pr_review`."""
    return (
        _thread_property_value(thread, REVIEW_BODY_PROPERTY_KEY)
        == REVIEW_BODY_PROPERTY_VALUE
    )


def _utc_iso_now() -> str:
    """Return the current UTC time as an ISO-8601 `Z`-suffixed string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_thread_id_from_alias(value: str | None) -> str | None:
    """Pull the thread-id prefix out of a comment id alias.

    Accepts both the canonical `thread_id` form and the legacy
    `f"{thread_id}.{comment_id}"` composite. Used when a caller passes
    a comment id back as `in_reply_to` — we only need the thread part
    to address the ADO API.
    """
    if not value:
        return None
    head = value.split(".", 1)[0]
    return head or None


def _map_pr(raw: dict, project: ProjectConfig) -> PullRequest:
    """Translate `/_apis/git/repositories/{repo}/pullrequests/{id}` into PR."""
    status_raw = raw.get("status")
    merged = bool(raw.get("mergeStatus") == "succeeded" and status_raw == "completed")
    if status_raw == "active":
        status: str = "open"
    elif merged:
        status = "merged"
    elif status_raw == "abandoned":
        status = "closed"
    else:
        # `completed` without successful merge (rare) or unknown — best
        # effort: treat as closed.
        status = "closed"

    source_ref = (raw.get("sourceRefName") or "").removeprefix("refs/heads/")
    target_ref = (raw.get("targetRefName") or "").removeprefix("refs/heads/")
    # `repo_full_name` mirrors GitHub's `org/repo` shape — on ADO the
    # canonical 3-segment identifier is `org/project/repo`, matching the
    # YAML `path` field. Fall back to the embedded repo name when any
    # segment is missing so the field is never empty.
    repo_name = (raw.get("repository") or {}).get("name", "") or ""
    org_seg = project.organization or ""
    proj_seg = project.ado_project or ""
    repo_seg = project.repository or repo_name
    if org_seg and proj_seg and repo_seg:
        repo_full_name = f"{org_seg}/{proj_seg}/{repo_seg}"
    else:
        repo_full_name = repo_name
    head = {
        "ref": source_ref,
        "sha": raw.get("lastMergeSourceCommit", {}).get("commitId", "") or "",
        "repo_full_name": repo_full_name,
    }
    base_ref_dict = {
        "ref": target_ref,
        "sha": raw.get("lastMergeTargetCommit", {}).get("commitId", "") or "",
    }

    reviewers: list[str] = []
    requested: list[str] = []
    for r in raw.get("reviewers") or []:
        name = _identity_display_name(r)
        if not name:
            continue
        vote = r.get("vote", 0)
        # vote != 0 means the reviewer cast a vote, so they "reviewed"
        # in our sense. vote 0 = no action yet = still requested.
        if vote:
            reviewers.append(name)
        else:
            requested.append(name)

    labels = sorted(
        (lbl.get("name") or "")
        for lbl in raw.get("labels") or []
        if lbl.get("name")
    )

    pr_id = raw.get("pullRequestId") or 0
    return PullRequest(
        id=str(pr_id),
        number=int(pr_id) if pr_id else 0,
        title=raw.get("title") or "",
        body=_html_to_markdown(raw.get("description") or ""),
        status=status,
        draft=bool(raw.get("isDraft")),
        author=_identity_display_name(raw.get("createdBy")),
        assignees=[],
        reviewers=reviewers,
        requested_reviewers=requested,
        labels=labels,
        head=head,
        base=base_ref_dict,
        merged=merged,
        mergeable=None
        if raw.get("mergeStatus") in (None, "queued", "notSet")
        else raw.get("mergeStatus") in ("succeeded", "conflicts"),
        url=_build_pr_url(project, pr_id) if pr_id else "",
        created_at=normalize_timestamp(raw.get("creationDate") or ""),
        updated_at=normalize_timestamp(
            raw.get("closedDate") or raw.get("creationDate") or ""
        ),
        merge_commit_sha=(raw.get("lastMergeCommit") or {}).get("commitId"),
    )


def _map_build_run(raw: dict, project: ProjectConfig) -> PipelineRun:
    """Translate an entry from `/_apis/build/builds` into a `PipelineRun`."""
    status = raw.get("status") or ""
    result = raw.get("result")
    if status == "completed":
        if result == "succeeded":
            conclusion: str | None = "success"
        elif result in ("failed", "partiallySucceeded"):
            conclusion = "failure"
        elif result == "canceled":
            conclusion = "cancelled"
        else:
            conclusion = result
    else:
        conclusion = None

    branch = (raw.get("sourceBranch") or "").removeprefix("refs/heads/")

    return PipelineRun(
        id=str(raw.get("id", "")),
        name=raw.get("definition", {}).get("name") or "",
        branch=branch,
        head_sha=raw.get("sourceVersion") or "",
        event=raw.get("reason") or "",
        status=status or "",
        conclusion=conclusion,
        url=(raw.get("_links") or {}).get("web", {}).get("href")
        or f"{(project.base_url or 'https://dev.azure.com').rstrip('/')}"
        f"/{project.organization}/{project.ado_project}/_build/results?buildId={raw.get('id')}",
        created_at=normalize_timestamp(raw.get("queueTime") or ""),
        updated_at=normalize_timestamp(raw.get("finishTime") or raw.get("queueTime") or ""),
        run_attempt=int(raw.get("retainedByRelease", 0) or 0) + 1,
    )


def _normalize_az_issues(rec: dict) -> list[FailureAnnotation]:
    """Map an Azure Pipelines timeline record's `issues[]` into
    `FailureAnnotation`s (ticket #152).

    Each timeline record for a failed Job/Task carries an `issues` array
    of `{"type": "error"|"warning", "message": "...", "data": {...}}`
    entries — real structured failure data that was previously fetched
    and discarded. `rec.get("name")` (the job/task name) becomes every
    mapped annotation's `step`.

    `data` carries the source location under provider-inconsistent
    casing (`sourcePath`/`sourcepath`, `lineNumber`/`linenumber`, ...);
    keys are matched case-insensitively. `line` is coerced to `int` and
    left `None` when absent or non-numeric.

    Handles a missing/empty `issues` key gracefully (returns `[]`).
    """
    step = rec.get("name") or ""
    out: list[FailureAnnotation] = []
    for issue in rec.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        data = issue.get("data") or {}
        file_val: str | None = None
        line_val: int | None = None
        if isinstance(data, dict):
            for key, value in data.items():
                key_lower = key.lower() if isinstance(key, str) else ""
                if key_lower == "sourcepath" and file_val is None:
                    file_val = value
                elif key_lower == "linenumber" and line_val is None:
                    try:
                        line_val = int(value)
                    except (TypeError, ValueError):
                        line_val = None
        out.append(
            FailureAnnotation(
                step=step,
                message=issue.get("message") or "",
                file=file_val,
                line=line_val,
                severity=issue.get("type"),
            )
        )
    return out


# ---------- relation kind mapping -------------------------------------------


_RELATION_FORWARD: dict[str, str] = {
    "parent": "System.LinkTypes.Hierarchy-Reverse",
    "child": "System.LinkTypes.Hierarchy-Forward",
    "blocks": "System.LinkTypes.Dependency-Forward",
    "blocked_by": "System.LinkTypes.Dependency-Reverse",
    "duplicate_of": "System.LinkTypes.Duplicate-Forward",
    "relates_to": "System.LinkTypes.Related",
}

# Write-direction table for add_relation/remove_relation (ticket #171).
# `_RELATION_FORWARD` above is the *read* basis: it defines how a native
# ADO link type maps back to our generic kind when hydrating relations
# from a work item (`_RELATION_REVERSE` / `_ado_rel_to_kind`), and must
# stay untouched. On write, parent/child are intentionally the inverse of
# that read mapping: writing "parent" places the target as this item's
# child (System.LinkTypes.Hierarchy-Forward is the link type ADO uses to
# express "the target is my child"), which is the mirror image of how the
# read table interprets an existing native Hierarchy-Forward link as
# "child". blocks/blocked_by/duplicate_of/relates_to are symmetric and
# unaffected.
_RELATION_WRITE: dict[str, str] = {
    **_RELATION_FORWARD,
    "parent": "System.LinkTypes.Hierarchy-Forward",
    "child": "System.LinkTypes.Hierarchy-Reverse",
}

_RELATION_REVERSE: dict[str, str] = {v: k for k, v in _RELATION_FORWARD.items()}

# Pairs where the inverse relation kind is what the *other* work item
# would see when we add this kind on the source. Used to surface
# `child` when the API reports `parent` etc.
_RELATION_INVERSE: dict[str, str] = {
    "parent": "child",
    "child": "parent",
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "duplicate_of": "duplicated_by",
    "duplicated_by": "duplicate_of",
}

SUPPORTED_RELATION_KINDS: tuple[str, ...] = (
    "parent",
    "child",
    "blocks",
    "blocked_by",
    "duplicate_of",
    "relates_to",
)


def _ado_rel_to_kind(rel: str) -> str | None:
    """Translate an ADO `System.LinkTypes.*` string to our generic kind."""
    return _RELATION_REVERSE.get(rel)


def _resolve_ado_branch(
    client: "httpx.Client",
    project: "ProjectConfig",
    repo_id: str,
    branch: str,
) -> bool:
    """Return ``True`` if `branch` exists in `repo_id`, ``False`` otherwise.

    Uses the refs filter endpoint; non-success responses are treated as
    ``False`` (best-effort — we do not want a secondary auth failure to
    shadow the primary result).
    """
    # ADO refs filter: `heads/<branch>` (without `refs/` prefix)
    branch_ref = branch.removeprefix("refs/heads/")
    path = (
        f"{_project_scope(project)}/_apis/git/repositories"
        f"/{repo_id}/refs"
    )
    try:
        resp = client.get(
            path,
            params=_api_version_params({"filter": f"heads/{branch_ref}"}),
        )
        if not resp.is_success:
            return False
        return (resp.json() or {}).get("count", 0) > 0
    except Exception:  # noqa: BLE001
        return False


def _resolve_ado_commit(
    client: "httpx.Client",
    project: "ProjectConfig",
    repo_id: str,
    sha: str,
) -> bool:
    """Return ``True`` if commit `sha` exists in `repo_id`, ``False`` otherwise.

    Best-effort — non-success responses (including a genuine 404) are
    treated as ``False`` rather than raised, so a secondary lookup failure
    never shadows the primary result.
    """
    path = (
        f"{_project_scope(project)}/_apis/git/repositories"
        f"/{repo_id}/commits/{sha}"
    )
    try:
        resp = client.get(path, params=_api_version_params())
        return resp.is_success
    except Exception:  # noqa: BLE001
        return False


def _resolve_ado_tag(
    client: "httpx.Client",
    project: "ProjectConfig",
    repo_id: str,
    tag: str,
) -> bool:
    """Return ``True`` if tag `tag` exists in `repo_id`, ``False`` otherwise.

    Uses the same refs filter endpoint as `_resolve_ado_branch`, filtering
    on `tags/<name>` instead of `heads/<name>`; non-success responses are
    treated as ``False`` (best-effort).
    """
    tag_ref = tag.removeprefix("refs/tags/")
    path = (
        f"{_project_scope(project)}/_apis/git/repositories"
        f"/{repo_id}/refs"
    )
    try:
        resp = client.get(
            path,
            params=_api_version_params({"filter": f"tags/{tag_ref}"}),
        )
        if not resp.is_success:
            return False
        return (resp.json() or {}).get("count", 0) > 0
    except Exception:  # noqa: BLE001
        return False


# ---------- the provider class ----------------------------------------------


class AzureDevOpsProvider(TokenCapabilityProvider, TokenProjectDiscoveryProvider):
    """Azure DevOps provider.

    Implements the same surface as `GitHubProvider` and `GitLabProvider`
    (see `providers/base.py`). Module-level helpers carry the per-request
    plumbing; the class is a thin facade so the cache state survives
    across calls (the tool layer instantiates one `_PROVIDERS["azuredevops"]`
    at import time).
    """

    # ---------- shared scaffolding -----------------------------------------

    def _list_work_item_types(
        self, project: ProjectConfig, token: str | None
    ) -> list[dict]:
        path = f"{_project_scope(project)}/_apis/wit/workitemtypes"
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        return list((resp.json().get("value") or []))

    def _default_work_item_type(
        self, project: ProjectConfig, token: str | None
    ) -> str:
        """Return the work-item type used for `create_ticket`.

        Resolution order:
          1. `project.default_work_item_type` if set.
          2. Cached per-project lookup.
          3. First match in a fixed priority list against the project's
             `/workitemtypes` listing.
        """
        if project.default_work_item_type:
            return project.default_work_item_type
        key = (project.organization or "", project.ado_project or "")
        cached = _cache_get(_default_type_cache, key)
        if cached:
            return cached
        types = [t.get("name") for t in self._list_work_item_types(project, token)]
        for candidate in (
            "Issue",
            "Bug",
            "User Story",
            "Product Backlog Item",
            "Requirement",
        ):
            if candidate in types:
                _cache_put(_default_type_cache, key, candidate)
                return candidate
        # Last resort — surface whatever the project has so the user gets
        # a useful error from the create call instead of a silent default.
        if types:
            picked = types[0]
            _cache_put(_default_type_cache, key, picked)
            return picked
        raise AzureDevOpsError(
            0,
            f"project '{project.id}' has no work-item types available",
        )

    def _states_for_type(
        self,
        project: ProjectConfig,
        token: str | None,
        work_item_type: str,
    ) -> list[dict]:
        """List the `{name, category, color}` entries for a work-item type."""
        key = (project.organization or "", project.ado_project or "", work_item_type)
        cached = _cache_get(_state_cache, key)
        if cached is not None:
            return cached
        path = (
            f"{_project_scope(project)}/_apis/wit/workitemtypes/"
            f"{quote(work_item_type, safe='')}/states"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        states = list((resp.json().get("value") or []))
        _cache_put(_state_cache, key, states)
        return states

    def _fields_for_type(
        self,
        project: ProjectConfig,
        token: str | None,
        work_item_type: str,
    ) -> list[dict]:
        """List the field descriptors for a work-item type.

        Each entry is the raw ADO field dict with at least `referenceName`,
        `name`, `isReadOnly`, `alwaysRequired`, and optionally
        `allowedValues` (a list of strings for picklist fields). Despite
        what the ADO docs imply, this endpoint's response model does NOT
        include a `type` key — field types must be sourced separately via
        `_field_types_for_project`. Results are cached per
        `(org, project, work_item_type)` for 1 hour.
        """
        key = (project.organization or "", project.ado_project or "", work_item_type)
        cached = _cache_get(_field_cache, key)
        if cached is not None:
            return cached
        path = (
            f"{_project_scope(project)}/_apis/wit/workitemtypes/"
            f"{quote(work_item_type, safe='')}/fields"
        )
        with _client(project, token) as c:
            resp = c.get(
                path,
                params={**_api_version_params(), "$expand": "allowedValues"},
            )
        _check(resp)
        fields = list((resp.json().get("value") or []))
        _cache_put(_field_cache, key, fields)
        return fields

    def _field_types_for_project(
        self,
        project: ProjectConfig,
        token: str | None,
    ) -> dict[str, str]:
        """Return a `referenceName -> type` map for every field ADO knows
        about in this project.

        The bulk `.../workitemtypes/{type}/fields` endpoint used by
        `_fields_for_type` never includes a `type` key in its response
        model, so field types must be sourced separately from the global
        "Fields - List" endpoint (`.../_apis/wit/fields`). Project-scoped
        (not org-scoped) so project-level custom fields are included.
        A field missing from the registry simply has no entry — callers
        should treat that as "unknown" (empty string) rather than an
        error. Results are cached per `(org, project)` for 1 hour.
        """
        key = (project.organization or "", project.ado_project or "")
        cached = _cache_get(_field_type_cache, key)
        if cached is not None:
            return cached
        path = f"{_project_scope(project)}/_apis/wit/fields"
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        types: dict[str, str] = {}
        for f in resp.json().get("value") or []:
            reference_name = f.get("referenceName")
            if reference_name:
                types[reference_name] = f.get("type") or ""
        _cache_put(_field_type_cache, key, types)
        return types

    def _classification_node_paths(
        self,
        project: ProjectConfig,
        token: str | None,
        structure: str,
    ) -> list[str]:
        """Return every Area/Iteration path in *structure*'s classification tree.

        ``structure`` is ``"Areas"`` or ``"Iterations"``. ADO's raw node
        `path` is ``\\Project\\Area\\Child`` (or ``\\Project\\Iteration\\Child``)
        — the second segment is a synthetic structure label, not part of a
        valid `System.AreaPath` / `System.IterationPath` value (real values
        are ``Project`` and ``Project\\Child``). Each returned entry is the
        node's `path` with the leading backslash stripped and that synthetic
        segment removed (e.g. ``MyProject\\Team\\SubArea``), matching the
        value ADO actually accepts as a filter. The root project node is
        included — it is itself a valid area/iteration path. Results are
        cached per `(org, project, structure)` for 1 hour.
        """
        key = (project.organization or "", project.ado_project or "", structure)
        cached = _cache_get(_classification_cache, key)
        if cached is not None:
            return cached
        path = (
            f"{_project_scope(project)}/_apis/wit/classificationnodes/"
            f"{quote(structure, safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(
                path,
                params=_api_version_params({"$depth": _CLASSIFICATION_DEPTH}),
            )
        _check(resp)
        root = resp.json()

        paths: list[str] = []
        structure_label = _CLASSIFICATION_STRUCTURE_LABEL.get(structure)

        def _walk(node: dict) -> None:
            raw_path = node.get("path") or ""
            segments = raw_path.lstrip("\\").split("\\")
            # Drop the synthetic structure-label segment (index 1) —
            # name-gated so a genuine sub-area/iteration literally named
            # "Area"/"Iteration" is only stripped at that one synthetic
            # position, never elsewhere in the path.
            if len(segments) > 1 and segments[1] == structure_label:
                del segments[1]
            paths.append("\\".join(segments))
            for child in node.get("children") or []:
                _walk(child)

        _walk(root)
        _cache_put(_classification_cache, key, paths)
        return paths

    def _field_allowed_values(
        self,
        project: ProjectConfig,
        token: str | None,
        work_item_type: str,
        reference_name: str,
    ) -> list[str] | None:
        """Fetch `allowedValues` for a single field via the per-field endpoint.

        Some picklist fields come back without `allowedValues` from the bulk
        `.../fields?$expand=allowedValues` call; fetching the field
        individually with the same `$expand` fills them in. Returns `None`
        when the field has no allowed values. Cached per
        `(org, project, work_item_type, reference_name)` for 1 hour.
        """
        key = (
            project.organization or "",
            project.ado_project or "",
            work_item_type,
            reference_name,
        )
        cached = _cache_get(_field_values_cache, key)
        if cached is not None:
            return cached
        path = (
            f"{_project_scope(project)}/_apis/wit/workitemtypes/"
            f"{quote(work_item_type, safe='')}/fields/"
            f"{quote(reference_name, safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(
                path,
                params={**_api_version_params(), "$expand": "allowedValues"},
            )
        _check(resp)
        raw_allowed = resp.json().get("allowedValues")
        allowed = list(raw_allowed) if raw_allowed else None
        _cache_put(_field_values_cache, key, allowed)
        return allowed

    def _resolve_repository_id(
        self,
        project: ProjectConfig,
        token: str | None,
    ) -> str:
        """Resolve `project.repository` (a name) to the ADO repo GUID."""
        repo_name = project.repository
        if not repo_name:
            raise AzureDevOpsError(
                0,
                f"project '{project.id}': missing repository in path "
                f"{project.path!r}",
            )
        key = (
            project.organization or "",
            project.ado_project or "",
            repo_name,
        )
        cached = _cache_get(_repo_id_cache, key)
        if cached:
            return cached
        path = f"{_project_scope(project)}/_apis/git/repositories"
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        for repo in resp.json().get("value") or []:
            if (repo.get("name") or "").lower() == repo_name.lower():
                repo_id = repo.get("id") or ""
                if repo_id:
                    _cache_put(_repo_id_cache, key, repo_id)
                    return repo_id
        raise AzureDevOpsError(
            404,
            f"repository '{repo_name}' not found in project "
            f"'{project.organization}/{project.ado_project}'",
        )

    # ---------- list_statuses ----------------------------------------------

    def list_statuses(
        self, project: ProjectConfig, token: str | None
    ) -> StatusSpec:
        wi_type = self._default_work_item_type(project, token)
        states = self._states_for_type(project, token, wi_type)
        values = [s.get("name") for s in states if s.get("name")]
        by_cat: dict[str, list[str]] = {}
        for s in states:
            cat = s.get("category") or "Proposed"
            by_cat.setdefault(cat, []).append(s.get("name") or "")

        # transitions: ADO doesn't expose state-rule legal transitions
        # through public REST. Report the full graph as legal and let the
        # API reject invalid moves.
        transitions = {v: [other for other in values if other != v] for v in values}

        # default_open: first non-terminal state, falling back to the
        # first value if every state is terminal (see `_default_open_state`).
        default_open = _default_open_state(states)

        # Process templates without a "Removed" category (notably Basic)
        # don't have a distinct declined state — surface that honestly as
        # None rather than collapsing it onto terminal_completed, which
        # previously left both fields equal and agents thinking they had
        # two interchangeable terminal states to choose from.
        terminal = (by_cat.get("Completed") or []) + (by_cat.get("Removed") or [])
        terminal_completed = by_cat.get("Completed") or []
        terminal_declined = by_cat.get("Removed") or []
        hints: dict[str, str | list[str] | None] = {
            "default_open": default_open,
            "terminal": terminal,
            "terminal_completed": terminal_completed[0] if terminal_completed else None,
            "terminal_declined": terminal_declined[0] if terminal_declined else None,
        }
        return StatusSpec(values=values, transitions=transitions, hints=hints)

    # ---------- list_fields -----------------------------------------------

    def list_fields(
        self,
        project: ProjectConfig,
        token: str | None,
        *,
        work_item_type: str | None = None,
    ) -> list[FieldSpec]:
        """Return the field descriptors for a work-item type.

        Each `FieldSpec` describes one field accepted by the given
        work-item type: its reference name, display name, data type,
        optional picklist of allowed values, and read-only /
        always-required flags.

        When `work_item_type` is ``None`` the default work-item type for
        the project is resolved via `_default_work_item_type` (the same
        resolution used by `create_ticket` and `list_statuses`). Pass an
        explicit value to skip that lookup and avoid the extra HTTP call.
        """
        wi_type = work_item_type or self._default_work_item_type(project, token)
        raw_fields = self._fields_for_type(project, token, wi_type)
        type_map = self._field_types_for_project(project, token)
        result: list[FieldSpec] = []
        for f in raw_fields:
            reference_name = f.get("referenceName") or ""
            field_type = f.get("type") or type_map.get(reference_name, "")
            raw_allowed = f.get("allowedValues")
            allowed: list[str] | None = (
                list(raw_allowed) if raw_allowed else None
            )
            if reference_name == "System.AreaPath":
                allowed = self._classification_node_paths(project, token, "Areas")
            elif reference_name == "System.IterationPath":
                allowed = self._classification_node_paths(
                    project, token, "Iterations"
                )
            elif allowed is None and field_type.startswith("picklist"):
                allowed = self._field_allowed_values(
                    project, token, wi_type, reference_name
                )
            result.append(
                FieldSpec(
                    reference_name=reference_name,
                    display_name=f.get("name") or "",
                    type=field_type,
                    allowed_values=allowed,
                    read_only=bool(f.get("isReadOnly")),
                    always_required=bool(f.get("alwaysRequired")),
                )
            )
        return result

    # ---------- tickets — read --------------------------------------------

    def _build_wiql(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> str:
        clauses: list[str] = ["[System.TeamProject] = @project"]
        if filters.states:
            # Provider-native state values take precedence over `status`
            # entirely (including `status == "any"`) — validate each
            # requested value against the discovery vocabulary and emit
            # an exact-match IN clause. No casing/whitespace normalization.
            wi_type = self._default_work_item_type(project, token)
            states = self._states_for_type(project, token, wi_type)
            valid_names = [s.get("name") for s in states if s.get("name")]
            for value in filters.states:
                if value not in valid_names:
                    raise ValueError(
                        f"unsupported status {value!r} for Azure DevOps — "
                        f"use list_ticket_statuses to discover valid values. "
                        f"Accepted: {', '.join(valid_names)}."
                    )
            in_list = ", ".join(f"'{_escape_wiql(v)}'" for v in filters.states)
            clauses.append(f"[System.State] IN ({in_list})")
        elif filters.status != "any":
            # Map ListStatus to state-category sets discovered for the
            # default work-item type. We can't filter by category in
            # WIQL directly, but we can enumerate the states.
            try:
                wi_type = self._default_work_item_type(project, token)
                states = self._states_for_type(project, token, wi_type)
            except Exception:  # noqa: BLE001 - fall back to no-state filter
                states = []
            allowed = _open_state_categories() if filters.status == "open" else _closed_state_categories()
            names = [s.get("name") for s in states if (s.get("category") in allowed)]
            if names:
                in_list = ", ".join(f"'{_escape_wiql(n)}'" for n in names)
                clauses.append(f"[System.State] IN ({in_list})")
            elif filters.status == "open":
                clauses.append("[System.State] <> 'Done' AND [System.State] <> 'Closed' AND [System.State] <> 'Removed'")
            else:
                clauses.append("([System.State] = 'Done' OR [System.State] = 'Closed' OR [System.State] = 'Removed')")

        wi_type = (
            project.default_work_item_type
            if project.default_work_item_type
            else None
        )
        if wi_type:
            clauses.append(f"[System.WorkItemType] = '{_escape_wiql(wi_type)}'")

        if filters.labels:
            for lbl in filters.labels:
                clauses.append(f"[System.Tags] CONTAINS '{_escape_wiql(lbl)}'")
        if filters.not_labels:
            for lbl in filters.not_labels:
                clauses.append(f"[System.Tags] NOT CONTAINS '{_escape_wiql(lbl)}'")
        if filters.assignee:
            clauses.append(f"[System.AssignedTo] = '{_escape_wiql(filters.assignee)}'")
        if filters.author:
            clauses.append(f"[System.CreatedBy] = '{_escape_wiql(filters.author)}'")
        if filters.search:
            clauses.append(
                f"([System.Title] CONTAINS WORDS '{_escape_wiql(filters.search)}' "
                f"OR [System.Description] CONTAINS WORDS '{_escape_wiql(filters.search)}')"
            )
        if filters.created_after:
            clauses.append(f"[System.CreatedDate] >= '{_escape_wiql(filters.created_after)}'")
        if filters.created_before:
            clauses.append(f"[System.CreatedDate] <= '{_escape_wiql(filters.created_before)}'")
        if filters.updated_after:
            clauses.append(f"[System.ChangedDate] >= '{_escape_wiql(filters.updated_after)}'")
        if filters.updated_before:
            clauses.append(f"[System.ChangedDate] <= '{_escape_wiql(filters.updated_before)}'")
        # No validation against classification nodes here — that would
        # require an extra API round-trip and is out of scope. ADO's WIQL
        # endpoint 404s (TF51011) for an unrecognised area path rather than
        # returning zero rows; `list_tickets` swallows that specific
        # response into an empty result (see `_is_area_path_not_found`), so
        # the "invalid area path simply yields zero matching work items"
        # contract holds without an extra validation round-trip. The
        # effective area (an explicit `filters.area_path` overriding the
        # config-level `project.area_path` default — ticket #172) is
        # resolved by `_effective_area_path`.
        area, area_recursive = _effective_area_path(project, filters)
        area_clause = _area_path_clause(area, area_recursive)
        if area_clause:
            clauses.append(area_clause)

        if filters.board_column:
            clauses.extend(_board_column_wiql_clauses(project, filters.board_column))

        sort_field = {
            "created": "System.CreatedDate",
            "updated": "System.ChangedDate",
            "comments": "System.CommentCount",
        }.get(filters.sort_by, "System.CreatedDate")
        sort_order = "ASC" if filters.sort_order == "asc" else "DESC"

        return (
            "SELECT [System.Id] FROM workitems WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY [{sort_field}] {sort_order}"
        )

    def list_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> tuple[list[Ticket], bool]:
        """List work items for a project.

        `filters.states`, when non-empty, is matched exact-verbatim
        against `[System.State]` and takes precedence over `filters.status`
        entirely (including `status == "any"`) — see `_build_wiql`.
        Unknown values raise `ValueError` pointing back to
        `list_ticket_statuses`.

        `filters.board_column` (ticket #119) filters on `System.BoardColumn`
        for the Azure Boards board bound to `project.board`. It requires a
        `kind="azure-boards"` binding with `team` and `board` set — Azure
        Boards boards are bound to a team + backlog level, and column
        config is team-scoped. Raises `ValueError` when that context is
        missing (no `project.board`, wrong binding kind, or missing
        `team`/`board`) or when the requested column isn't one of
        `project.board.columns` — never silently ignored. When board
        context isn't configured, use `status` / `states` (matching
        `System.State` directly) as a manual fallback filter instead.
        """
        _validate_limit(filters.limit)
        wiql = self._build_wiql(project, token, filters)
        with _client(project, token) as c:
            resp = c.post(
                f"{_project_scope(project)}/_apis/wit/wiql",
                params=_api_version_params({"$top": max(1, filters.limit)}),
                json={"query": wiql},
            )
        # Honour the documented "invalid area path yields zero matches"
        # contract (ticket #147): ADO's WIQL endpoint 404s (TF51011)
        # instead of returning an empty result set when `area_path`
        # doesn't resolve. Swallow only that specific, gated case — any
        # other 4xx (missing project, malformed query, etc.) still
        # surfaces via `_check` below. Gated on the *effective* area
        # (ticket #172): either an explicit `filters.area_path` or a
        # config-level `project.area_path` default, so an invalid
        # config-level area also yields `[], False` rather than raising.
        effective_area, _ = _effective_area_path(project, filters)
        if effective_area and _is_area_path_not_found(resp):
            return [], False
        _check(resp)
        ids = [
            int(item.get("id"))
            for item in (resp.json().get("workItems") or [])
            if item.get("id") is not None
        ][: max(1, filters.limit)]
        if not ids:
            return [], False
        has_more = len(ids) >= filters.limit
        return self._fetch_work_items_batch(project, token, ids), has_more

    def list_board_columns(
        self, project: ProjectConfig, token: str | None,
    ) -> list[BoardColumnSpec]:
        """Resolve `project.board.columns` against the live Azure Boards
        columns for the bound team + backlog board (ticket #119).

        Azure Boards boards are bound to a **team + backlog level**, not
        the project alone, so this reads
        `GET {team}/_apis/work/boards/{board}/columns` and pairs each
        logical column with its resolved native column name
        (`Board.resolve()`: explicit `map` wins, else case-insensitive
        identity fallback), that live column's `id` (as `option_id`), the
        `System.State` names from its `stateMappings` (as `.states`), and
        whether it's a Doing/Done split column (`.is_split`).

        Raises `ValueError` when: `project.board` is unset; the binding
        isn't `kind="azure-boards"`; the binding is missing `team`/`board`;
        or a resolved native column isn't present among the live board's
        columns.
        """
        board = project.board
        if board is None:
            raise ValueError(
                f"project {project.id!r} has no 'board' configuration — "
                f"add one to projects.yml before calling list_board_columns"
            )
        binding = board.binding
        if binding.kind != "azure-boards":
            raise ValueError(
                f"project {project.id!r} board binding is {binding.kind!r}, "
                f"not 'azure-boards' — list_board_columns is "
                f"Azure-Boards-only"
            )
        if not binding.team or not binding.board:
            raise ValueError(
                f"project {project.id!r} azure-boards binding is missing "
                f"'team' and/or 'board' — both are required to resolve a "
                f"live Azure Boards board (Azure Boards boards are bound "
                f"to a team + backlog level)"
            )
        path = (
            f"{_project_scope(project)}/{quote(binding.team, safe='')}"
            f"/_apis/work/boards/{quote(binding.board, safe='')}/columns"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        live_columns = resp.json().get("value") or []
        by_lower_name = {
            str(col.get("name") or "").lower(): col for col in live_columns
        }
        result: list[BoardColumnSpec] = []
        for col in board.columns:
            native = board.resolve(col)
            live = by_lower_name.get(native.lower())
            if live is None:
                available = sorted(str(c.get("name") or "") for c in live_columns)
                raise ValueError(
                    f"board column {col!r} resolves to native column "
                    f"{native!r}, which is not present on the live Azure "
                    f"Boards board (available columns: {available})"
                )
            state_mappings = live.get("stateMappings") or {}
            states = tuple(dict.fromkeys(state_mappings.values()))
            result.append(
                BoardColumnSpec(
                    logical=col,
                    native=native,
                    option_id=str(live.get("id") or ""),
                    states=states,
                    is_split=bool(live.get("isSplit")),
                )
            )
        return result

    def _fetch_work_items_batch(
        self,
        project: ProjectConfig,
        token: str | None,
        ids: list[int],
    ) -> list[Ticket]:
        """Bulk-fetch work items by id, chunked at 200 per request."""
        out: list[Ticket] = []
        with _client(project, token) as c:
            for chunk_start in range(0, len(ids), 200):
                chunk = ids[chunk_start : chunk_start + 200]
                resp = c.post(
                    f"{_project_scope(project)}/_apis/wit/workitemsbatch",
                    params=_api_version_params(),
                    json={
                        "ids": chunk,
                        "fields": [
                            "System.Id",
                            "System.Title",
                            "System.Description",
                            "System.State",
                            "System.WorkItemType",
                            "System.Tags",
                            "System.AssignedTo",
                            "System.CreatedBy",
                            "System.CreatedDate",
                            "System.ChangedDate",
                            "System.IterationPath",
                        ],
                    },
                )
                _check(resp)
                for raw in resp.json().get("value") or []:
                    out.append(_map_work_item(raw, project))
        # Preserve the ordering implied by WIQL.
        id_to_ticket = {int(t.id): t for t in out}
        return [id_to_ticket[i] for i in ids if i in id_to_ticket]

    def get_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        include_relations: bool = True,
        include_custom_fields: bool = False,
    ) -> tuple[Ticket, list[Comment], list[Relation] | None, bool | None]:
        """Fetch a single work item with its comments and (optionally) relations.

        When ``include_custom_fields`` is ``True``, ``ticket.custom_fields``
        is populated with the entire raw ``fields`` dict from the work-item
        payload already fetched for this call (no extra HTTP request) —
        every ``System.*`` field plus any custom field references, with
        provider-native keys and values. Defaults to ``False``, in which
        case ``ticket.custom_fields`` stays ``None``.
        """
        _validate_int32_id(ticket_id, "ticket")
        params = _api_version_params({"$expand": "Relations"})
        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
        with _client(project, token) as c:
            resp = c.get(path, params=params)
        _check(resp)
        raw = resp.json()
        ticket = _map_work_item(raw, project)
        if include_custom_fields:
            ticket.custom_fields = raw.get("fields") or {}

        # Comments are a separate endpoint.
        comments = self._list_work_item_comments(
            project, token, ticket_id, limit=200, order="asc"
        )

        if include_relations:
            relations: list[Relation] | None = self._build_relations_from_work_item(
                project, token, raw, ticket_id
            )
            truncated: bool | None = False
            ticket.parent_id = _extract_parent_id(relations)
        else:
            relations = []
            truncated = None

        return ticket, comments, relations, truncated

    def _list_work_item_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        limit: int = 30,
        order: str = "asc",
    ) -> list[Comment]:
        """Pull work-item comments. Pagination via `continuationToken`."""
        path = (
            f"{_project_scope(project)}/_apis/wit/workItems/"
            f"{quote(str(ticket_id), safe='')}/comments"
        )
        params = _api_version_params(
            {"api-version": API_VERSION_COMMENTS, "$top": min(max(1, limit), 200), "order": "asc"}
        )
        all_comments: list[Comment] = []
        continuation: str | None = None
        with _client(project, token) as c:
            while True:
                if continuation:
                    params["continuationToken"] = continuation
                resp = c.get(path, params=params)
                _check(resp)
                payload = resp.json()
                for raw in payload.get("comments") or []:
                    all_comments.append(
                        _map_work_item_comment(raw, project, str(ticket_id))
                    )
                continuation = payload.get("continuationToken")
                if not continuation or len(all_comments) >= limit:
                    break
        if order == "desc":
            all_comments.reverse()
        return all_comments[:limit]

    def _build_relations_from_work_item(
        self,
        project: ProjectConfig,
        token: str | None,
        raw: dict,
        ticket_id: str,
    ) -> list[Relation]:
        rels_payload = raw.get("relations") or []
        # Phase 1: collect (kind, target_id, display_url) for typed
        # relations, and (ref_kind, ref_id) for body-text mentions. We
        # defer Relation construction until phase 3 so a single batch
        # call can populate Title + State for every target.
        typed_targets: list[tuple[str, str, str]] = []
        for rel in rels_payload:
            rel_type = rel.get("rel") or ""
            kind = _ado_rel_to_kind(rel_type)
            if not kind:
                continue
            url = rel.get("url") or ""
            # url looks like `https://dev.azure.com/{org}/_apis/wit/workItems/{id}`.
            m = re.search(r"/workItems/(\d+)", url)
            target_id = m.group(1) if m else ""
            display_url = (
                _build_work_item_url(project, target_id) if target_id else url
            )
            typed_targets.append((kind, target_id, display_url))

        body_text = _html_to_markdown(
            (raw.get("fields") or {}).get("System.Description")
        )
        # Build a set of target ids that already appear as typed relations
        # (phase 1 above).  Any body-text mention of the same id would be
        # a duplicate — typed links are authoritative, so we skip body refs
        # to items that are already represented by a typed relation.
        typed_target_ids: set[str] = {tid for _, tid, _ in typed_targets if tid}
        mention_targets: list[tuple[str, str]] = []
        for ref_kind, ref_id in _scan_refs_for_mentions(body_text):
            if ref_id == str(ticket_id):
                continue
            if ref_id in typed_target_ids:
                # This item already surfaces as a typed relation; don't
                # also add a separate "mentions" entry for it.
                continue
            mention_targets.append((ref_kind, ref_id))

        # Phase 2: single workitemsbatch call to populate Title + State
        # for all targets at once. ADO caps at 200 ids per call which is
        # well above any realistic relation count for a single ticket.
        all_ids = {tid for _, tid, _ in typed_targets if tid}
        all_ids.update(rid for _, rid in mention_targets)
        title_state_by_id = self._fetch_work_item_title_state(
            project, token, sorted(all_ids)
        )

        # Phase 3: build the Relations using the looked-up Title + State.
        out: list[Relation] = []
        for kind, target_id, display_url in typed_targets:
            title, state = title_state_by_id.get(target_id, ("", ""))
            out.append(
                Relation(
                    kind=kind,
                    ticket_id=f"#{target_id}" if target_id else "",
                    title=title,
                    url=display_url,
                    state=state,
                    is_pull_request=False,
                    resolved=True,
                )
            )
        for ref_kind, ref_id in mention_targets:
            title, state = title_state_by_id.get(ref_id, ("", ""))
            out.append(
                Relation(
                    kind=ref_kind,
                    ticket_id=f"#{ref_id}",
                    title=title,
                    url=_build_work_item_url(project, ref_id),
                    state=state,
                    is_pull_request=False,
                    resolved=False,
                )
            )
        return out

    def _fetch_work_item_title_state(
        self,
        project: ProjectConfig,
        token: str | None,
        ids: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Batch-fetch System.Title + System.State for the given work-item ids.

        Returns a `{id: (title, state)}` map. Missing ids (deleted /
        invisible work items) are absent from the map so the caller can
        default to empty strings.

        ADO's `workitemsbatch` defaults to `errorPolicy: "Fail"`, which
        fails the *entire* multi-id request if even one id in the batch
        is momentarily unresolvable — silently blanking title/state for
        every relation in that batch, not just the poison id (#179). We
        request `errorPolicy: "Omit"` so resolvable ids still come back,
        and additionally fall back to per-id requests if the batch call
        itself fails outright (non-success response or a transport
        error), so one bad id can never blank its batch-mates.
        """
        if not ids:
            return {}
        numeric_ids = [int(i) for i in ids if i.isdigit()]
        if not numeric_ids:
            return {}
        result = self._fetch_work_item_title_state_batch(project, token, numeric_ids)
        if result is not None:
            return result
        log.warning(
            "workitemsbatch request failed for ids=%s; retrying individually",
            numeric_ids,
        )
        if len(numeric_ids) == 1:
            # Already a single-id request — retrying it again would just
            # repeat the same failure. Best-effort: empty titles beat
            # blowing up the whole get_ticket path.
            return {}
        # Per-id fallback: a batch of >1 id failed outright, so retry each
        # id as its own single-id request and merge whatever succeeds.
        # This guarantees a single poison/failing id can't blank its
        # batch-mates. These single-id calls never recurse into this
        # fallback (see the `len(numeric_ids) == 1` branch above).
        merged: dict[str, tuple[str, str]] = {}
        for wid in numeric_ids:
            single = self._fetch_work_item_title_state_batch(project, token, [wid])
            if single is None:
                log.warning(
                    "workitemsbatch per-id fallback failed for id=%s", wid
                )
                continue
            merged.update(single)
        return merged

    def _fetch_work_item_title_state_batch(
        self,
        project: ProjectConfig,
        token: str | None,
        numeric_ids: list[int],
    ) -> dict[str, tuple[str, str]] | None:
        """Issue a single `workitemsbatch` POST for the given ids.

        Returns the `{id: (title, state)}` map on success (missing ids are
        simply absent), or `None` if the request itself failed (non-success
        response or a transport error) so the caller can decide whether to
        retry.
        """
        body = {
            "ids": numeric_ids,
            "fields": ["System.Title", "System.State"],
            "errorPolicy": "Omit",
        }
        path = f"{_project_scope(project)}/_apis/wit/workitemsbatch"
        try:
            with _client(project, token) as c:
                resp = c.post(path, params=_api_version_params(), json=body)
            if not resp.is_success:
                return None
            value = (resp.json() or {}).get("value") or []
        except httpx.HTTPError:
            return None
        out: dict[str, tuple[str, str]] = {}
        for item in value:
            wid = item.get("id")
            fields = item.get("fields") or {}
            if wid is None:
                continue
            out[str(wid)] = (
                fields.get("System.Title") or "",
                fields.get("System.State") or "",
            )
        return out

    # ---------- tickets — write -------------------------------------------

    def create_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        title: str,
        body: str,
        labels: list[str],
        assignees: list[str],
        status: Status | None = None,
        custom_fields: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        milestone: Any = _UNSET,
    ) -> Ticket:
        """Create an ADO work item.

        When ``status`` is given, this runs a two-step flow: the work item
        is first POSTed without a ``System.State`` patch (so ADO picks its
        template-default initial state, e.g. ``"New"`` / ``"To Do"`` /
        ``"Proposed"``), then — if the requested status differs from that
        initial state — a follow-up ``update_ticket`` PATCH transitions
        the item via the state machine. This is necessary because ADO
        rejects terminal states (``"Done"`` / ``"Closed"`` / ``"Resolved"``)
        on creation with 400 even though ``list_ticket_statuses`` lists
        them. If the transition fails the work item is **not** rolled
        back; the caller can delete it explicitly if needed.

        When ``status`` is ``None``, behavior is unchanged: a single POST.

        ``custom_fields`` mirrors ``update_ticket.custom_fields``: each
        remaining entry is emitted as a ``/fields/{ref}`` JSON-Patch "add"
        op, appended *after* the standard Title/Description/Tags/AssignedTo
        ops — so an explicit ``System.Title`` (or other standard field) in
        ``custom_fields`` wins over the same-named argument above.
        ``None``/``{}`` is a no-op.

        Special case: if ``custom_fields`` carries ``System.WorkItemType``
        (the canonical ref) or the short alias ``WorkItemType``, that value
        is used as the work-item type for the POST instead of the
        project's configured/discovered default (``System.WorkItemType``
        wins if both are given), and neither key becomes a ``/fields/...``
        op. This is resolved before status validation so states are
        checked against the overridden type.

        Optional ``idempotency_key`` (ticket #150): a retried call with
        the same key (scoped to this project) returns the work item
        created by the first successful call instead of creating a
        duplicate, with ``idempotent_replay=True`` set on the result.
        Only ``title``/``body`` are compared across calls with the same
        key. A retry with the same key but a different ``title``/``body``
        raises ``IdempotencyConflict``. ``None``/``""`` (the default)
        disables idempotency entirely.

        Optional ``milestone`` (ticket #151, keyword-only): maps to a
        ``/fields/System.IterationPath`` JSON-Patch "add" op, appended
        *after* the ``custom_fields`` loop so it wins on conflict with
        ``custom_fields["System.IterationPath"]`` (which keeps working
        unchanged). Uses the ``_UNSET`` sentinel default so "not
        provided" issues no patch op at all; ``milestone=None`` is also a
        no-op on create (there's nothing to clear on a fresh work item).
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

        remaining_custom_fields = dict(custom_fields or {})
        canonical_type_override = remaining_custom_fields.pop("System.WorkItemType", None)
        alias_type_override = remaining_custom_fields.pop("WorkItemType", None)
        wi_type_override = canonical_type_override or alias_type_override
        if wi_type_override:
            wi_type = wi_type_override
        else:
            wi_type = self._default_work_item_type(project, token)

        # Pre-validate the requested status before the POST so the caller
        # gets a clean ValueError for an unknown state name rather than a
        # confusing 400 from the follow-up transition PATCH (or, worse, an
        # orphan work item if the POST succeeds but the transition is
        # rejected). Mirrors github.py / gitlab.py up-front validation.
        if status is not None:
            states = self._states_for_type(project, token, wi_type)
            valid_names = [s.get("name") for s in states if s.get("name")]
            if status not in valid_names:
                raise ValueError(
                    f"unsupported status {status!r} for Azure DevOps — "
                    f"use list_ticket_statuses to discover valid values. "
                    f"Accepted: {', '.join(valid_names)}."
                )

        markers = _marker_set(project)
        body_with_marker = ensure_body_prefix(body or "", markers=markers)
        board_create_labels = (
            project.board.auto_label_names_on_create()
            if project.board is not None
            else []
        )
        merged_labels = sorted(
            set([*labels, project.auto_labels.ai_generated, *board_create_labels])
        )

        patch: list[dict] = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": _markdown_to_html(body_with_marker)},
        ]
        if merged_labels:
            patch.append({
                "op": "add",
                "path": "/fields/System.Tags",
                "value": _tags_string_from_labels(merged_labels),
            })
        if assignees:
            patch.append({
                "op": "add",
                "path": "/fields/System.AssignedTo",
                "value": assignees[0],
            })
        # Note: System.State is intentionally NOT added here. ADO only
        # accepts initial-states on create; terminal states must be reached
        # through the state machine via a follow-up update_ticket below.

        for field_ref, field_value in remaining_custom_fields.items():
            patch.append({"op": "add", "path": f"/fields/{field_ref}", "value": field_value})

        if milestone is not _UNSET and milestone is not None:
            patch.append({
                "op": "add",
                "path": "/fields/System.IterationPath",
                "value": milestone,
            })

        # Config-level area-path scoping (ticket #172): when the project
        # declares a default `area_path` and the caller hasn't already
        # explicitly targeted `System.AreaPath` via `custom_fields`, scope
        # the new work item there. Unlike `milestone` above (an explicit
        # per-call argument that intentionally wins over `custom_fields`),
        # `project.area_path` is a low-precedence config default — an
        # explicit `custom_fields["System.AreaPath"]` always wins and no
        # second op is emitted, so there's never a duplicate.
        if project.area_path and "System.AreaPath" not in remaining_custom_fields:
            patch.append({
                "op": "add",
                "path": "/fields/System.AreaPath",
                "value": project.area_path,
            })

        path = f"{_project_scope(project)}/_apis/wit/workitems/${quote(wi_type, safe='')}"
        with _client(project, token) as c:
            resp = c.post(
                path,
                params=_api_version_params(),
                headers={"Content-Type": "application/json-patch+json"},
                json=patch,
            )
        _check(resp)
        created = _map_work_item(resp.json(), project)

        # Step 2: if a status was requested and differs from the
        # work-item's resulting initial state, transition via update.
        if status is not None and status != created.status:
            try:
                created = self.update_ticket(
                    project, token, str(created.id), status=status
                )
            except Exception as exc:  # AzureDevOpsError or httpx errors
                raise AzureDevOpsError(
                    getattr(exc, "status", 400),
                    f"work item #{created.id} created but state "
                    f"transition to '{status}' failed: {exc}",
                ) from exc
        if idempotency_key:
            _idempotency.record(
                (project.provider, project.id),
                idempotency_key,
                {"title": title, "body": body},
                created,
            )
        return created

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
        """Update a work item.

        Optional ``milestone`` (ticket #151, keyword-only): maps to a
        ``/fields/System.IterationPath`` JSON-Patch op, appended *after*
        the ``custom_fields`` loop so it wins on conflict with
        ``custom_fields["System.IterationPath"]`` (which keeps working
        unchanged). A title string sets the field; ``milestone=None``
        clears it (empty-string value — ADO has no "remove" semantics
        for ``System.IterationPath``, every work item belongs to some
        iteration). ``milestone=`` omitted (``_UNSET``) issues no patch
        op at all.
        """
        _validate_label_lists(labels_add, labels_remove)
        _validate_int32_id(ticket_id, "ticket")
        # Read current work item (need tags + assignee for diff semantics).
        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        try:
            _check(resp)
        except AzureDevOpsError as exc:
            if exc.status == 404:
                raise AzureDevOpsError(
                    404, f"ticket '{project.id}#{ticket_id}' not found"
                ) from exc
            raise
        current = resp.json()
        cur_fields = current.get("fields") or {}
        markers = _marker_set(project)

        patch: list[dict] = []
        if title is not None:
            patch.append({"op": "replace", "path": "/fields/System.Title", "value": title})
        if body is not None:
            already_ai = has_ai_generated_marker(
                _html_to_markdown(cur_fields.get("System.Description") or ""),
                markers=markers,
            )
            new_body = apply_body_marker(body, will_be_ai_generated=already_ai, markers=markers)
            patch.append({
                "op": "replace",
                "path": "/fields/System.Description",
                "value": _markdown_to_html(new_body),
            })
        if status is not None:
            patch.append({"op": "replace", "path": "/fields/System.State", "value": status})

        # Labels: read-modify-write the System.Tags string.
        # Board `on_update` auto-labels (ticket #154) are folded in here
        # too, additive-only — they can trigger a Tags write even when
        # the caller passed no labels_add/labels_remove. `on_move_to`
        # stays inert on Azure Boards (no status_field-equivalent here).
        board_update_labels = (
            project.board.auto_label_names_on_update()
            if project.board is not None
            else []
        )
        if labels_add or labels_remove or board_update_labels:
            current_labels = set(_label_list_from_tags(cur_fields.get("System.Tags")))
            for lbl in labels_add or []:
                current_labels.add(lbl)
            for lbl in labels_remove or []:
                current_labels.discard(lbl)
            current_labels.update(board_update_labels)
            # Always stamp the project's configured "modified" label on a
            # write that doesn't carry the "generated" label already
            # (parity with github.py).
            already_ai_label = project.auto_labels.ai_generated in current_labels
            if not already_ai_label:
                current_labels.add(project.auto_labels.ai_modified)
            patch.append({
                "op": "replace",
                "path": "/fields/System.Tags",
                "value": _tags_string_from_labels(sorted(current_labels)),
            })

        # Assignees: ADO stores a single identity; resolve add/remove
        # against the current value.
        if assignees_add or assignees_remove:
            current_assignee = _identity_display_name(cur_fields.get("System.AssignedTo"))
            new_assignee = current_assignee
            if assignees_remove and current_assignee in (assignees_remove or []):
                new_assignee = ""
            if assignees_add:
                # First new assignee wins (ADO has a single AssignedTo field).
                new_assignee = assignees_add[0]
            if new_assignee != current_assignee:
                op = "replace" if cur_fields.get("System.AssignedTo") else "add"
                if new_assignee:
                    patch.append({
                        "op": op,
                        "path": "/fields/System.AssignedTo",
                        "value": new_assignee,
                    })
                else:
                    patch.append({
                        "op": "remove",
                        "path": "/fields/System.AssignedTo",
                    })

        for field_ref, field_value in (custom_fields or {}).items():
            patch.append({"op": "add", "path": f"/fields/{field_ref}", "value": field_value})

        if milestone is not _UNSET:
            if milestone is None:
                patch.append({
                    "op": "replace",
                    "path": "/fields/System.IterationPath",
                    "value": "",
                })
            else:
                patch.append({
                    "op": "add",
                    "path": "/fields/System.IterationPath",
                    "value": milestone,
                })

        if not patch:
            return _map_work_item(current, project)

        with _client(project, token) as c:
            resp = c.patch(
                path,
                params=_api_version_params(),
                headers={"Content-Type": "application/json-patch+json"},
                json=patch,
            )
        try:
            _check(resp)
        except AzureDevOpsError as exc:
            # If the patch was rejected for an invalid state value,
            # surface a curated `ValueError` with the accepted list —
            # matching the comma-prose "Accepted: ..." style used by
            # `github._split_github_status` and `gitlab.py` so the
            # `_safe` translation produces a clean, uniform message
            # without the raw "Azure DevOps 400:" provider prefix.
            #
            # `_check` already appended the `list_ticket_statuses` hint
            # to the message; we use that as the trigger for the
            # curated rewrap.
            if (
                status is not None
                and exc.status in (400, 409)
                and "list_ticket_statuses" in exc.message
            ):
                accepted: list[str] | None = None
                try:
                    spec = self.list_statuses(project, token)
                    accepted = list(spec.values)
                except Exception:
                    accepted = None
                accepted_clause = (
                    f" Accepted: {', '.join(accepted)}." if accepted else ""
                )
                raise ValueError(
                    f"unsupported status {status!r} for Azure DevOps — "
                    f"use list_ticket_statuses to discover valid values."
                    f"{accepted_clause}"
                ) from exc
            raise
        return _map_work_item(resp.json(), project)

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

        Mirrors `GitHubProvider.bulk_update_tickets` — loops over
        `ticket_ids`, calling `update_ticket` once per id and catching
        `(ProviderError, ValueError)` around each call so one failing id
        (e.g. 404, invalid status/id) does not abort the rest of the
        batch. Results preserve `ticket_ids` order 1:1 — duplicates are
        not deduped, each occurrence is updated independently. An empty
        `ticket_ids` returns `[]` without any HTTP call.
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

    # ---------- comments — work item --------------------------------------

    def add_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        body: str,
    ) -> Comment:
        if not body or not body.strip():
            raise ValueError("body must not be empty")
        body_with_marker = ensure_comment_prefix(body, markers=_marker_set(project))
        path = (
            f"{_project_scope(project)}/_apis/wit/workItems/"
            f"{quote(str(ticket_id), safe='')}/comments"
        )
        with _client(project, token) as c:
            resp = c.post(
                path,
                params=_api_version_params({"api-version": API_VERSION_COMMENTS}),
                json={"text": _markdown_to_html(body_with_marker)},
            )
        try:
            _check(resp)
        except AzureDevOpsError as exc:
            if exc.status == 404:
                raise AzureDevOpsError(
                    404, f"ticket '{project.id}#{ticket_id}' not found"
                ) from exc
            raise
        return _map_work_item_comment(resp.json(), project, str(ticket_id))

    def list_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        limit: int = 30,
        since: str | None = None,
        page: int = 1,
        order: str = "asc",
    ) -> tuple[list[Comment], bool]:
        # Pagination via continuationToken is opaque; `page` is mapped by
        # skipping `(page - 1) * limit` items. Cheap for small limits.
        path = (
            f"{_project_scope(project)}/_apis/wit/workItems/"
            f"{quote(str(ticket_id), safe='')}/comments"
        )
        per_page = max(1, min(limit, 200))
        skip = max(0, (page - 1) * per_page)

        if order == "desc":
            # Drain ALL pages first (newest is at the end when the API
            # returns ascending order), then reverse and slice to limit.
            all_comments: list[Comment] = []
            params_desc: dict[str, Any] = _api_version_params(
                {"api-version": API_VERSION_COMMENTS, "$top": 200, "order": "asc"}
            )
            continuation_desc: str | None = None
            with _client(project, token) as c:
                while True:
                    if continuation_desc:
                        params_desc["continuationToken"] = continuation_desc
                    resp = c.get(path, params=params_desc)
                    _check(resp)
                    payload = resp.json()
                    for raw in payload.get("comments") or []:
                        comment = _map_work_item_comment(raw, project, str(ticket_id))
                        if since and comment.created_at and comment.created_at < since:
                            continue
                        all_comments.append(comment)
                    continuation_desc = payload.get("continuationToken")
                    if not continuation_desc:
                        break
            all_comments.reverse()
            # Apply page-based skip (rarely used in desc, but keep consistent).
            all_comments = all_comments[skip:]
            return all_comments[:limit], False

        collected: list[Comment] = []
        truncated = False
        params: dict[str, Any] = _api_version_params(
            {"api-version": API_VERSION_COMMENTS, "$top": per_page, "order": "asc"}
        )
        continuation: str | None = None
        seen = 0
        with _client(project, token) as c:
            while True:
                if continuation:
                    params["continuationToken"] = continuation
                resp = c.get(path, params=params)
                _check(resp)
                payload = resp.json()
                items = payload.get("comments") or []
                for raw in items:
                    if seen < skip:
                        seen += 1
                        continue
                    comment = _map_work_item_comment(raw, project, str(ticket_id))
                    if since and comment.created_at and comment.created_at < since:
                        continue
                    collected.append(comment)
                    if len(collected) >= per_page:
                        break
                continuation = payload.get("continuationToken")
                if not continuation or len(collected) >= per_page:
                    truncated = bool(continuation) and len(collected) >= per_page
                    break
        return collected, truncated

    def get_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        ticket_id: str | None = None,
    ) -> Comment:
        if not ticket_id:
            raise AzureDevOpsError(
                400,
                "azuredevops.get_comment requires ticket_id "
                "(work-item comment ids are scoped to a work item).",
            )
        _validate_int32_id(ticket_id, "ticket")
        _validate_int32_id(comment_id, "comment")
        path = (
            f"{_project_scope(project)}/_apis/wit/workItems/"
            f"{quote(str(ticket_id), safe='')}/comments/{quote(str(comment_id), safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params({"api-version": API_VERSION_COMMENTS}))
        _check(resp)
        return _map_work_item_comment(resp.json(), project, str(ticket_id))

    def update_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        body: str,
        ticket_id: str | None = None,
    ) -> Comment:
        if not ticket_id:
            raise AzureDevOpsError(
                400,
                "azuredevops.update_comment requires ticket_id "
                "(work-item comment ids are scoped to a work item).",
            )
        _validate_int32_id(ticket_id, "ticket")
        _validate_int32_id(comment_id, "comment")
        if not body or not body.strip():
            raise ValueError("body must not be empty")
        # Read current to decide the marker flavour.
        path = (
            f"{_project_scope(project)}/_apis/wit/workItems/"
            f"{quote(str(ticket_id), safe='')}/comments/{quote(str(comment_id), safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params({"api-version": API_VERSION_COMMENTS}))
        try:
            _check(resp)
        except AzureDevOpsError as exc:
            if exc.status == 404:
                raise AzureDevOpsError(
                    404, f"comment '{project.id}#{comment_id}' not found"
                ) from exc
            raise
        cur_markdown = _html_to_markdown((resp.json() or {}).get("text") or "")
        markers = _marker_set(project)
        already_ai = has_ai_generated_marker(cur_markdown, markers=markers)
        new_body = apply_body_marker(body, will_be_ai_generated=already_ai, markers=markers)
        with _client(project, token) as c:
            resp = c.patch(
                path,
                params=_api_version_params({"api-version": API_VERSION_COMMENTS}),
                json={"text": _markdown_to_html(new_body)},
            )
        _check(resp)
        return _map_work_item_comment(resp.json(), project, str(ticket_id))

    def delete_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        ticket_id: str | None = None,
    ) -> None:
        """Delete a work-item comment by its id.

        `ticket_id` is required because ADO comment ids are scoped to a
        work item. Raises `AzureDevOpsError(400, ...)` when omitted.

        Raises `AzureDevOpsError(404, ...)` when the comment does not
        exist.  Returns `None` on success.
        """
        if not ticket_id:
            raise AzureDevOpsError(
                400,
                "azuredevops.delete_comment requires ticket_id "
                "(work-item comment ids are scoped to a work item).",
            )
        _validate_int32_id(ticket_id, "ticket")
        _validate_int32_id(comment_id, "comment")
        path = (
            f"{_project_scope(project)}/_apis/wit/workItems/"
            f"{quote(str(ticket_id), safe='')}/comments/{quote(str(comment_id), safe='')}"
        )
        with _client(project, token) as c:
            resp = c.delete(
                path,
                params=_api_version_params({"api-version": API_VERSION_COMMENTS}),
            )
        try:
            _check(resp)
        except AzureDevOpsError as exc:
            if exc.status == 404:
                raise AzureDevOpsError(
                    404, f"comment '{project.id}#{comment_id}' not found"
                ) from exc
            raise

    # ---------- pull requests — read --------------------------------------

    def list_prs(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: PRFilters,
    ) -> tuple[list[PullRequest], bool]:
        """List pull requests for a project.

        Returns `(prs, has_more)`. `has_more` is derived from the raw API
        result count before client-side filtering: if the API returned at
        least `filters.limit` items, there may be more pages available.
        """
        repo_id = self._resolve_repository_id(project, token)
        status_param = {
            "open": "active",
            # "closed" maps to "all" so ADO returns both abandoned and
            # completed PRs; we post-filter below to exclude active ones.
            "closed": "all",
            "any": "all",
        }.get(filters.status, "active")
        params = _api_version_params(
            {
                "searchCriteria.status": status_param,
                "$top": max(1, filters.limit),
            }
        )
        if filters.head:
            params["searchCriteria.sourceRefName"] = (
                filters.head
                if filters.head.startswith("refs/")
                else f"refs/heads/{filters.head}"
            )
        if filters.base:
            params["searchCriteria.targetRefName"] = (
                filters.base
                if filters.base.startswith("refs/")
                else f"refs/heads/{filters.base}"
            )
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=params)
        _check(resp)
        raw_items = resp.json().get("value") or []
        # has_more is derived from the pre-filter API count, mirroring how
        # list_tickets derives it from len(ids) >= filters.limit.
        has_more = len(raw_items) >= filters.limit
        prs = [_map_pr(raw, project) for raw in raw_items]
        # ADO doesn't filter labels server-side; do it client-side.
        if filters.labels:
            wanted = set(filters.labels)
            prs = [pr for pr in prs if wanted.issubset(set(pr.labels))]
        if filters.search:
            needle = filters.search.lower()
            prs = [pr for pr in prs if needle in pr.title.lower() or needle in pr.body.lower()]
        # When the caller requests "closed" we asked ADO for "all" PRs so that
        # both abandoned and completed PRs are included. Post-filter to remove
        # the active ones that ADO included.
        if filters.status == "closed":
            prs = [pr for pr in prs if pr.status != "open"]
        return prs[: max(1, filters.limit)], has_more

    def get_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> tuple[PullRequest, list[Comment]]:
        repo_id = self._resolve_repository_id(project, token)
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        raw = resp.json()
        pr = _map_pr(raw, project)
        # ADO's single-PR GET doesn't include labels by default; the
        # list-PRs endpoint does. Fetch the labels endpoint so every
        # PR-returning surface (create / update / get / merge_pr)
        # advertises labels consistently.
        pr.labels = self._fetch_pr_labels(project, token, repo_id, pr_id)
        # Synthesize reviews from the vote data already present on `raw`
        # (ticket #148) — no extra round trip. `pr.reviewers` is left
        # untouched (`_map_pr` already sets it via `_identity_display_name`).
        reviews = self._reviews_from_votes(raw, project, pr_id)
        pr.reviews = reviews
        pr.review_decision = review_decision_from_states(
            [rv.state for rv in _latest_reviews_by_author(reviews)]
        )
        comments = self._list_pr_top_level_comments(project, token, pr_id, repo_id)
        return pr, comments

    def _fetch_pr_labels(
        self,
        project: ProjectConfig,
        token: str | None,
        repo_id: str,
        pr_id: str,
    ) -> list[str]:
        """Read the PR labels endpoint and return a sorted name list.

        Best-effort: a 403/404 just returns `[]` so a missing label
        permission doesn't kill the legitimate PR fetch.
        """
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/"
            f"{quote(str(pr_id), safe='')}/labels"
        )
        try:
            with _client(project, token) as c:
                resp = c.get(path, params=_api_version_params())
        except httpx.HTTPError:
            return []
        if resp.status_code in (403, 404):
            return []
        if not resp.is_success:
            return []
        payload = resp.json() or {}
        return sorted(
            (lbl.get("name") or "")
            for lbl in (payload.get("value") or [])
            if lbl.get("name") and lbl.get("active", True)
        )

    def _list_pr_threads(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        repo_id: str,
    ) -> list[dict]:
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}/threads"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        return list(resp.json().get("value") or [])

    def _list_pr_top_level_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        repo_id: str,
    ) -> list[Comment]:
        threads = self._list_pr_threads(project, token, pr_id, repo_id)
        out: list[Comment] = []
        for thread in threads:
            if thread.get("threadContext"):
                continue
            if thread.get("isDeleted"):
                continue
            # Threads created as the body of a `submit_pr_review` are
            # tagged with a custom property; hide them from the flat
            # comments stream to match GitHub's separation of review
            # bodies vs PR comments.
            if _is_review_body_thread(thread):
                continue
            for raw in thread.get("comments") or []:
                if raw.get("commentType") == "system":
                    continue
                out.append(_map_thread_comment(thread, raw, project, str(pr_id)))
        return out

    def list_pr_review_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> list[ReviewComment]:
        repo_id = self._resolve_repository_id(project, token)
        threads = self._list_pr_threads(project, token, pr_id, repo_id)
        out: list[ReviewComment] = []
        for thread in threads:
            if not thread.get("threadContext"):
                continue
            if thread.get("isDeleted"):
                continue
            for raw in thread.get("comments") or []:
                if raw.get("commentType") == "system":
                    continue
                out.append(
                    _map_thread_comment_for_review(thread, raw, project, str(pr_id))
                )
        return out

    _VOTE_STATE_MAP: dict[int, ReviewState] = {
        10: "approve",
        5: "approve",
        -10: "request_changes",
        -5: "comment",
    }

    def _reviews_from_votes(
        self, raw: dict, project: ProjectConfig, pr_id: str
    ) -> list[Review]:
        """Synthesize `Review` objects from `raw["reviewers"]` vote data.

        Lighter-weight sibling of `list_pr_reviews`: reuses the same
        vote -> state mapping (`_VOTE_STATE_MAP`) and identity resolution
        (`_identity_login_or_display`), but skips the extra thread fetch
        `list_pr_reviews` performs to recover a review body — `get_pr` is
        on the single-PR read path and shouldn't pay for an extra round
        trip just to populate `Review.body`. `body` is always `""` and
        `url` is the PR URL (via `_build_pr_url`) for every entry.
        Reviewers with a `0` (not-yet-voted) vote are skipped, matching
        `list_pr_reviews`.
        """
        out: list[Review] = []
        for reviewer in raw.get("reviewers") or []:
            vote = reviewer.get("vote") or 0
            state = self._VOTE_STATE_MAP.get(vote)
            if state is None:
                continue
            reviewer_id = reviewer.get("id") or ""
            out.append(
                Review(
                    id=f"{reviewer_id}:{vote}",
                    state=state,
                    author=_identity_login_or_display(reviewer),
                    body="",
                    url=_build_pr_url(project, pr_id),
                    submitted_at="",
                )
            )
        return out

    def list_pr_reviews(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> list[Review]:
        """List reviews on a PR, synthesized from reviewer votes.

        ADO models a review as a per-reviewer `vote` on the PR resource
        rather than a distinct review object (mirrors `submit_pr_review`'s
        write path). This fetches the PR (the same GET `get_pr` uses) and
        emits one `Review` per reviewer with a non-zero vote:

          - `10` (approved) / `5` (approved-with-suggestions) -> `"approve"`
          - `-10` (rejected) -> `"request_changes"`
          - `-5` (waiting-for-author) -> `"comment"`
          - `0` (merely requested, hasn't voted) -> skipped, matching the
            reviewers/requested split already applied in `_map_pr`.

        Bodies aren't attached to the vote itself, so this best-effort
        matches each review to the review-body thread (tagged via
        `_is_review_body_thread`) whose first comment's author id equals
        the reviewer's id. When matched, `body`/`submitted_at` come from
        that thread; otherwise both fall back to `""` (there is no
        review-specific body/timestamp to report on Azure DevOps, and
        GitHub/Azure both emit `str` rather than `None` for `Review.body`
        per the shared convention documented on the dataclass).
        """
        repo_id = self._resolve_repository_id(project, token)
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        raw = resp.json()

        review_body_threads = [
            t
            for t in self._list_pr_threads(project, token, pr_id, repo_id)
            if _is_review_body_thread(t)
        ]

        def _matching_thread(reviewer_id: str) -> dict | None:
            for thread in review_body_threads:
                comments = thread.get("comments") or []
                if not comments:
                    continue
                author_id = (comments[0].get("author") or {}).get("id")
                if author_id == reviewer_id:
                    return thread
            return None

        out: list[Review] = []
        for reviewer in raw.get("reviewers") or []:
            vote = reviewer.get("vote") or 0
            state = self._VOTE_STATE_MAP.get(vote)
            if state is None:
                continue
            reviewer_id = reviewer.get("id") or ""
            thread = _matching_thread(reviewer_id)
            if thread is not None:
                body = _html_to_markdown(
                    (thread.get("comments") or [{}])[0].get("content") or ""
                )
                submitted_at = normalize_timestamp(thread.get("publishedDate") or "")
            else:
                body = ""
                submitted_at = ""
            out.append(
                Review(
                    id=f"{reviewer_id}:{vote}",
                    state=state,
                    author=_identity_login_or_display(reviewer),
                    body=body,
                    url=_build_pr_url(project, pr_id),
                    submitted_at=submitted_at,
                )
            )
        return out

    # ---------- pull requests — write -------------------------------------

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
        """Create a pull request, applying the AI-generated marker + label.

        Mirrors `github.py:create_pr`: the body always gets the project's
        configured "generated" marker prefix and the project's configured
        "generated" label (`project.auto_labels.ai_generated`, `ai-generated`
        by default) is appended to the user-supplied labels (de-duped,
        preserving the caller's order). Label application is best-effort —
        a 403/404 from `_add_pr_label` is swallowed so a missing
        tag-management permission doesn't kill the legitimate PR.

        Optional `idempotency_key` (ticket #150): a retried call with the
        same key (scoped to this project) returns the PR created by the
        first successful call instead of creating a duplicate, with
        `idempotent_replay=True` set on the result. Only `title`/`head`/
        `base` are compared across calls with the same key. A retry with
        the same key but a different `title`/`head`/`base` raises
        `IdempotencyConflict`. `None`/`""` (the default) disables
        idempotency entirely.
        """
        if idempotency_key:
            replay = _idempotency.lookup(
                (project.provider, project.id),
                idempotency_key,
                {"title": title, "head": head, "base": base},
            )
            if replay is not None:
                return replay
        repo_id = self._resolve_repository_id(project, token)
        body_with_marker = ensure_body_prefix(body or "", markers=_marker_set(project))
        payload: dict[str, Any] = {
            "sourceRefName": head if head.startswith("refs/") else f"refs/heads/{head}",
            "targetRefName": base if base.startswith("refs/") else f"refs/heads/{base}",
            "title": title,
            "description": _markdown_to_html(body_with_marker),
            "isDraft": bool(draft),
        }
        if requested_reviewers:
            # ADO needs reviewer GUIDs; without a directory lookup we
            # cannot resolve names client-side. Best-effort: pass the
            # names through as `uniqueName`-style identifiers; ADO will
            # 400 if it can't resolve them, surfaced via _check.
            payload["reviewers"] = [{"id": r} for r in requested_reviewers]
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests"
        )
        with _client(project, token) as c:
            resp = c.post(path, params=_api_version_params(), json=payload)
        _check(resp)
        pr = _map_pr(resp.json(), project)
        # Mirror github.py:create_pr — always merge the project's
        # configured "generated" label, dropping caller duplicates while
        # preserving their order.
        merged_labels = list(
            dict.fromkeys([*(labels or []), project.auto_labels.ai_generated])
        )
        if merged_labels:
            for lbl in merged_labels:
                self._add_pr_label_best_effort(project, token, repo_id, pr.id, lbl)
            pr, _ = self.get_pr(project, token, pr.id)
        if idempotency_key:
            _idempotency.record(
                (project.provider, project.id),
                idempotency_key,
                {"title": title, "head": head, "base": base},
                pr,
            )
        return pr

    def _add_pr_label_best_effort(
        self,
        project: ProjectConfig,
        token: str | None,
        repo_id: str,
        pr_id: str,
        label: str,
    ) -> bool:
        """Like `_add_pr_label` but swallows permission failures.

        Returns True on success, False when the call returned 403/404 or
        an `AzureDevOpsError` carrying those statuses — matching the
        GitHub provider's best-effort label policy.
        """
        try:
            self._add_pr_label(project, token, repo_id, pr_id, label)
            return True
        except AzureDevOpsError as exc:
            if exc.status in (403, 404):
                log.info(
                    "skipping label %r on PR %s: %s", label, pr_id, exc.message,
                )
                return False
            raise

    def _add_pr_label(
        self,
        project: ProjectConfig,
        token: str | None,
        repo_id: str,
        pr_id: str,
        label: str,
    ) -> None:
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}/labels"
        )
        with _client(project, token) as c:
            resp = c.post(
                path,
                params=_api_version_params(),
                json={"name": label},
            )
        _check(resp)

    def _remove_pr_label(
        self,
        project: ProjectConfig,
        token: str | None,
        repo_id: str,
        pr_id: str,
        label: str,
    ) -> None:
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}/labels/{quote(label, safe='')}"
        )
        with _client(project, token) as c:
            resp = c.delete(path, params=_api_version_params())
        if resp.status_code == 404:
            return
        _check(resp)

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
        """Update a PR's title/body/state/base, plus label/reviewer deltas.

        Mirrors `github.py:update_pr` for the marker + label policy:
          - The body always carries a marker line; the project's
            configured "generated" marker is preserved when the PR was
            originally agent-authored, otherwise the "modified" marker
            is stamped.
          - When the existing PR does not already carry the project's
            configured "generated" label, the "modified" label is added
            (best-effort, swallowing 403/404).

        Limitation: ADO has no native `updated_at` field on PRs, so we
        synthesize one (current UTC) whenever this call performs any
        write — the returned `pr.updated_at` therefore reflects "this
        call mutated the PR" rather than ADO's persisted state.
        """
        _validate_label_lists(labels_add, labels_remove)
        repo_id = self._resolve_repository_id(project, token)
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
        )

        # Read once: we need the current body/labels for marker + label
        # state decisions; reusing this snapshot across the body and
        # label branches keeps it to one round-trip.
        with _client(project, token) as c:
            cur_resp = c.get(path, params=_api_version_params())
        _check(cur_resp)
        cur = cur_resp.json() or {}
        cur_labels = {
            (lbl.get("name") or "")
            for lbl in (cur.get("labels") or [])
            if lbl.get("name")
        }
        cur_md = _html_to_markdown(cur.get("description") or "")
        markers = _marker_set(project)
        already_ai = has_ai_generated_marker(cur_md, markers=markers) or (
            project.auto_labels.ai_generated in cur_labels
        )

        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            new_body = apply_body_marker(body, will_be_ai_generated=already_ai, markers=markers)
            payload["description"] = _markdown_to_html(new_body)
        if base is not None:
            payload["targetRefName"] = (
                base if base.startswith("refs/") else f"refs/heads/{base}"
            )
        if draft is not None:
            payload["isDraft"] = bool(draft)
        if status is not None:
            payload["status"] = {
                "open": "active",
                "closed": "abandoned",
                "merged": "completed",
            }.get(status, status)

        wrote = False
        if payload:
            with _client(project, token) as c:
                resp = c.patch(path, params=_api_version_params(), json=payload)
            _check(resp)
            wrote = True

        # Auto-apply the project's configured "modified" label when the PR
        # wasn't originally agent-authored AND that label isn't already in
        # the requested deltas. Best-effort: a missing tag-management
        # permission doesn't kill the legitimate update.
        explicit_label_names = set(labels_add or []) | set(labels_remove or [])
        if (
            not already_ai
            and project.auto_labels.ai_modified not in cur_labels
            and project.auto_labels.ai_modified not in explicit_label_names
        ):
            if self._add_pr_label_best_effort(
                project, token, repo_id, pr_id, project.auto_labels.ai_modified
            ):
                wrote = True

        for lbl in labels_add or []:
            self._add_pr_label(project, token, repo_id, pr_id, lbl)
            wrote = True
        for lbl in labels_remove or []:
            self._remove_pr_label(project, token, repo_id, pr_id, lbl)
            wrote = True

        if reviewers_add or reviewers_remove:
            for r in reviewers_add or []:
                rev_path = (
                    f"{_project_scope(project)}/_apis/git/repositories/"
                    f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}/reviewers/{quote(r, safe='')}"
                )
                with _client(project, token) as c:
                    resp = c.put(rev_path, params=_api_version_params(), json={"vote": 0})
                _check(resp)
                wrote = True
            for r in reviewers_remove or []:
                rev_path = (
                    f"{_project_scope(project)}/_apis/git/repositories/"
                    f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}/reviewers/{quote(r, safe='')}"
                )
                with _client(project, token) as c:
                    resp = c.delete(rev_path, params=_api_version_params())
                if resp.status_code != 404:
                    _check(resp)
                wrote = True

        pr, _ = self.get_pr(project, token, pr_id)
        if wrote:
            pr.updated_at = _utc_iso_now()
        return pr

    def merge_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        merge_method: str = "merge",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> PullRequest:
        """Complete a PR. ADO's `status=completed` PATCH triggers an async
        merge — the response carries the pre-merge snapshot, so we
        poll until `mergeStatus` settles before mapping the PR. The
        backoff cap is ~3s total; if the merge is still in flight after
        that we raise `AzureDevOpsError(202, …)` so callers know to
        re-fetch rather than treat the snapshot as merged=false.

        Limitation: ADO branch policies can override the requested merge
        strategy server-side without returning an error. When the settled
        PR reports a different ``mergeStrategy`` than the one we sent, a
        warning is emitted so callers are aware of the discrepancy.

        Note: completing a PR does not alter its reviewer set — ADO does not add the merging user to the PR's reviewers, so the returned PullRequest.requested_reviewers reflects only reviewers already assigned (empty if none); see test_merge_pr_does_not_populate_requested_reviewers.
        """
        repo_id = self._resolve_repository_id(project, token)
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
        )
        # Need lastMergeSourceCommit for the completion handshake.
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        cur = resp.json() or {}
        if cur.get("status") == "completed" and cur.get("mergeStatus") == "succeeded":
            raise AzureDevOpsError(
                405, f"PR '{project.id}#{pr_id}' is already merged"
            )
        merge_strategy = {
            "merge": "noFastForward",
            "squash": "squash",
            "rebase": "rebase",
        }.get(merge_method, merge_method)
        body: dict[str, Any] = {
            "status": "completed",
            "lastMergeSourceCommit": cur.get("lastMergeSourceCommit") or {},
            "completionOptions": {
                "mergeStrategy": merge_strategy,
                "deleteSourceBranch": False,
            },
        }
        if commit_title or commit_message:
            body["completionOptions"]["mergeCommitMessage"] = (
                (commit_title or "") + ("\n\n" + commit_message if commit_message else "")
            ).strip()
        with _client(project, token) as c:
            resp = c.patch(path, params=_api_version_params(), json=body)
        _check(resp)

        # Settle-loop: ADO returns the PATCH response before the async
        # merge finishes. Poll until mergeStatus is a terminal state.
        settled = self._wait_for_merge_settle(project, token, path, pr_id)

        # Branch policies may silently override the requested merge
        # strategy.  Warn when a non-default strategy was requested and
        # the settled PR reports a different one so callers are aware.
        if merge_method != "merge":
            settled_strategy = (
                settled.get("completionOptions") or {}
            ).get("mergeStrategy")
            if settled_strategy and settled_strategy != merge_strategy:
                log.warning(
                    "PR %s: requested merge strategy %r (ADO: %r) was "
                    "overridden to %r by a branch policy",
                    pr_id, merge_method, merge_strategy, settled_strategy,
                )

        merge_status = settled.get("mergeStatus")
        if merge_status == "conflicts":
            raise AzureDevOpsError(
                409,
                f"PR {pr_id}: merge has conflicts — resolve before retrying",
            )
        if merge_status == "rejectedByPolicy":
            raise AzureDevOpsError(
                409,
                f"PR {pr_id}: merge rejected by branch policy",
            )
        if merge_status == "failure":
            raise AzureDevOpsError(
                500,
                f"PR {pr_id}: merge failed (Azure DevOps server-side error)",
            )
        if merge_status != "succeeded":
            raise AzureDevOpsError(
                202,
                f"PR {pr_id}: merge in progress — retry get_pr to confirm",
            )
        pr = _map_pr(settled, project)
        # Labels live on a separate endpoint — keep merge_pr's return
        # consistent with get_pr / list_prs.
        pr.labels = self._fetch_pr_labels(project, token, repo_id, str(pr_id))
        return pr

    _MERGE_SETTLE_DELAYS_MS: tuple[int, ...] = (
        200, 400, 800, 1600, 3200, 4000,
    )

    def _wait_for_merge_settle(
        self,
        project: ProjectConfig,
        token: str | None,
        path: str,
        pr_id: str,
    ) -> dict:
        """Poll the PR until both `mergeStatus` AND `status` settle.

        Returns the last fetched PR payload regardless of outcome; the
        caller inspects `mergeStatus` (and `status`) to translate into
        success / error.

        Settled means EITHER:
          - mergeStatus == "succeeded" AND status == "completed"
            (i.e. ADO has actually finalized the merge), OR
          - mergeStatus ∈ {"conflicts", "rejectedByPolicy", "failure"}
            (terminal failure modes).

        Earlier versions only checked mergeStatus and could return a
        snapshot with status still "active" — `_map_pr` then derives
        `merged=false` even though the merge is done. Cumulative cap
        is ~10s to absorb slow-environment lag.
        """
        merge_failure = {"conflicts", "rejectedByPolicy", "failure"}
        last: dict = {}
        for delay_ms in self._MERGE_SETTLE_DELAYS_MS:
            time.sleep(delay_ms / 1000.0)
            with _client(project, token) as c:
                resp = c.get(path, params=_api_version_params())
            _check(resp)
            last = resp.json() or {}
            ms = last.get("mergeStatus")
            st = last.get("status")
            if ms in merge_failure:
                return last
            if ms == "succeeded" and st == "completed":
                return last
        return last

    # ---------- pull requests — comments / reviews ------------------------

    def add_pr_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        body: str,
    ) -> Comment:
        repo_id = self._resolve_repository_id(project, token)
        body_with_marker = ensure_comment_prefix(body or "", markers=_marker_set(project))
        path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}/threads"
        )
        payload = {
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": _markdown_to_html(body_with_marker),
                    "commentType": "text",
                }
            ],
            "status": "active",
        }
        with _client(project, token) as c:
            resp = c.post(path, params=_api_version_params(), json=payload)
        _check(resp)
        thread = resp.json()
        comments = thread.get("comments") or []
        if not comments:
            raise AzureDevOpsError(0, "thread create returned no comments")
        return _map_thread_comment(thread, comments[0], project, str(pr_id))

    def add_pr_review_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        body: str,
        path: str | None = None,
        line: int | None = None,
        side: str | None = None,
        commit_sha: str | None = None,
        in_reply_to: str | None = None,
    ) -> ReviewComment:
        """Add a review comment to a PR, either as a new diff-anchored
        thread (`path`+`line`) or as a reply to an existing thread
        (`in_reply_to`).

        `commit_sha`, if provided on the new-thread path, is now
        persisted as a thread-level property
        (`REVIEW_COMMIT_SHA_PROPERTY_KEY`, ticket #175): a later
        `get_pr`/`list_pr_review_comments` re-read of this same comment
        goes through `_map_thread_comment_for_review`, which reads that
        property back and reports the same `commit_sha` instead of
        `None`. It is also still echoed into the `ReviewComment`
        returned immediately here as a create-time convenience
        (matching GitHub's review-comment return shape), which is a
        harmless no-op fallback once the property round-trips. The
        reply (`in_reply_to`) path does not write the property itself —
        a reply comment inherits whatever `commit_sha` is already
        stored on its parent thread when read back, since the property
        lives at the thread level, not per-comment.
        """
        # Validate before doing any network I/O so callers fail fast.
        if not in_reply_to and (not path or line is None):
            raise AzureDevOpsError(
                0,
                "azuredevops.add_pr_review_comment requires either "
                "in_reply_to (reply to existing thread) or path+line "
                "(create a new diff-anchored thread).",
            )
        repo_id = self._resolve_repository_id(project, token)
        body_with_marker = ensure_comment_prefix(body or "", markers=_marker_set(project))

        if in_reply_to:
            # Reply lives inside an existing thread. Accept both the
            # canonical thread-id form and the legacy
            # `"{thread}.{comment}"` composite that older callers may
            # have round-tripped from a previous `id`.
            thread_alias = _parse_thread_id_from_alias(in_reply_to)
            if not thread_alias:
                raise AzureDevOpsError(
                    422,
                    f"in_reply_to {in_reply_to!r} is not a valid thread id",
                )
            thread_path = (
                f"{_project_scope(project)}/_apis/git/repositories/"
                f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
                f"/threads/{quote(thread_alias, safe='')}/comments"
            )
            with _client(project, token) as c:
                resp = c.post(
                    thread_path,
                    params=_api_version_params(),
                    json={
                        "parentCommentId": 1,
                        "content": _markdown_to_html(body_with_marker),
                        "commentType": "text",
                    },
                )
            _check(resp)
            raw = resp.json()
            # Re-fetch the thread to populate `threadContext` properly.
            thread_get_path = (
                f"{_project_scope(project)}/_apis/git/repositories/"
                f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
                f"/threads/{quote(thread_alias, safe='')}"
            )
            with _client(project, token) as c:
                tresp = c.get(thread_get_path, params=_api_version_params())
            _check(tresp)
            thread = tresp.json()
            rc = _map_thread_comment_for_review(thread, raw, project, str(pr_id))
            # Echo the caller-supplied commit_sha when the thread payload
            # doesn't carry one — matches GitHub's review-comment return.
            if commit_sha and not rc.commit_sha:
                rc.commit_sha = commit_sha
            return rc

        # Validate that `line` is reachable at the PR head — ADO would
        # silently accept lines beyond the file length and create a
        # context-less thread; GitHub returns 422 here. Mirror that.
        self._validate_pr_diff_line(
            project, token, repo_id, str(pr_id), path, line, side, commit_sha,
        )

        thread_context: dict[str, Any] = {"filePath": path}
        if (side or "RIGHT").upper() == "LEFT":
            thread_context["leftFileStart"] = {"line": line, "offset": 1}
            thread_context["leftFileEnd"] = {"line": line, "offset": 1}
        else:
            thread_context["rightFileStart"] = {"line": line, "offset": 1}
            thread_context["rightFileEnd"] = {"line": line, "offset": 1}

        payload: dict[str, Any] = {
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": _markdown_to_html(body_with_marker),
                    "commentType": "text",
                }
            ],
            "status": "active",
            "threadContext": thread_context,
        }
        if commit_sha:
            # Persist the caller-supplied commit_sha as a thread-level
            # property (ticket #175) so a later re-read through
            # `_map_thread_comment_for_review` gets it back instead of
            # `None`. Mirrors `submit_pr_review`'s REVIEW_BODY_PROPERTY_KEY
            # envelope shape so both round-trip through the same
            # `_thread_property_value` reader.
            payload["properties"] = {
                REVIEW_COMMIT_SHA_PROPERTY_KEY: {
                    "$type": "System.String",
                    "$value": commit_sha,
                },
            }
        threads_path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}/threads"
        )
        with _client(project, token) as c:
            resp = c.post(threads_path, params=_api_version_params(), json=payload)
        _check(resp)
        thread = resp.json()
        comments = thread.get("comments") or []
        if not comments:
            raise AzureDevOpsError(0, "thread create returned no comments")
        rc = _map_thread_comment_for_review(thread, comments[0], project, str(pr_id))
        # Echo the caller-supplied commit_sha when the thread payload
        # doesn't carry one — matches GitHub's review-comment return.
        if commit_sha and not rc.commit_sha:
            rc.commit_sha = commit_sha
        return rc

    def _validate_pr_diff_line(
        self,
        project: ProjectConfig,
        token: str | None,
        repo_id: str,
        pr_id: str,
        file_path: str,
        line: int,
        side: str | None,
        commit_sha: str | None,
    ) -> None:
        """Reject `line` if outside the file's line range at the PR head.

        ADO would otherwise silently anchor the thread to nothing; GitHub
        returns 422 for the same input. The check is best-effort —
        binary files, deleted files, and missing iterations short-circuit
        to `AzureDevOpsError(422, …)` so the caller sees the same shape.
        """
        if line is None or line < 1:
            raise AzureDevOpsError(
                422,
                f"line {line!r} is not a valid 1-based line number",
            )
        # Resolve the commit to fetch the file from. For `side=LEFT` we
        # want the target (base) commit; otherwise the source (head)
        # commit. Use the provided commit_sha when set; else fetch from
        # the PR resource.
        commit_to_use = commit_sha
        if not commit_to_use:
            pr_path = (
                f"{_project_scope(project)}/_apis/git/repositories/"
                f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
            )
            with _client(project, token) as c:
                pr_resp = c.get(pr_path, params=_api_version_params())
            _check(pr_resp)
            pr_raw = pr_resp.json() or {}
            if (side or "RIGHT").upper() == "LEFT":
                commit_to_use = (
                    (pr_raw.get("lastMergeTargetCommit") or {}).get("commitId")
                )
            else:
                commit_to_use = (
                    (pr_raw.get("lastMergeSourceCommit") or {}).get("commitId")
                )
        if not commit_to_use:
            raise AzureDevOpsError(
                422,
                f"could not resolve commit to validate line {line} of {file_path}",
            )
        item_path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/items"
        )
        params = _api_version_params(
            {
                "path": file_path,
                "versionDescriptor.version": commit_to_use,
                "versionDescriptor.versionType": "commit",
                "includeContent": "true",
                "$format": "json",
            }
        )
        with _client(project, token) as c:
            item_resp = c.get(item_path, params=params)
        if item_resp.status_code == 404:
            raise AzureDevOpsError(
                422,
                f"file {file_path!r} not found at commit {commit_to_use[:8]} "
                f"of PR {pr_id}",
            )
        _check(item_resp)
        content = (item_resp.json() or {}).get("content")
        if not isinstance(content, str):
            # Binary or unreadable file — fall through silently rather
            # than blocking the comment.
            return
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        if line > line_count:
            raise AzureDevOpsError(
                422,
                f"line {line} is outside the {line_count}-line range of "
                f"{file_path} at commit {commit_to_use[:8]}",
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
        """Vote on a PR (approve / request_changes / comment).

        ADO models reviews as a per-reviewer vote on the PR resource;
        there is no native "review body" attached to the vote. When a
        body is supplied we post it as a normal thread but tag the
        thread with a custom property
        (`projectIssues.kind=review_body`) so it doesn't leak into
        `get_pr().comments[]` or `list_comments` — matching GitHub's
        separation of review bodies vs ordinary PR comments.

        Identity and id are synthesized to match the cross-provider
        `Review` shape: `author` is the reviewer's unique-name (UPN/email,
        the closest analogue of a GitHub login), and `id` includes the
        vote + ms-timestamp so consecutive `comment + approve` calls
        from the same reviewer don't collide.

        ADO's vote API has no timestamp field, so `submitted_at` cannot
        be synthesized the way `id` is — a fabricated "now" would not
        match what an immediate read-back via `list_pr_reviews` recovers
        (ticket #178). Instead, when `body` is supplied, `submitted_at`
        is the `publishedDate` of the review-body thread just posted
        (normalized via `normalize_timestamp`) — the same value
        `list_pr_reviews` reads back from that thread. When no `body` is
        supplied there is no thread to source a timestamp from, so
        `submitted_at` is `""`.

        Note: casting a vote has a server-side side effect on the PR's reviewer set — ADO enrolls the voting (current) user as a participant. An 'approve'/'request_changes' vote places that user in the PR's 'reviewers'; a 'comment' vote (vote=0) transiently surfaces them in 'requested_reviewers'. This is ADO server behavior triggered by the PUT to /reviewers/{reviewerId} below, not a mutation this method makes to the returned Review (which carries no reviewer list); a subsequent get_pr reflects it. See test_submit_pr_review_docstring_records_reviewer_side_effect.
        """
        repo_id = self._resolve_repository_id(project, token)
        vote = {"approve": 10, "request_changes": -10, "comment": 0}.get(state, 0)

        # Resolve "myself" via connectionData; ADO requires the reviewer id.
        # connectionData is still a preview API — the stable marker is rejected.
        with _client(project, token) as c:
            cd_resp = c.get(
                f"{_org_scope(project)}/_apis/connectionData",
                params={"api-version": "7.1-preview.1"},
            )
        _check(cd_resp)
        identity = cd_resp.json().get("authenticatedUser") or {}
        reviewer_id = identity.get("id") or ""
        if not reviewer_id:
            raise AzureDevOpsError(
                400,
                "could not resolve current user id for PR review submission",
            )

        rev_path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
            f"/reviewers/{quote(reviewer_id, safe='')}"
        )
        with _client(project, token) as c:
            resp = c.put(rev_path, params=_api_version_params(), json={"vote": vote})
        try:
            _check(resp)
        except AzureDevOpsError as exc:
            # TF401181: "The pull request cannot be edited because its status
            # is not 'Active'." — surfaces when the PR is merged/completed.
            if "TF401181" in exc.message or (
                exc.status in (400, 409)
                and "cannot be edited" in exc.message
            ):
                raise AzureDevOpsError(
                    exc.status,
                    "cannot submit a review on a merged or completed pull request",
                ) from exc
            raise
        # The reviewer PUT response carries the full identity
        # (displayName + uniqueName), while connectionData often returns
        # only the GUID. Prefer the PUT response, falling back to
        # connectionData so a missing field doesn't drop us to the GUID.
        reviewer_identity = resp.json() or {}
        merged_identity = {**identity, **reviewer_identity}

        body_with_marker = (
            ensure_comment_prefix(body, markers=_marker_set(project)) if body else ""
        )
        submitted_at = ""
        if body:
            threads_path = (
                f"{_project_scope(project)}/_apis/git/repositories/"
                f"{quote(repo_id, safe='')}/pullrequests/"
                f"{quote(str(pr_id), safe='')}/threads"
            )
            payload = {
                "comments": [
                    {
                        "parentCommentId": 0,
                        "content": _markdown_to_html(body_with_marker),
                        "commentType": "text",
                    }
                ],
                "status": "active",
                "properties": {
                    REVIEW_BODY_PROPERTY_KEY: {
                        "$type": "System.String",
                        "$value": REVIEW_BODY_PROPERTY_VALUE,
                    },
                },
            }
            with _client(project, token) as c:
                tresp = c.post(threads_path, params=_api_version_params(), json=payload)
            _check(tresp)
            submitted_at = normalize_timestamp(
                (tresp.json() or {}).get("publishedDate") or ""
            )

        normalized_state: Any = (
            state if state in ("approve", "request_changes", "comment") else "comment"
        )
        synthesized_id = (
            f"{reviewer_id}:{vote}:{int(time.time() * 1000)}"
        )
        return Review(
            id=synthesized_id,
            state=normalized_state,
            # `_identity_login_or_display` keeps the author field
            # consistent with `_map_thread_comment` (PR comment author)
            # and `_map_thread_comment_for_review` (review-comment
            # author) — all comment/review authorship surfaces on Azure
            # now use the same login-shaped identifier, matching how
            # GitHub and GitLab surface `user.login`/username on every
            # comment-author surface. PR-level participant fields
            # (`_map_pr`'s author/reviewers/requested_reviewers) are a
            # different surface and intentionally keep the display-name
            # shape.
            author=_identity_login_or_display(merged_identity),
            # Surface the marker-prefixed body so the contract docs
            # ("body carries #ai-generated") hold for the Review return.
            body=body_with_marker,
            url=_build_pr_url(project, pr_id),
            submitted_at=submitted_at,
            commit_sha=commit_sha,
        )

    # ---------- relations --------------------------------------------------

    def add_relation(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        kind: str,
        target: str,
    ) -> Relation:
        if kind not in SUPPORTED_RELATION_KINDS:
            raise RelationKindUnsupported(
                kind, "azuredevops", SUPPORTED_RELATION_KINDS
            )
        target_id = _parse_relation_target(target, project)
        _assert_not_self_relation(ticket_id, target_id)
        rel_name = _RELATION_WRITE[kind]
        target_url = _build_workitem_api_url(project, target_id)
        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"

        # Pre-flight duplicate check: fetch current relations and raise
        # RelationAlreadyExists if the same rel+target already exists
        # (mirrors GitHub 422 / GitLab 409 natural behaviour).
        target_url_marker = f"/workItems/{target_id}"
        with _client(project, token) as c:
            preflight_resp = c.get(
                path, params=_api_version_params({"$expand": "Relations"})
            )
        _check(preflight_resp)
        existing_relations = (preflight_resp.json() or {}).get("relations") or []
        for existing_rel in existing_relations:
            if (
                existing_rel.get("rel") == rel_name
                and target_url_marker in (existing_rel.get("url") or "")
            ):
                raise RelationAlreadyExists(
                    kind=kind,
                    ticket_id=str(ticket_id),
                    target=f"#{target_id}",
                )

        patch = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {"rel": rel_name, "url": target_url},
            }
        ]
        with _client(project, token) as c:
            resp = c.patch(
                path,
                params=_api_version_params(),
                headers={"Content-Type": "application/json-patch+json"},
                json=patch,
            )
        _check(resp)

        # For duplicate_of: append body marker + close the source work item,
        # matching the GitHub and GitLab providers' _mark_duplicate_of helpers.
        # The relation-link PATCH above fires FIRST (most likely to fail) so
        # any error surfaces before we mutate the body or state.
        if kind == "duplicate_of":
            # Read the current work item to get description + type.
            src_path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
            with _client(project, token) as c:
                src_resp = c.get(src_path, params=_api_version_params())
            _check(src_resp)
            src_fields = (src_resp.json() or {}).get("fields") or {}
            current_html = src_fields.get("System.Description") or ""
            current_markdown = _html_to_markdown(current_html)
            wi_type = src_fields.get("System.WorkItemType") or ""
            markers = _marker_set(project)
            already_ai = has_ai_generated_marker(current_markdown, markers=markers)

            # Build the new body with "Duplicate of #N" prepended.
            dup_line = f"Duplicate of #{target_id}"
            body_without_marker = strip_leading_ai_marker(current_markdown, markers=markers)
            if dup_line not in body_without_marker:
                new_body_core = (
                    f"{dup_line}\n\n{body_without_marker}"
                    if body_without_marker
                    else dup_line
                )
            else:
                new_body_core = body_without_marker
            new_body = apply_body_marker(
                new_body_core, will_be_ai_generated=already_ai, markers=markers
            )

            # Resolve the "Completed"-category closed state for this work item type.
            closed_state = "Closed"  # safe fallback
            if wi_type:
                try:
                    states = self._states_for_type(project, token, wi_type)
                    for s in states:
                        if s.get("category") == "Completed":
                            closed_state = s.get("name") or "Closed"
                            break
                except Exception:  # noqa: BLE001
                    pass  # fall through to "Closed" fallback

            body_close_patch = [
                {
                    "op": "replace",
                    "path": "/fields/System.Description",
                    "value": _markdown_to_html(new_body),
                },
                {
                    "op": "replace",
                    "path": "/fields/System.State",
                    "value": closed_state,
                },
            ]
            with _client(project, token) as c:
                bc_resp = c.patch(
                    src_path,
                    params=_api_version_params(),
                    headers={"Content-Type": "application/json-patch+json"},
                    json=body_close_patch,
                )
            _check(bc_resp)

        # Surface the target's title + state so the Relation return
        # matches what `get_ticket` reports for the same link — agents
        # often display this immediately after add_relation succeeds.
        title_state = self._fetch_work_item_title_state(
            project, token, [str(target_id)]
        )
        title, state = title_state.get(str(target_id), ("", ""))
        return Relation(
            kind=kind,
            ticket_id=f"#{target_id}",
            title=title,
            url=_build_work_item_url(project, target_id),
            state=state,
            is_pull_request=False,
            resolved=True,
        )

    def remove_relation(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        kind: str,
        target: str,
    ) -> dict:
        if kind not in SUPPORTED_RELATION_KINDS:
            raise RelationKindUnsupported(
                kind, "azuredevops", SUPPORTED_RELATION_KINDS
            )
        target_id = _parse_relation_target(target, project)
        rel_name = _RELATION_WRITE[kind]
        target_url_marker = f"/workItems/{target_id}"

        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
        params = _api_version_params({"$expand": "Relations"})
        with _client(project, token) as c:
            resp = c.get(path, params=params)
        _check(resp)
        work_item = resp.json() or {}
        relations = work_item.get("relations") or []
        index = None
        for i, rel in enumerate(relations):
            if rel.get("rel") == rel_name and target_url_marker in (rel.get("url") or ""):
                index = i
                break
        if index is None:
            raise RelationNotFound(
                kind=kind,
                ticket_id=ticket_id,
                target=f"#{target_id}",
            )

        patch = [{"op": "remove", "path": f"/relations/{index}"}]
        with _client(project, token) as c:
            resp = c.patch(
                path,
                params=_api_version_params(),
                headers={"Content-Type": "application/json-patch+json"},
                json=patch,
            )
        _check(resp)

        # For duplicate_of: strip the "Duplicate of #N" body line and
        # reopen the source work item — inverse of add_relation's
        # prepend + close. Unconditional (not gated on the ticket
        # currently being in a "Completed" category), matching GitHub
        # (`state: "open"`) and GitLab (`state_event: "reopen"`).
        # The relations-array PATCH above fires FIRST (most likely to
        # fail) so any error surfaces before we mutate body or state.
        if kind == "duplicate_of":
            fields = work_item.get("fields") or {}
            current_html = fields.get("System.Description") or ""
            current_markdown = _html_to_markdown(current_html)
            wi_type = fields.get("System.WorkItemType") or ""
            markers = _marker_set(project)
            already_ai = has_ai_generated_marker(current_markdown, markers=markers)

            # Strip the exact "Duplicate of #<target_id>" line (and a
            # trailing blank line, mirroring add_relation's prepend
            # format). (?!\d) guards against a partial-id match, e.g.
            # removing "#9" must not also eat "#90".
            dup_line = f"Duplicate of #{target_id}"
            body_core = strip_leading_ai_marker(current_markdown, markers=markers)
            body_core = re.sub(
                rf"^{re.escape(dup_line)}(?!\d)\n?(?:\n)?",
                "",
                body_core,
                flags=re.MULTILINE,
            )
            body_core = re.sub(r"\n{3,}", "\n\n", body_core).strip()
            new_body = apply_body_marker(
                body_core, will_be_ai_generated=already_ai, markers=markers
            )

            # Resolve the open state to reopen into. Tolerate a failed
            # states lookup (e.g. unknown work-item type) and fall back
            # to "New" rather than raising — the relation removal has
            # already succeeded at this point.
            open_state = "New"
            if wi_type:
                try:
                    states = self._states_for_type(project, token, wi_type)
                    open_state = _default_open_state(states)
                except Exception:  # noqa: BLE001
                    pass  # fall through to "New" fallback

            reopen_patch = [
                {
                    "op": "replace",
                    "path": "/fields/System.Description",
                    "value": _markdown_to_html(new_body),
                },
                {
                    "op": "replace",
                    "path": "/fields/System.State",
                    "value": open_state,
                },
            ]
            with _client(project, token) as c:
                ro_resp = c.patch(
                    path,
                    params=_api_version_params(),
                    headers={"Content-Type": "application/json-patch+json"},
                    json=reopen_patch,
                )
            _check(ro_resp)

        return {"removed": True}

    # ---------- pipelines -------------------------------------------------

    def list_runs_for_branch(
        self,
        project: ProjectConfig,
        token: str | None,
        ref: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List builds filtered by branch.

        Returns ``(runs, resolved_refs)`` to mirror the tag/ticket shape:
        - ``([], [])`` — branch not found in repository
        - ``(runs, [ref])`` — branch exists (runs may be empty)
        """
        _validate_limit(limit)
        repo_id = self._resolve_repository_id(project, token)
        branch = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
        with _client(project, token) as c:
            exists = _resolve_ado_branch(c, project, repo_id, branch)
        if not exists:
            return [], []
        params = _api_version_params({"branchName": branch, "$top": max(1, limit)})
        runs = self._list_builds(project, token, params, status, limit)
        return runs, [ref]

    def list_runs_for_commit(
        self,
        project: ProjectConfig,
        token: str | None,
        sha: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List builds whose ``sourceVersion`` matches ``sha``.

        Returns ``(runs, resolved_refs)`` to mirror the tag/ticket shape:
        - ``([], [])`` — commit not found in the repository
        - ``([], [sha])`` — commit exists, no builds reference it
        - ``(runs, [sha])`` — commit exists and has matching builds
        """
        _validate_limit(limit)
        # ADO doesn't filter by sourceVersion server-side on the public
        # /builds list; fetch the last page and filter client-side.
        params = _api_version_params({"$top": 200})
        runs = self._list_builds(project, token, params, status, 200)
        filtered = [r for r in runs if r.head_sha == sha]
        result = filtered[: max(1, limit)]
        if result:
            return result, [sha]
        repo_id = self._resolve_repository_id(project, token)
        with _client(project, token) as c:
            exists = _resolve_ado_commit(c, project, repo_id, sha)
        return [], ([sha] if exists else [])

    def list_runs_for_tag(
        self,
        project: ProjectConfig,
        token: str | None,
        tag: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Returns `(runs, resolved_refs)`:
        - ``([], [])`` — tag not found in the repository
        - ``([], [tag])`` — tag exists, no builds reference it
        - ``(runs, [tag])`` — tag exists and has matching builds
        """
        branch = tag if tag.startswith("refs/") else f"refs/tags/{tag}"
        params = _api_version_params({"branchName": branch, "$top": max(1, limit)})
        runs = self._list_builds(project, token, params, status, limit)
        if runs:
            return runs, [tag]
        repo_id = self._resolve_repository_id(project, token)
        with _client(project, token) as c:
            exists = _resolve_ado_tag(c, project, repo_id, tag)
        return [], ([tag] if exists else [])

    def list_runs_for_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Returns `(runs, resolved_refs)`. `resolved_refs` is the list of
        `build/{id}` markers we walked from the work item's
        ArtifactLink relations — mirrors GitLab's `!{iid}` shape.
        """
        _validate_limit(limit)
        # Walk the work item's relations for ArtifactLink entries pointing
        # at Build artifacts.
        path = (
            f"{_project_scope(project)}/_apis/wit/workitems/"
            f"{quote(str(ticket_id), safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params({"$expand": "Relations"}))
        _check(resp)
        build_ids: list[int] = []
        for rel in (resp.json() or {}).get("relations") or []:
            if rel.get("rel") != "ArtifactLink":
                continue
            attrs = rel.get("attributes") or {}
            if attrs.get("name") != "Build":
                continue
            # url shape: `vstfs:///Build/Build/{id}`
            m = re.search(r"/Build/(\d+)$", rel.get("url") or "")
            if m:
                build_ids.append(int(m.group(1)))
        if not build_ids:
            return [], []
        runs: list[PipelineRun] = []
        resolved_refs: list[str] = []
        with _client(project, token) as c:
            for bid in build_ids[: max(1, limit)]:
                bresp = c.get(
                    f"{_project_scope(project)}/_apis/build/builds/{bid}",
                    params=_api_version_params(),
                )
                if bresp.status_code == 404:
                    continue
                _check(bresp)
                runs.append(_map_build_run(bresp.json(), project))
                resolved_refs.append(f"build/{bid}")
        return runs, resolved_refs

    def list_runs_recent(
        self,
        project: ProjectConfig,
        token: str | None,
        *,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """List the most recent builds, unfiltered by ref.

        Returns ``(runs, [])`` — the empty ``resolved_refs`` signals that
        no ref filter was applied.
        """
        _validate_limit(limit)
        params = _api_version_params({"$top": max(1, limit)})
        return self._list_builds(project, token, params, status, limit), []

    def _list_builds(
        self,
        project: ProjectConfig,
        token: str | None,
        params: dict,
        status: str,
        limit: int,
    ) -> list[PipelineRun]:
        _validate_limit(limit)
        if status and status != "all":
            params["statusFilter"] = status
        path = f"{_project_scope(project)}/_apis/build/builds"
        with _client(project, token) as c:
            resp = c.get(path, params=params)
        _check(resp)
        runs = [_map_build_run(raw, project) for raw in (resp.json().get("value") or [])]
        return runs[: max(1, limit)]

    def get_run(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
        include_failure_excerpt: bool = False,
    ) -> PipelineRun:
        if not str(run_id).strip().isdigit():
            raise AzureDevOpsError(
                404,
                f"pipeline '{project.id}#{run_id}' not found"
                f" — run_id must be numeric for Azure DevOps",
            )
        path = f"{_project_scope(project)}/_apis/build/builds/{quote(str(run_id), safe='')}"
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        try:
            _check(resp)
        except AzureDevOpsError as exc:
            if exc.status == 404:
                raise AzureDevOpsError(
                    404, f"pipeline '{project.id}#{run_id}' not found"
                ) from exc
            raise
        run = _map_build_run(resp.json(), project)
        if include_failure_excerpt and run.conclusion == "failure":
            run.failure = self._fetch_build_failure_context(
                project, token, str(run_id)
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

        Here `job_id` is the Azure DevOps build *log* id
        (`rec["log"]["id"]` on a timeline record), not the timeline
        record GUID — the same identifier already used internally by
        `_fetch_build_failure_context` to fetch its 120-line tail
        excerpt. This method hits the same
        `GET .../builds/{run_id}/logs/{job_id}` endpoint but returns the
        full response body, unsliced. Raises `AzureDevOpsError` on a
        non-success response (via `_check`), or
        `AzureDevOpsError(404, ...)` when `run_id` is not numeric.
        """
        if not str(run_id).strip().isdigit():
            raise AzureDevOpsError(
                404,
                f"pipeline '{project.id}#{run_id}' not found"
                f" — run_id must be numeric for Azure DevOps",
            )
        log_path = (
            f"{_project_scope(project)}/_apis/build/builds/"
            f"{quote(str(run_id), safe='')}/logs/{job_id}"
        )
        with _client(project, token) as c:
            resp = c.get(log_path, params=_api_version_params())
        _check(resp)
        return resp.text

    def _fetch_build_failure_context(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
    ) -> PipelineFailure:
        path = (
            f"{_project_scope(project)}/_apis/build/builds/"
            f"{quote(str(run_id), safe='')}/timeline"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        if resp.status_code in (403, 404):
            return PipelineFailure(failing_jobs=[], note="timeline unavailable")
        _check(resp)
        records = (resp.json() or {}).get("records") or []
        failing: list[FailingJob] = []
        with _client(project, token) as c:
            for rec in records:
                if rec.get("result") != "failed":
                    continue
                if rec.get("type") not in ("Job", "Task"):
                    continue
                log_excerpt = None
                log_ref = rec.get("log") or {}
                log_id = log_ref.get("id")
                if log_id:
                    log_path = (
                        f"{_project_scope(project)}/_apis/build/builds/"
                        f"{quote(str(run_id), safe='')}/logs/{log_id}"
                    )
                    try:
                        lresp = c.get(log_path, params=_api_version_params())
                        if lresp.is_success:
                            text = lresp.text
                            log_excerpt = "\n".join(text.splitlines()[-120:])
                    except httpx.HTTPError:
                        log_excerpt = None
                failing.append(
                    FailingJob(
                        name=rec.get("name") or "",
                        url=(log_ref.get("url") or ""),
                        failed_step=rec.get("name") or "",
                        annotations=_normalize_az_issues(rec),
                        log_excerpt=log_excerpt,
                        job_id=str(log_id or ""),
                    )
                )
        return PipelineFailure(
            failing_jobs=failing,
            note=None if failing else "no failed records on timeline",
        )

    # ---------- token capabilities ----------------------------------------

    def probe_token_capabilities(
        self, project: ProjectConfig, token: str
    ) -> TokenCapabilities:
        if not token:
            return TokenCapabilities(reason="bad_credentials")
        try:
            with _client(project, token) as c:
                # connectionData is still a preview API — the stable
                # marker is rejected by the gateway.
                resp = c.get(
                    f"{_org_scope(project)}/_apis/connectionData",
                    params={"api-version": "7.1-preview.1"},
                )
        except httpx.HTTPError:
            return TokenCapabilities(reason="network_error")
        if resp.status_code == 401:
            return TokenCapabilities(reason="bad_credentials")
        if resp.status_code in (403, 404):
            return TokenCapabilities(reason="repo_invisible_to_token")
        if not resp.is_success:
            return TokenCapabilities(reason="permissions_field_missing")
        # PAT scopes aren't enumerable through public REST. We can probe
        # individual write surfaces by doing zero-effect operations, but
        # that costs round-trips. Defer to the configured permissions
        # block — connectionData proves the token is valid for *this*
        # organization, which is the most useful capability bit.
        return TokenCapabilities(
            issues_create=True,
            issues_modify=True,
            pulls_create=True,
            pulls_modify=True,
            pulls_merge=True,
        )

    # ---------- token project discovery --------------------------------------

    def discover_projects(
        self, token: str, *, limit: int
    ) -> ProjectDiscoveryResult:
        """Enumerate all ADO repositories visible to *token*.

        Org resolution: if ``AZURE_DEVOPS_ORG`` is set, only that org is
        queried; otherwise the VSSPS accounts API is used to discover all
        orgs.  For each org every ADO project and its git repositories are
        fetched.  Collection stops at *limit* repos; when the cap is hit
        ``truncated=True`` is set on the result.

        This method MUST NOT raise on expected failure modes — all errors
        are captured and returned as ``reason`` strings.
        """
        if not token:
            return ProjectDiscoveryResult(projects=[], reason="bad_credentials")
        if limit <= 0:
            return ProjectDiscoveryResult(projects=[], truncated=False)

        # --- org resolution -------------------------------------------------
        hint = _org_hint(base_url=None)
        if hint:
            orgs: list[str] = [hint]
        else:
            orgs, discovery_reason = _discover_orgs_via_api(token)
            if discovery_reason:
                return ProjectDiscoveryResult(
                    projects=[], reason=discovery_reason
                )

        # --- collection loop ------------------------------------------------
        # We collect up to ``limit + 1`` raw tuples so we can distinguish
        # "hit the cap with items remaining" (truncated) from "naturally
        # exhausted all repos" (not truncated).  The extra entry is removed
        # before the capability probe stage.
        #
        # Each entry: (org, ado_project_name, repo_name, description)
        fetch_cap = limit + 1  # collect one extra to detect overflow
        collected: list[tuple[str, str, str, str]] = []

        # Track org-level enumeration success so we can distinguish
        # "zero repos found" from "every org's projects call failed".
        # Rule: if at least one org was successfully enumerated (its
        # /_apis/projects call returned a 2xx), the overall result is a
        # success even if that org had zero repos.  Only when NOTHING was
        # successfully enumerated do we surface a failure reason.
        orgs_succeeded = 0
        last_org_failure_reason: str | None = None

        outer_done = False
        for org in orgs:
            if outer_done:
                break
            # Build a per-org sentinel for _client (base URL = dev.azure.com)
            try:
                from lib_python_projects.models import ProjectConfig as _PC
                org_sentinel = _PC(
                    id="_disc",
                    provider="azuredevops",
                    path=f"{org}/_p/_r",
                )
            except Exception:
                # Org name fails validation — skip this org
                log.warning("discover_projects: org %r failed sentinel creation", org)
                last_org_failure_reason = "network_error"
                continue

            # Fetch ADO projects for the org
            try:
                with _client(org_sentinel, token) as c:
                    projects_resp = c.get(
                        f"/{org}/_apis/projects",
                        params={"api-version": "7.1"},
                    )
            except httpx.HTTPError:
                log.warning(
                    "discover_projects: network error fetching projects for org %r",
                    org,
                )
                last_org_failure_reason = "network_error"
                continue

            if not projects_resp.is_success:
                log.warning(
                    "discover_projects: HTTP %s fetching projects for org %r",
                    projects_resp.status_code,
                    org,
                )
                last_org_failure_reason = f"http_{projects_resp.status_code}"
                continue

            orgs_succeeded += 1
            ado_projects = projects_resp.json().get("value", [])

            for ado_proj in ado_projects:
                if outer_done:
                    break
                proj_name = ado_proj.get("name", "")
                if not proj_name:
                    continue

                # Fetch git repositories for this ADO project
                try:
                    with _client(org_sentinel, token) as c:
                        repos_resp = c.get(
                            f"/{org}/{proj_name}/_apis/git/repositories",
                            params={"api-version": "7.1"},
                        )
                except httpx.HTTPError:
                    log.warning(
                        "discover_projects: network error fetching repos for "
                        "%r/%r",
                        org,
                        proj_name,
                    )
                    continue

                if not repos_resp.is_success:
                    log.warning(
                        "discover_projects: HTTP %s fetching repos for %r/%r",
                        repos_resp.status_code,
                        org,
                        proj_name,
                    )
                    continue

                for repo in repos_resp.json().get("value", []):
                    repo_name = repo.get("name", "")
                    if not repo_name:
                        continue
                    description = repo.get("remoteUrl", "")
                    collected.append((org, proj_name, repo_name, description))
                    if len(collected) >= fetch_cap:
                        outer_done = True
                        break

        # Determine truncation: we fetched one extra to detect overflow.
        truncated = len(collected) > limit
        if truncated:
            collected = collected[:limit]

        # Contract enforcement: if nothing was collected AND no org was
        # successfully enumerated, surface the failure reason so the caller
        # can distinguish "token sees zero repos" from "every org errored".
        if not collected and orgs_succeeded == 0 and last_org_failure_reason:
            return ProjectDiscoveryResult(
                projects=[], reason=last_org_failure_reason
            )

        # --- capability probe -----------------------------------------------
        discovered: list[DiscoveredProject] = []
        for org, proj_name, repo_name, description in collected:
            path = f"{org}/{proj_name}/{repo_name}"
            try:
                from lib_python_projects.models import ProjectConfig as _PC
                cfg = _PC(
                    id="_disc",
                    provider="azuredevops",
                    path=path,
                )
            except Exception:
                log.warning(
                    "discover_projects: path %r failed ProjectConfig validation",
                    path,
                )
                continue

            caps = self.probe_token_capabilities(cfg, token)
            discovered.append(
                DiscoveredProject(
                    provider="azuredevops",
                    path=path,
                    permissions=caps,
                    description=description,
                )
            )

        return ProjectDiscoveryResult(projects=discovered, truncated=truncated)

    # ---------- label management ---------------------------------------------

    def list_labels(
        self,
        project: ProjectConfig,
        token: str | None,
    ) -> list[Label]:
        """List tags actually applied to the project's work items (best-effort).

        Azure DevOps tags are implicit — they have no dedicated create/
        rename/delete API and carry no colour or description metadata.

        This method derives the "in use" tag set from the project's work
        items rather than calling the org/project tag catalog (``GET
        /{org}/{project}/_apis/wit/tags``): the catalog also lists
        catalog-only tags — tags that exist in ADO's tag-picker history
        but are not currently applied to any work item — which leaked
        into the previous implementation's results (ticket #172).

        The approach: run a WIQL query scoped to `[System.TeamProject] =
        @project` to get every work-item id in the project, then reuse
        `_fetch_work_items_batch` (chunked at 200 ids/request) to pull
        `System.Tags` for each. The returned list is the sorted union of
        tags present on at least one work item.

        Note: two wrapper `ProjectConfig`s that map to the same
        organization/ado_project (differing only by `repository`) share
        one ADO team project and therefore, absent further scoping, would
        share work items — and this in-use tag list — since ADO has no
        finer native scope than the team project for tags/work items.
        `ProjectConfig.area_path` (ticket #172) closes that gap: when set,
        it scopes this query to that `System.AreaPath` sub-tree (`UNDER`,
        recursive) via the same `_effective_area_path` resolver
        `list_tickets` uses, so two such wrappers configured with distinct
        `area_path` values return distinct tag sets. Left unset, behaviour
        is unchanged — the query still spans the whole team project.

        Best-effort: if the WIQL query or the batch fetch returns a
        non-success response, returns `[]` rather than raising. Each
        `Label` is returned with `color=""` and `description=""` because
        ADO tags have no such fields.
        """
        area, area_recursive = _effective_area_path(project, TicketFilters())
        area_clause = _area_path_clause(area, area_recursive)
        wiql = "SELECT [System.Id] FROM workitems WHERE [System.TeamProject] = @project"
        if area_clause:
            wiql += f" AND {area_clause}"
        with _client(project, token) as c:
            resp = c.post(
                f"{_project_scope(project)}/_apis/wit/wiql",
                params=_api_version_params(),
                json={"query": wiql},
            )
        if not resp.is_success:
            return []
        ids = [
            int(item.get("id"))
            for item in (resp.json().get("workItems") or [])
            if item.get("id") is not None
        ]
        if not ids:
            return []
        try:
            tickets = self._fetch_work_items_batch(project, token, ids)
        except AzureDevOpsError:
            return []
        tags: set[str] = set()
        for ticket in tickets:
            tags.update(ticket.labels)
        return [Label(name=name, color="", description="") for name in sorted(tags)]

    def create_label(
        self,
        project: ProjectConfig,
        token: str | None,
        name: str,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Not supported on Azure DevOps.

        ADO tags are implicit — they emerge when applied to a work item
        and have no dedicated create API. Raises `LabelOperationUnsupported`
        immediately without making any HTTP call.
        """
        raise LabelOperationUnsupported("create_label", "azuredevops")

    def update_label(
        self,
        project: ProjectConfig,
        token: str | None,
        name: str,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Not supported on Azure DevOps.

        ADO tags have no rename/recolour API. Raises
        `LabelOperationUnsupported` immediately without making any HTTP call.
        """
        raise LabelOperationUnsupported("update_label", "azuredevops")

    def delete_label(
        self,
        project: ProjectConfig,
        token: str | None,
        name: str,
    ) -> None:
        """Not supported on Azure DevOps.

        ADO tags have no delete API. Raises `LabelOperationUnsupported`
        immediately without making any HTTP call.
        """
        raise LabelOperationUnsupported("delete_label", "azuredevops")


# ---------- module-level helpers used by the provider ----------------------


def _escape_wiql(value: str) -> str:
    """Escape a single quote in a WIQL string literal."""
    return value.replace("'", "''")


def _board_column_wiql_clauses(
    project: ProjectConfig, board_column: str,
) -> list[str]:
    """Build the WIQL clause(s) for `TicketFilters.board_column` (#119).

    Config-driven — no live board API round-trip (mirrors the
    `area_path` "no validation round-trip" pattern in `_build_wiql`).
    Validates `project.board` context per design decision D3 and raises
    `ValueError` when it's missing rather than silently ignoring the
    filter.

    Always emits `[System.BoardColumn] = '<native>'`. Additionally
    applies the Doing/Done split (D2), inferred purely from the binding's
    `map` + `provider_extras["split_done_column"]`:
      - the logical column named in `split_done_column` (the "done" half)
        gets `AND [System.BoardColumnDone] = true`.
      - a sibling logical column that resolves to the same native column
        name (the "doing" half) gets `AND [System.BoardColumnDone] = false`.
      - a column that uniquely owns its native value (non-split) gets no
        `BoardColumnDone` clause at all.
    """
    board = project.board
    if board is None:
        raise ValueError(
            f"project {project.id!r} has no 'board' configuration — "
            f"board_column requires one (use 'status'/'states' matching "
            f"System.State as a fallback filter instead)"
        )
    binding = board.binding
    if binding.kind != "azure-boards":
        raise ValueError(
            f"project {project.id!r} board binding is {binding.kind!r}, "
            f"not 'azure-boards' — board_column filtering on Azure DevOps "
            f"requires an azure-boards binding (use 'status'/'states' "
            f"matching System.State as a fallback filter instead)"
        )
    if not binding.team or not binding.board:
        raise ValueError(
            f"project {project.id!r} azure-boards binding is missing "
            f"'team' and/or 'board' — both are required to filter by "
            f"board_column (use 'status'/'states' matching System.State "
            f"as a fallback filter instead)"
        )
    columns_lower = {c.lower() for c in board.columns}
    if board_column.lower() not in columns_lower:
        raise ValueError(
            f"board_column {board_column!r} is not one of this project's "
            f"board columns {board.columns!r}"
        )
    native_column = board.resolve(board_column)
    clauses = [f"[System.BoardColumn] = '{_escape_wiql(native_column)}'"]

    split_done_column = binding.provider_extras.get("split_done_column")
    if split_done_column:
        if board_column.lower() == str(split_done_column).lower():
            clauses.append("[System.BoardColumnDone] = true")
        else:
            split_native = board.resolve(str(split_done_column))
            if split_native == native_column:
                clauses.append("[System.BoardColumnDone] = false")
    return clauses


def _build_workitem_api_url(project: ProjectConfig, work_item_id: str) -> str:
    """Return the canonical REST URL for a work item (used in relation refs)."""
    base = (project.base_url or "https://dev.azure.com").rstrip("/")
    org = project.organization or ""
    return f"{base}/{org}/_apis/wit/workItems/{work_item_id}"


def _parse_relation_target(target: str, project: ProjectConfig) -> str:
    """Extract the bare work-item id from a relation target.

    Accepts the same shapes as GitHub's `_parse_relation_target`:
      - `"123"` / `"#123"` / `"  #123  "`
      - `"org/project/repo#123"` → rejected with NotImplementedError
        to match GitHub/GitLab cross-project parity
    """
    stripped = (target or "").strip()
    if not stripped:
        raise ValueError("relation target is empty")
    if "/" in stripped and ("#" in stripped or "!" in stripped):
        raise NotImplementedError(
            "azuredevops: cross-project relation targets are not "
            f"supported yet (got {target!r})"
        )
    if stripped.startswith("#"):
        stripped = stripped[1:].strip()
    if not stripped.isdigit():
        raise ValueError(
            f"relation target {target!r} could not be parsed — expected "
            "a bare work-item number or '#N'"
        )
    return stripped


_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
_CLOSING_KEYWORDS_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
    re.IGNORECASE,
)


def _scan_refs_for_mentions(text: str) -> list[tuple[str, str]]:
    """Yield `(kind, id)` tuples for mentions / closing keywords in `text`.

    Same heuristic as `github._scan_refs`: closing keywords (`close`,
    `fix`, `resolve` and variants) emit `closes`; plain `#N` emits
    `mentions`. De-duplicated, closes-wins-over-mentions semantics.
    """
    if not text:
        return []
    closes: set[str] = set()
    for m in _CLOSING_KEYWORDS_RE.finditer(text):
        closes.add(m.group(1))
    mentions: set[str] = set()
    for m in _REF_RE.finditer(text):
        if m.group(1) not in closes:
            mentions.add(m.group(1))
    out: list[tuple[str, str]] = []
    for cid in sorted(closes):
        out.append(("closes", cid))
    for mid in sorted(mentions):
        out.append(("mentions", mid))
    return out


__all__ = [
    "AzureDevOpsError",
    "AzureDevOpsProvider",
    "SUPPORTED_RELATION_KINDS",
]
