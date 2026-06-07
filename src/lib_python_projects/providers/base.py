"""Provider abstraction — common types shared by GitHub/GitLab."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


_TIMESTAMP_FRACTION_RE = re.compile(
    r"(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+"
    r"(?P<tz>(?:Z|[+-]\d{2}:?\d{2}))?$"
)


def normalize_timestamp(raw: str | None) -> str:
    """Normalise a provider-returned timestamp to second precision (#49 finding 10).

    GitHub returns `"2026-05-20T23:07:48Z"` (seconds), GitLab returns
    `"2026-05-20T23:07:59.507Z"` (milliseconds). Both providers feed
    through this helper so the cross-provider surface looks identical
    and lexical string comparisons work without surprises.

    Anything not matching `<YYYY-MM-DDTHH:MM:SS><.fraction><tz>` is
    returned unchanged — we don't want to silently mangle vendor
    payloads that happen to encode time differently.
    """
    if not raw:
        return raw or ""
    m = _TIMESTAMP_FRACTION_RE.match(raw)
    if not m:
        return raw
    return f"{m.group('base')}{m.group('tz') or ''}"

# Provider-native status as a free string.
#
# Historically a 3-value enum (`open`/`completed`/`not_planned`) was used
# here, but that model could not represent Azure-DevOps workflows
# (`Resolved`, `Committed`, custom states, etc.). The string now flows
# through unchanged; agents discover valid values + semantic hints via
# `list_ticket_statuses`. GitHub uses a `state:state_reason` suffix
# encoding to preserve the `closed:completed` vs `closed:not_planned`
# distinction.
Status = str
ListStatus = Literal["open", "closed", "any"]


@dataclass
class Ticket:
    id: str               # provider-native id (issue.number / iid) as string
    title: str
    body: str
    status: Status
    author: str
    assignees: list[str]
    labels: list[str]
    url: str
    created_at: str       # ISO-8601 string
    updated_at: str


@dataclass
class Comment:
    id: str
    author: str
    body: str
    url: str
    created_at: str
    updated_at: str = ""


RelationKind = Literal[
    "parent",
    "child",
    "closes",
    "closed_by",
    "duplicate_of",
    "duplicated_by",
    "mentions",
    "mentioned_by",
    "blocks",
    "blocked_by",
    "relates_to",
]


# Kinds that the write-side `add_relation` / `remove_relation` tools
# accept. Read-only inverse kinds (`closed_by`, `duplicated_by`,
# `mentioned_by`, `mentions`) are not settable directly — they emerge
# from the other side of a write or from body content.
WRITABLE_RELATION_KINDS: tuple[str, ...] = (
    "parent",
    "child",
    "blocks",
    "blocked_by",
    "duplicate_of",
    "relates_to",
)

# Kinds that are read-only — they are surfaced by the provider from
# the other side of a write or from body/text scanning and cannot be
# set directly via `add_relation` / `remove_relation`.
READ_ONLY_RELATION_KINDS: tuple[str, ...] = (
    "closes",
    "closed_by",
    "duplicated_by",
    "mentions",
    "mentioned_by",
)


class RelationNotFound(LookupError):
    """Raised when `remove_relation` is called for a link that does not exist.

    Carries `kind`, `ticket_id`, and `target` so the agent can branch on
    the failure without parsing the message text. Subclass of `LookupError`
    so the tool-layer `_safe` wrapper translates it to `{"error": "..."}`.
    """

    def __init__(self, kind: str, ticket_id: str, target: str) -> None:
        self.kind = kind
        self.ticket_id = ticket_id
        self.target = target
        super().__init__(
            f"no {kind!r} relation on #{ticket_id} targeting {target!r} found to remove"
        )


class RelationAlreadyExists(ValueError):
    """Raised when `add_relation` is called for a link that already exists.

    Carries `kind`, `ticket_id`, and `target` so the agent can branch on
    the failure without parsing the message text. Subclass of `ValueError`
    so the tool-layer `_safe` wrapper translates it to `{"error": "..."}`.
    """

    def __init__(self, kind: str, ticket_id: str, target: str) -> None:
        self.kind = kind
        self.ticket_id = ticket_id
        self.target = target
        super().__init__(
            f"relation {kind!r} from #{ticket_id} to {target!r} already exists"
        )


def _assert_not_self_relation(ticket_id: str, target_number: str) -> None:
    """Raise ValueError when `ticket_id` and `target_number` refer to the same issue.

    Only fires for same-repo targets (no `/` in either argument).
    Both arguments are expected to be bare numeric strings (the `#` prefix
    must be stripped by the caller before passing in).

    Examples of matches that raise:
      ticket_id="5", target_number="5"
      ticket_id="5", target_number="#5" (caller strips "#" first — not needed,
      but harmless if the caller passes the raw form)

    Cross-repo targets (containing `/`) are never a self-relation even when
    the issue numbers coincide, so this helper is a no-op for them.
    """
    # Strip any leading "#" for robustness — callers vary slightly.
    t_num = ticket_id.lstrip("#").strip()
    r_num = target_number.lstrip("#").strip()
    if not t_num or not r_num:
        return
    # Cross-repo qualifiers: "owner/repo#N" — never a self-relation.
    if "/" in t_num or "/" in r_num:
        return
    if t_num == r_num:
        raise ValueError(
            f"self-relation: ticket and target are the same issue (#{t_num})"
        )


class RelationKindUnsupported(NotImplementedError):
    """Raised by a provider when `add_relation` / `remove_relation` is
    called with a relation kind that provider cannot model natively.

    Carries `kind`, `provider`, and `supported_kinds` so the agent can
    branch on the failure or surface a precise error to the user.
    Subclass of `NotImplementedError` so the generic `_safe` wrapper in
    `tools/_providers.py` translates it to `{"error": "..."}` without
    further plumbing.
    """

    def __init__(
        self,
        kind: str,
        provider: str,
        supported_kinds: tuple[str, ...] | list[str],
    ) -> None:
        self.kind = kind
        self.provider = provider
        self.supported_kinds = tuple(supported_kinds)
        super().__init__(
            f"relation kind {kind!r} is not supported on provider "
            f"{provider!r}; supported_kinds={list(supported_kinds)}"
        )


class LabelOperationUnsupported(NotImplementedError):
    """Raised by a provider when a label mutating operation is called but
    the provider cannot model that operation natively.

    Carries `operation` and `provider` string attributes so the agent can
    branch on the failure without parsing the message text. Subclass of
    `NotImplementedError` so the generic `_safe` wrapper in
    `tools/_providers.py` translates it to `{"error": "..."}` without
    further plumbing.

    Azure DevOps uses implicit tags with no create/rename/delete API, so
    `create_label`, `update_label`, and `delete_label` all raise this
    exception on that provider.
    """

    def __init__(self, operation: str, provider: str) -> None:
        self.operation = operation
        self.provider = provider
        super().__init__(
            f"label operation {operation!r} is not supported on provider "
            f"{provider!r}"
        )


@dataclass
class Label:
    """A repository label (tag) as returned by the provider API.

    `name` is the label's display name. `color` is the provider-native
    colour string — for GitHub a 6-hex string without `#` (e.g.
    ``"ededed"``), for GitLab a `#RRGGBB` string (e.g. ``"#ededed"``).
    Azure DevOps tags have no colour concept so `color` is always ``""``.

    `description` is an optional human-readable summary. Fields default
    to ``""`` so callers that receive partial payloads (Azure DevOps) can
    construct the object without conditional guards.
    """

    name: str = ""
    color: str = ""
    description: str = ""


@dataclass
class Relation:
    """A typed link between this ticket and another ticket / PR.

    `ticket_id` is `"#N"` for references within the same repository and
    `"owner/repo#N"` for cross-repo references. `state` is `"open"`,
    `"closed"`, `"merged"`, or `""` when the provider didn't report one.
    `is_pull_request` is true when the other side is a PR/MR. `title`
    is best-effort and may be empty if the provider didn't return it.

    When `resolved` is ``False``, the relation was derived from a
    body/text scan and the target was not independently fetched.
    In that case `title` and `state` will both be `""`.  This is the
    canonical "not fetched" sentinel; callers that need live metadata
    must resolve the relation themselves via the provider API.
    """

    kind: str
    ticket_id: str
    title: str
    url: str
    state: str
    is_pull_request: bool
    resolved: bool | None = None
    """Liveness of the target:
    - ``True``  — built from a live API response (target exists and was fetched).
    - ``False`` — built from a body/text scan (target not independently fetched).
    - ``None``  — not applicable or not set.
    """


SortBy = Literal["created", "updated", "comments"]
SortOrder = Literal["asc", "desc"]


@dataclass
class TicketFilters:
    status: ListStatus = "open"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    search: str | None = None
    limit: int = 30
    not_labels: list[str] = field(default_factory=list)
    author: str | None = None
    created_after: str | None = None
    created_before: str | None = None
    updated_after: str | None = None
    updated_before: str | None = None
    sort_by: SortBy = "created"
    sort_order: SortOrder = "desc"


def _validate_limit(limit: int, name: str = "limit") -> None:
    """Raise ValueError if `limit` is not a positive integer."""
    if limit <= 0:
        raise ValueError(f"{name} must be a positive integer, got {limit!r}")


def _validate_label_lists(
    labels_add: list[str] | None,
    labels_remove: list[str] | None,
) -> None:
    """Raise ValueError if the same label appears in both lists."""
    conflict = set(labels_add or []) & set(labels_remove or [])
    if conflict:
        raise ValueError(
            f"labels_add and labels_remove overlap — conflicting labels: {sorted(conflict)}"
        )


PRStatus = Literal["open", "closed", "merged"]
PRListStatus = Literal["open", "closed", "any"]


@dataclass
class PullRequest:
    """A pull-request snapshot mirroring `Ticket` but with PR-specific fields.

    `id` is the PR number as a string (mirrors `Ticket.id` style).
    `mergeable` is `None` when GitHub has not yet computed mergeability.

    Provider-specific fields are nullable on the other provider:

    Shared:
      - `merge_commit_sha`: the SHA of the merge commit once merged.

    GitHub-only (always `None` on GitLab payloads):
      - `mergeable_state`: GitHub's qualitative state — `clean`, `dirty`,
        `behind`, `unstable`, `blocked`, `draft`, `unknown`.
      - `review_decision`: `APPROVED` / `REVIEW_REQUIRED` /
        `CHANGES_REQUESTED`. Sourced from GraphQL only; the REST `_map_pr`
        path leaves it `None`.
      - `auto_merge`: GitHub's auto-merge configuration block (or `None`
        when auto-merge is not enabled).

    GitLab-only (always `None` on GitHub payloads):
      - `detailed_merge_status`: GitLab's qualitative state — `mergeable`,
        `broken_status`, `ci_must_pass`, `discussions_not_resolved`, etc.
      - `pipeline_status`: head-pipeline status (`success`, `failed`,
        `running`, ...). `None` when no pipeline is attached.
      - `approvals_required` / `approvals_received`: GitLab approval
        counts (Premium+); both `None` on free tier.

    Mergeability note (Issue 6):
      `list_prs` never populates `mergeable` or `mergeable_state` — the
      list endpoint does not compute mergeability for cost reasons. Both
      fields are always `None` in `list_prs` results. Call `get_pr` for
      the single-PR path, which does populate them.
    """

    id: str
    number: int
    title: str
    body: str
    status: PRStatus
    draft: bool
    author: str
    assignees: list[str]
    reviewers: list[str]              # users who actually submitted a review
    requested_reviewers: list[str]
    labels: list[str]
    head: dict                        # {"ref", "sha", "repo_full_name"}
    base: dict                        # {"ref", "sha"}
    merged: bool
    mergeable: bool | None
    url: str
    created_at: str
    updated_at: str
    mergeable_state: str | None = None
    merge_commit_sha: str | None = None
    review_decision: str | None = None
    auto_merge: dict | None = None
    detailed_merge_status: str | None = None
    pipeline_status: str | None = None
    approvals_required: int | None = None
    approvals_received: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReviewComment:
    """An inline (code-review) comment on a pull-request diff.

    Distinct from `Comment`, which is the issue-style discussion
    comment. Review comments are anchored to a file path and a line in
    the diff and are organised into threads (`in_reply_to`).

    Field semantics:
      - `path`: file path the comment is attached to. `None` only for
        legacy GitLab notes that lost their position metadata.
      - `line`: line number on the post-change ("RIGHT") side of the
        diff. `None` when the position is unresolvable (e.g. outdated
        comment whose anchor moved out of the latest diff).
      - `original_line`: the line as of `original_commit_sha`. GitHub
        only; GitLab leaves it `None`.
      - `side`: `"LEFT"` for a deletion-side anchor, `"RIGHT"` for the
        addition side. `None` for GitLab (uses `old_line`/`new_line`
        directly). Note: `side` and `url` may differ between the
        `add_pr_review_comment` response and the same comment as returned
        by `get_pr`. GitLab does not echo `side` on write and constructs
        the `url` only after the comment is saved, so the write response
        carries `side=None` and an empty or provisional `url`; the
        authoritative values are available via `get_pr` afterwards.
      - `commit_sha`: the diff base the comment was anchored against.
      - `in_reply_to`: the id of the comment / discussion this is a
        reply to, or `None` for new threads. On GitHub this is the
        parent comment id; on GitLab it is the discussion id (so
        multiple notes in the same discussion share the same value).
      - `discussion_id`: the thread anchor — the value to pass as
        `in_reply_to` when replying. Provider-uniform semantic: every
        note in the same thread carries the same `discussion_id`,
        regardless of provider. On GitHub the value is the top-of-thread
        note id (`in_reply_to_id` for replies, own `id` for the first
        note); on GitLab it is the actual discussion id from the
        `/discussions` endpoint.
    """

    id: str
    author: str
    body: str
    path: str | None
    line: int | None
    original_line: int | None = None
    side: str | None = None
    commit_sha: str | None = None
    in_reply_to: str | None = None
    created_at: str = ""
    updated_at: str = ""
    url: str | None = None
    discussion_id: str | None = None


ReviewState = Literal["approve", "request_changes", "comment"]


@dataclass
class Review:
    """A pull-request review submission.

    `state` is normalized to provider-agnostic lower-case values:
    `"approve"`, `"request_changes"`, `"comment"`. GitHub's enum maps
    directly; GitLab's discrete endpoints (`approve` / `unapprove` +
    note) are synthesised into the same surface.

    `commit_sha` is set only when the provider pins reviews to a
    specific commit (GitHub). GitLab leaves it `None`.
    """

    id: str
    state: ReviewState
    author: str
    body: str
    url: str
    submitted_at: str
    commit_sha: str | None = None


@dataclass
class StatusSpec:
    """Result of `list_ticket_statuses` — discovery payload for the
    provider-native status state-space.

    `values` lists every accepted status string (including any GitHub
    `state:state_reason` suffix encodings). `transitions` maps each
    value to the values that can legally follow it. `hints` exposes
    semantic anchors so agents can act without provider-specific
    knowledge:

    - `default_open` — the value to use when reopening a ticket.
    - `terminal` — every value that ends the workflow.
    - `terminal_completed` — the terminal value meaning "done as planned".
    - `terminal_declined` — the terminal value meaning "won't do" /
      "not planned".

    For providers that don't distinguish completed-vs-declined (GitLab,
    most ADO templates) `terminal_completed` and `terminal_declined`
    may be the same value.
    """

    values: list[str]
    transitions: dict[str, list[str]]
    hints: dict[str, str | list[str] | None]


@dataclass
class PRFilters:
    status: PRListStatus = "open"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    head: str | None = None           # branch name (`feat/x`) or `owner:branch`
    base: str | None = None
    search: str | None = None
    limit: int = 30


# ---------- pipelines / CI runs ---------------------------------------------


@dataclass
class FailingJob:
    """A single failing job within a pipeline run.

    `failed_step` is the name of the step that flipped the job red, when
    GitHub reports it. `annotations` is the list of GitHub Check-Run
    annotations attached to the job (typically the `failure` /
    `warning` items emitted by build tooling). `log_excerpt` is a small
    text excerpt around the failure (or `None` when logs were
    unavailable, e.g. 403/404 on the log endpoint).
    """

    name: str
    url: str
    failed_step: str
    annotations: list[dict]
    log_excerpt: str | None


@dataclass
class PipelineFailure:
    """Aggregated failure context for a single completed-failed run."""

    failing_jobs: list[FailingJob]
    note: str | None = None  # e.g. "logs unavailable"


@dataclass
class PipelineRun:
    """A CI/CD pipeline run (GitHub Actions workflow_run / GitLab pipeline).

    `conclusion` is `None` for in-progress runs. `failure` is only
    populated by `get_pipeline_run` when the caller asks for the
    failure excerpt AND the run actually concluded as failed.
    """

    id: str
    name: str
    branch: str
    head_sha: str
    event: str
    status: str
    conclusion: str | None
    url: str
    created_at: str
    updated_at: str
    run_attempt: int
    failure: PipelineFailure | None = None


# ---------- token capabilities (ticket #32) ---------------------------------


@dataclass
class TokenCapabilities:
    """Result of `probe_token_capabilities` — what a given token may do
    against a given project.

    Mirrors the nested `Permissions` model from `config.py` so the result
    can be substituted in directly for auto-discovered projects:

    - `issues_create` / `issues_modify` — issue write operations.
    - `pulls_create` / `pulls_modify` / `pulls_merge` — pull-request
      write operations.

    `reason` is `None` on the happy path. On any failure mode it carries
    a stable string identifier so the caller (and tests) can branch on
    it without parsing free-form text:

    - `"bad_credentials"`        — 401 from the provider.
    - `"repo_invisible_to_token"` — 404 from the provider (the token has
      no visibility into the repo, which is GitHub's privacy-preserving
      response for both "doesn't exist" and "exists but you can't see it").
    - `"network_error"`           — transport-level failure (DNS,
      connection refused, timeout, ...).
    - `"permissions_field_missing"` — request succeeded but GitHub
      didn't populate `permissions` on the response (classic PAT
      sometimes, or unexpected payload shape). Combined with all-False
      flags this preserves today's hardcoded-False default behavior.

    When `reason` is not `None`, all boolean flags should be False —
    the caller must not grant any operation based on a failed probe.
    """

    issues_create: bool = False
    issues_modify: bool = False
    pulls_create: bool = False
    pulls_modify: bool = False
    pulls_merge: bool = False
    reason: str | None = None


class TokenCapabilityProvider:
    """Mixin/interface: providers that can probe a token's effective
    capabilities against a single project implement this method.

    Implementations MUST NOT raise on expected failure modes (401, 404,
    network error, missing field) — they must return a `TokenCapabilities`
    with `reason` set and all flags False so the caller can degrade
    gracefully. Only programming errors (bad project shape, etc.) should
    propagate.
    """

    def probe_token_capabilities(
        self, project, token: str
    ) -> TokenCapabilities:
        raise NotImplementedError


# ---------- token project discovery (ticket #80) ----------


@dataclass
class DiscoveredProject:
    """A single project/repository found via token-driven discovery.

    Fields
    ------
    provider
        Matches the ``Provider`` literal values: ``"github"``,
        ``"gitlab"``, or ``"azuredevops"``.
    path
        Provider-native path identifier, e.g. ``"owner/repo"`` for
        GitHub, ``"namespace/project"`` for GitLab, or
        ``"org/project/repo"`` for Azure DevOps.
    permissions
        The token's effective capabilities against this project, as
        returned by ``probe_token_capabilities``.
    description
        Human-readable description of the project (empty string when
        the provider does not supply one).
    default_work_item_type
        Provider-specific default work-item type label (e.g.
        ``"Issue"``); ``None`` when not applicable or not returned.
    base_url
        Self-hosted base URL for the provider instance; ``None`` for
        cloud-hosted (github.com / gitlab.com / dev.azure.com).
    """

    provider: str
    path: str
    permissions: TokenCapabilities
    description: str = ""
    default_work_item_type: str | None = None
    base_url: str | None = None


@dataclass
class ProjectDiscoveryResult:
    """Aggregate result returned by ``TokenProjectDiscoveryProvider.discover_projects``.

    Contract
    --------
    *   On the happy path ``reason`` is ``None`` and ``projects`` may
        contain zero or more entries.
    *   On failure ``projects`` MUST be empty and ``reason`` MUST be
        set to one of the taxonomy values below.
    *   ``truncated=True`` means the provider hit ``limit`` before
        exhausting all visible repositories; it is NOT a failure —
        ``projects`` will be non-empty and ``reason`` will be ``None``.

    Reason taxonomy (mirrors ``TokenCapabilities.reason``)
    -------------------------------------------------------
    ``"bad_credentials"``
        HTTP 401 from the provider.
    ``"network_error"``
        Transport-level failure (DNS, connection refused, timeout, …).
    ``"http_<code>"``
        Unexpected HTTP error, e.g. ``"http_403"`` or ``"http_500"``.
    ``"repo_invisible_to_token"``
        HTTP 404; the token has no visibility into the resource.
    ``"permissions_field_missing"``
        Request succeeded but the provider omitted the ``permissions``
        block (classic PAT / unexpected payload shape).
    ``"insufficient_scope"``
        GitLab-specific: the token lacks the ``read_api`` or
        ``api`` OAuth scope required to enumerate projects.
    """

    projects: list[DiscoveredProject]
    truncated: bool = False
    reason: str | None = None


class TokenProjectDiscoveryProvider:
    """Mixin/interface: providers that can enumerate projects visible to
    a token implement this method.

    Implementations MUST NOT raise on expected failure modes (401, 404,
    network error, missing field, insufficient scope) — they must return
    an empty ``ProjectDiscoveryResult`` with ``reason`` set so the
    caller can degrade gracefully. Only programming errors (e.g. bad
    argument types) should propagate as exceptions.
    """

    def discover_projects(
        self, token: str, *, limit: int
    ) -> ProjectDiscoveryResult:
        raise NotImplementedError
