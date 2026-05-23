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
    FailingJob,
    PRFilters,
    PipelineFailure,
    PipelineRun,
    PullRequest,
    Relation,
    RelationKindUnsupported,
    Review,
    ReviewComment,
    Status,
    StatusSpec,
    Ticket,
    TicketFilters,
    TokenCapabilities,
    TokenCapabilityProvider,
    WRITABLE_RELATION_KINDS,
    normalize_timestamp,
)

log = logging.getLogger("project-issues.azuredevops")

USER_AGENT = "claude-code-project-issues-plugin/0.1.0"
API_VERSION = "7.1"
# Comments live on the preview-marked endpoint; the GA version drops the
# suffix once Microsoft promotes it.
API_VERSION_COMMENTS = "7.1-preview.4"


# ---------- error type -------------------------------------------------------


class AzureDevOpsError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Azure DevOps {status}: {message}")
        self.status = status
        self.message = message


# ---------- client + error mapping ------------------------------------------


def _basic_auth_header(token: str) -> str:
    raw = f":{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _client(project: ProjectConfig, token: str | None) -> httpx.Client:
    """Build an httpx.Client targeted at the ADO REST root.

    Base URL is `project.base_url` if set (covers self-hosted Azure
    DevOps Server installations), otherwise `https://dev.azure.com`.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = _basic_auth_header(token)
    base = (project.base_url or "https://dev.azure.com").rstrip("/")
    return httpx.Client(base_url=base, headers=headers, timeout=30.0)


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


def _check(resp: httpx.Response) -> None:
    """Translate a non-success ADO response into an `AzureDevOpsError`.

    Two extra translations on top of the raw envelope:
      - ADO returns 400 for several "not found" classes (deleted work
        items, missing PR refs, unknown work-item types). We re-tag
        those as 404 so the tool-layer `_rewrap_404` adds the
        `kind 'project#id' not found` context.
      - Work-item state-transition 400s get a hint pointing at
        `list_ticket_statuses`, mirroring the GitHub/GitLab providers.
    """
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

    # Status-transition hint parity with GitHub/GitLab. ADO surfaces
    # invalid System.State values under a handful of typeKeys with
    # message fragments that all boil down to "your value isn't in the
    # allowed list" — match any of them so the hint reliably fires.
    if (
        status in (400, 409)
        and (
            type_key in _TRANSITION_TYPE_KEYS
            or any(frag in msg_lower for frag in _TRANSITION_MSG_FRAGMENTS)
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
            0,
            f"project '{project.id}': missing organization/project in "
            f"path {project.path!r}",
        )
    return f"/{quote(org, safe='')}/{quote(proj, safe='')}"


def _org_scope(project: ProjectConfig) -> str:
    """Return the `/{org}` URL prefix used by org-wide endpoints."""
    org = project.organization
    if not org:
        raise AzureDevOpsError(
            0,
            f"project '{project.id}': missing organization in path "
            f"{project.path!r}",
        )
    return f"/{quote(org, safe='')}"


def _api_version_params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the common query-param dict, defaulting `api-version` to 7.1."""
    params: dict[str, Any] = {"api-version": API_VERSION}
    if extra:
        params.update(extra)
    return params


# ---------- caches (org/project-scoped, with TTL) ----------------------------


_CACHE_TTL_SECONDS = 60 * 60  # 1 hour, matching tools/tickets list_ticket_statuses cache
_cache_lock = threading.Lock()
_repo_id_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_state_cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_default_type_cache: dict[tuple[str, str], tuple[float, str]] = {}


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
        _default_type_cache.clear()


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
            joined = "<br>\n".join(_inline_md(line) for line in para)
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

    # Now run the rest through link → bold → italic on the *escaped* form,
    # using sentinels so the angle brackets we emit aren't double-escaped.
    rest = _LINK_RE.sub(
        lambda mm: f"\x01A\x01{html_escape(mm.group(2))}\x01B\x01"
        f"{html_escape(mm.group(1))}\x01C\x01",
        rest,
    )
    rest = _BOLD_RE.sub(lambda mm: f"\x01D\x01{html_escape(mm.group(1))}\x01E\x01", rest)
    rest = _ITALIC_RE.sub(lambda mm: f"\x01F\x01{html_escape(mm.group(1))}\x01G\x01", rest)
    # Whatever survives is literal text that still needs escaping.
    rest = (
        html_escape(rest)
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
            if not self._in_pre:
                self._emit("`")
        elif tag == "pre":
            self._in_pre += 1
            self._emit("\n```\n")
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
    """Inverse of `_label_list_from_tags` — deduplicates, sorts, joins."""
    seen: list[str] = []
    for lbl in labels:
        if lbl and lbl not in seen:
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
        status=state,
        author=_identity_display_name(fields.get("System.CreatedBy")),
        assignees=assignees,
        labels=labels,
        url=_build_work_item_url(project, raw_id) if raw_id is not None else "",
        created_at=normalize_timestamp(fields.get("System.CreatedDate") or ""),
        updated_at=normalize_timestamp(fields.get("System.ChangedDate") or ""),
    )


def _map_work_item_comment(raw: dict, project: ProjectConfig, work_item_id: str) -> Comment:
    """Translate an entry from `/_apis/wit/workItems/{id}/comments` into Comment."""
    return Comment(
        id=str(raw.get("id", "")),
        author=_identity_display_name(raw.get("createdBy")),
        body=_html_to_markdown(raw.get("text") or ""),
        url=_build_work_item_url(project, work_item_id) + f"?commentId={raw.get('id', '')}",
        created_at=normalize_timestamp(raw.get("createdDate") or ""),
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

    # ADO doesn't expose a commit SHA on the thread itself. The
    # iteration tracking only carries iteration indices; resolving them
    # to SHAs needs an extra `/iterations` round-trip per thread, which
    # is too expensive for `list_pr_review_comments`. Leave `None` when
    # not derivable rather than fabricating from an unrelated field.
    commit_sha_raw = (
        (thread.get("pullRequestThreadContext") or {})
        .get("changeTrackingId")
    )
    commit_sha = str(commit_sha_raw) if isinstance(commit_sha_raw, str) else None

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
        author=_identity_display_name(raw.get("author")),
        body=_html_to_markdown(raw.get("content") or ""),
        url=_build_pr_url(project, pr_id) + f"?discussionId={thread_id}",
        created_at=normalize_timestamp(raw.get("publishedDate") or ""),
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


# ---------- relation kind mapping -------------------------------------------


_RELATION_FORWARD: dict[str, str] = {
    "parent": "System.LinkTypes.Hierarchy-Reverse",
    "child": "System.LinkTypes.Hierarchy-Forward",
    "blocks": "System.LinkTypes.Dependency-Forward",
    "blocked_by": "System.LinkTypes.Dependency-Reverse",
    "duplicate_of": "System.LinkTypes.Duplicate-Forward",
    "relates_to": "System.LinkTypes.Related",
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


# ---------- the provider class ----------------------------------------------


class AzureDevOpsProvider(TokenCapabilityProvider):
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
        # first value if every state is terminal.
        opens = [s.get("name") for s in states if s.get("category") in _open_state_categories()]
        default_open = opens[0] if opens else (values[0] if values else "")

        # Process templates without a "Removed" category (notably Basic)
        # don't have a distinct declined state — surface that honestly as
        # an empty string rather than collapsing it onto
        # terminal_completed, which previously left both fields equal
        # and agents thinking they had two interchangeable terminal
        # states to choose from.
        terminal = (by_cat.get("Completed") or []) + (by_cat.get("Removed") or [])
        terminal_completed = by_cat.get("Completed") or []
        terminal_declined = by_cat.get("Removed") or []
        hints: dict[str, str | list[str]] = {
            "default_open": default_open,
            "terminal": terminal,
            "terminal_completed": terminal_completed[0] if terminal_completed else "",
            "terminal_declined": terminal_declined[0] if terminal_declined else "",
        }
        return StatusSpec(values=values, transitions=transitions, hints=hints)

    # ---------- tickets — read --------------------------------------------

    def _build_wiql(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> str:
        clauses: list[str] = ["[System.TeamProject] = @project"]
        # Map ListStatus to state-category sets discovered for the default
        # work-item type. We can't filter by category in WIQL directly,
        # but we can enumerate the states.
        if filters.status != "any":
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
    ) -> list[Ticket]:
        wiql = self._build_wiql(project, token, filters)
        with _client(project, token) as c:
            resp = c.post(
                f"{_project_scope(project)}/_apis/wit/wiql",
                params=_api_version_params({"$top": max(1, filters.limit)}),
                json={"query": wiql},
            )
        _check(resp)
        ids = [
            int(item.get("id"))
            for item in (resp.json().get("workItems") or [])
            if item.get("id") is not None
        ][: max(1, filters.limit)]
        if not ids:
            return []
        return self._fetch_work_items_batch(project, token, ids)

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
    ) -> tuple[Ticket, list[Comment], list[Relation], bool]:
        _validate_int32_id(ticket_id, "ticket")
        params = _api_version_params({"$expand": "Relations"})
        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
        with _client(project, token) as c:
            resp = c.get(path, params=params)
        _check(resp)
        raw = resp.json()
        ticket = _map_work_item(raw, project)

        # Comments are a separate endpoint.
        comments = self._list_work_item_comments(
            project, token, ticket_id, limit=200, order="asc"
        )

        relations: list[Relation] = []
        truncated = False
        if include_relations:
            relations = self._build_relations_from_work_item(
                project, token, raw, ticket_id
            )

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
        mention_targets: list[tuple[str, str]] = []
        for ref_kind, ref_id in _scan_refs_for_mentions(body_text):
            if ref_id == str(ticket_id):
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
        """
        if not ids:
            return {}
        body = {
            "ids": [int(i) for i in ids if i.isdigit()],
            "fields": ["System.Title", "System.State"],
        }
        if not body["ids"]:
            return {}
        path = f"{_project_scope(project)}/_apis/wit/workitemsbatch"
        try:
            with _client(project, token) as c:
                resp = c.post(path, params=_api_version_params(), json=body)
            if not resp.is_success:
                # Best-effort: empty titles beat blowing up the whole
                # get_ticket path. The relation array still surfaces.
                return {}
            value = (resp.json() or {}).get("value") or []
        except httpx.HTTPError:
            return {}
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
        """
        wi_type = self._default_work_item_type(project, token)
        body_with_marker = ensure_body_prefix(body or "")
        merged_labels = sorted(set([*labels, AI_GENERATED_LABEL]))

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
                return self.update_ticket(
                    project, token, str(created.id), status=status
                )
            except Exception as exc:  # AzureDevOpsError or httpx errors
                raise AzureDevOpsError(
                    getattr(exc, "status", 400),
                    f"work item #{created.id} created but state "
                    f"transition to '{status}' failed: {exc}",
                ) from exc
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
    ) -> Ticket:
        _validate_int32_id(ticket_id, "ticket")
        # Read current work item (need tags + assignee for diff semantics).
        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        current = resp.json()
        cur_fields = current.get("fields") or {}

        patch: list[dict] = []
        if title is not None:
            patch.append({"op": "replace", "path": "/fields/System.Title", "value": title})
        if body is not None:
            already_ai = has_ai_generated_marker(
                _html_to_markdown(cur_fields.get("System.Description") or "")
            )
            new_body = apply_body_marker(body, will_be_ai_generated=already_ai)
            patch.append({
                "op": "replace",
                "path": "/fields/System.Description",
                "value": _markdown_to_html(new_body),
            })
        if status is not None:
            patch.append({"op": "replace", "path": "/fields/System.State", "value": status})

        # Labels: read-modify-write the System.Tags string.
        if labels_add or labels_remove:
            current_labels = set(_label_list_from_tags(cur_fields.get("System.Tags")))
            for lbl in labels_add or []:
                current_labels.add(lbl)
            for lbl in labels_remove or []:
                current_labels.discard(lbl)
            # Always stamp the `ai-modified` marker on a write that doesn't
            # carry `ai-generated` already (parity with github.py).
            already_ai_label = AI_GENERATED_LABEL in current_labels
            if not already_ai_label:
                current_labels.add(AI_MODIFIED_LABEL)
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
            # mirrors `github._split_github_status` so the `_safe`
            # translation produces a clean `{"error": "ticket #5 state
            # 'Bogus' rejected — accepted: [...]"}` payload without the
            # raw "Azure DevOps 400:" provider prefix.
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
                    f" — accepted: {accepted}" if accepted else ""
                )
                raise ValueError(
                    f"ticket #{ticket_id} state {status!r} rejected"
                    f"{accepted_clause}"
                ) from exc
            raise
        return _map_work_item(resp.json(), project)

    # ---------- comments — work item --------------------------------------

    def add_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        body: str,
    ) -> Comment:
        body_with_marker = ensure_comment_prefix(body or "")
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
        _check(resp)
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
        if order == "desc":
            collected.reverse()
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
                0,
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
                0,
                "azuredevops.update_comment requires ticket_id "
                "(work-item comment ids are scoped to a work item).",
            )
        _validate_int32_id(ticket_id, "ticket")
        _validate_int32_id(comment_id, "comment")
        # Read current to decide the marker flavour.
        path = (
            f"{_project_scope(project)}/_apis/wit/workItems/"
            f"{quote(str(ticket_id), safe='')}/comments/{quote(str(comment_id), safe='')}"
        )
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params({"api-version": API_VERSION_COMMENTS}))
        _check(resp)
        cur_markdown = _html_to_markdown((resp.json() or {}).get("text") or "")
        already_ai = has_ai_generated_marker(cur_markdown)
        new_body = apply_body_marker(body, will_be_ai_generated=already_ai)
        with _client(project, token) as c:
            resp = c.patch(
                path,
                params=_api_version_params({"api-version": API_VERSION_COMMENTS}),
                json={"text": _markdown_to_html(new_body)},
            )
        _check(resp)
        return _map_work_item_comment(resp.json(), project, str(ticket_id))

    # ---------- pull requests — read --------------------------------------

    def list_prs(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: PRFilters,
    ) -> list[PullRequest]:
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
        prs = [_map_pr(raw, project) for raw in (resp.json().get("value") or [])]
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
        return prs[: max(1, filters.limit)]

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
        pr = _map_pr(resp.json(), project)
        # ADO's single-PR GET doesn't include labels by default; the
        # list-PRs endpoint does. Fetch the labels endpoint so every
        # PR-returning surface (create / update / get / merge_pr)
        # advertises labels consistently.
        pr.labels = self._fetch_pr_labels(project, token, repo_id, pr_id)
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
    ) -> PullRequest:
        """Create a pull request, applying the AI-generated marker + label.

        Mirrors `github.py:create_pr`: the body always gets the
        `#ai-generated` marker prefix and the `ai-generated` label is
        appended to the user-supplied labels (de-duped, preserving the
        caller's order). Label application is best-effort — a 403/404
        from `_add_pr_label` is swallowed so a missing tag-management
        permission doesn't kill the legitimate PR.
        """
        repo_id = self._resolve_repository_id(project, token)
        body_with_marker = ensure_body_prefix(body or "")
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
        # Mirror github.py:create_pr — always merge the ai-generated
        # label, dropping caller duplicates while preserving their order.
        merged_labels = list(dict.fromkeys([*(labels or []), AI_GENERATED_LABEL]))
        if merged_labels:
            for lbl in merged_labels:
                self._add_pr_label_best_effort(project, token, repo_id, pr.id, lbl)
            pr, _ = self.get_pr(project, token, pr.id)
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
          - The body always carries a marker line; `#ai-generated` is
            preserved when the PR was originally agent-authored,
            otherwise `#ai-modified` is stamped.
          - When the existing PR does not already carry the
            `ai-generated` label, the `ai-modified` label is added
            (best-effort, swallowing 403/404).

        Limitation: ADO has no native `updated_at` field on PRs, so we
        synthesize one (current UTC) whenever this call performs any
        write — the returned `pr.updated_at` therefore reflects "this
        call mutated the PR" rather than ADO's persisted state.
        """
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
        already_ai = has_ai_generated_marker(cur_md) or (
            AI_GENERATED_LABEL in cur_labels
        )

        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            new_body = apply_body_marker(body, will_be_ai_generated=already_ai)
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

        # Auto-apply `ai-modified` when the PR wasn't originally agent-
        # authored AND that label isn't already in the requested deltas.
        # Best-effort: a missing tag-management permission doesn't kill
        # the legitimate update.
        explicit_label_names = set(labels_add or []) | set(labels_remove or [])
        if (
            not already_ai
            and AI_MODIFIED_LABEL not in cur_labels
            and AI_MODIFIED_LABEL not in explicit_label_names
        ):
            if self._add_pr_label_best_effort(
                project, token, repo_id, pr_id, AI_MODIFIED_LABEL
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
        body_with_marker = ensure_comment_prefix(body or "")
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
        # Validate before doing any network I/O so callers fail fast.
        if not in_reply_to and (not path or line is None):
            raise AzureDevOpsError(
                0,
                "azuredevops.add_pr_review_comment requires either "
                "in_reply_to (reply to existing thread) or path+line "
                "(create a new diff-anchored thread).",
            )
        repo_id = self._resolve_repository_id(project, token)
        body_with_marker = ensure_comment_prefix(body or "")

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

        payload = {
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

        Identity, timestamp, and id are synthesized to match the
        cross-provider `Review` shape: `author` is the reviewer's
        unique-name (UPN/email, the closest analogue of a GitHub login),
        `submitted_at` is a current UTC ISO-8601, and `id` includes the
        vote + ms-timestamp so consecutive `comment + approve` calls
        from the same reviewer don't collide.
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
                0,
                "could not resolve current user id for PR review submission",
            )

        rev_path = (
            f"{_project_scope(project)}/_apis/git/repositories/"
            f"{quote(repo_id, safe='')}/pullrequests/{quote(str(pr_id), safe='')}"
            f"/reviewers/{quote(reviewer_id, safe='')}"
        )
        with _client(project, token) as c:
            resp = c.put(rev_path, params=_api_version_params(), json={"vote": vote})
        _check(resp)
        # The reviewer PUT response carries the full identity
        # (displayName + uniqueName), while connectionData often returns
        # only the GUID. Prefer the PUT response, falling back to
        # connectionData so a missing field doesn't drop us to the GUID.
        reviewer_identity = resp.json() or {}
        merged_identity = {**identity, **reviewer_identity}

        body_with_marker = ensure_comment_prefix(body) if body else ""
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

        normalized_state: Any = (
            state if state in ("approve", "request_changes", "comment") else "comment"
        )
        submitted_at = _utc_iso_now()
        synthesized_id = (
            f"{reviewer_id}:{vote}:{int(time.time() * 1000)}"
        )
        return Review(
            id=synthesized_id,
            state=normalized_state,
            # `_identity_display_name` keeps the author field
            # consistent with `_map_pr` (PR-level author) and
            # `_map_thread_comment` (PR comment author) — all three
            # surfaces use the same display-name shape on Azure, so an
            # agent that compares them sees a single identity string
            # per user. Cross-provider, GitHub still returns its login;
            # Azure returns the display name (its natural primary key).
            author=_identity_display_name(merged_identity),
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
        rel_name = _RELATION_FORWARD[kind]
        target_url = _build_workitem_api_url(project, target_id)
        patch = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {"rel": rel_name, "url": target_url},
            }
        ]
        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
        with _client(project, token) as c:
            resp = c.patch(
                path,
                params=_api_version_params(),
                headers={"Content-Type": "application/json-patch+json"},
                json=patch,
            )
        _check(resp)
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
        rel_name = _RELATION_FORWARD[kind]
        target_url_marker = f"/workItems/{target_id}"

        path = f"{_project_scope(project)}/_apis/wit/workitems/{quote(str(ticket_id), safe='')}"
        params = _api_version_params({"$expand": "Relations"})
        with _client(project, token) as c:
            resp = c.get(path, params=params)
        _check(resp)
        relations = (resp.json() or {}).get("relations") or []
        index = None
        for i, rel in enumerate(relations):
            if rel.get("rel") == rel_name and target_url_marker in (rel.get("url") or ""):
                index = i
                break
        if index is None:
            raise LookupError(
                f"relation '{kind}' on ticket '#{ticket_id}' targeting "
                f"'#{target_id}' not found"
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
        return {"removed": True, "kind": kind, "target": f"#{target_id}"}

    # ---------- pipelines -------------------------------------------------

    def list_runs_for_branch(
        self,
        project: ProjectConfig,
        token: str | None,
        ref: str,
        status: str = "all",
        limit: int = 20,
    ) -> list[PipelineRun]:
        branch = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
        params = _api_version_params({"branchName": branch, "$top": max(1, limit)})
        return self._list_builds(project, token, params, status, limit)

    def list_runs_for_commit(
        self,
        project: ProjectConfig,
        token: str | None,
        sha: str,
        status: str = "all",
        limit: int = 20,
    ) -> list[PipelineRun]:
        # ADO doesn't filter by sourceVersion server-side on the public
        # /builds list; fetch the last page and filter client-side.
        params = _api_version_params({"$top": 200})
        runs = self._list_builds(project, token, params, status, 200)
        filtered = [r for r in runs if r.head_sha == sha]
        return filtered[: max(1, limit)]

    def list_runs_for_tag(
        self,
        project: ProjectConfig,
        token: str | None,
        tag: str,
        status: str = "all",
        limit: int = 20,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Returns `(runs, resolved_refs)`. `resolved_refs` is `[tag]`
        because ADO's `branchName` parameter accepts the ref directly
        without resolving to a commit SHA first.
        """
        branch = tag if tag.startswith("refs/") else f"refs/tags/{tag}"
        params = _api_version_params({"branchName": branch, "$top": max(1, limit)})
        runs = self._list_builds(project, token, params, status, limit)
        return runs, ([tag] if runs else [])

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

    def _list_builds(
        self,
        project: ProjectConfig,
        token: str | None,
        params: dict,
        status: str,
        limit: int,
    ) -> list[PipelineRun]:
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
        path = f"{_project_scope(project)}/_apis/build/builds/{quote(str(run_id), safe='')}"
        with _client(project, token) as c:
            resp = c.get(path, params=_api_version_params())
        _check(resp)
        run = _map_build_run(resp.json(), project)
        if include_failure_excerpt and run.conclusion == "failure":
            run.failure = self._fetch_build_failure_context(
                project, token, str(run_id)
            )
        return run

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
                        annotations=[],
                        log_excerpt=log_excerpt,
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


# ---------- module-level helpers used by the provider ----------------------


def _escape_wiql(value: str) -> str:
    """Escape a single quote in a WIQL string literal."""
    return value.replace("'", "''")


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
