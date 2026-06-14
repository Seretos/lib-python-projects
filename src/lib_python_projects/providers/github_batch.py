"""Batch read aggregation for GitHub — fetches open issues and open PRs for
N repositories in a single GraphQL POST.

This module is parallel to and independent of the existing `GitHubProvider`
REST client.  It is intentionally read-only and stateless: one call, one
response, no session kept.

Public surface
--------------
- `BatchProjectResult` — dataclass with project, tickets, pull_requests, error.
- `fetch_open_board(projects, token)` — the single entry point.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from lib_python_projects.models import ProjectConfig
from lib_python_projects.providers.base import (
    PullRequest,
    RateLimitError,
    Ticket,
    normalize_timestamp,
)
from lib_python_projects.providers.github import (
    ACCEPT,
    GitHubError,
    USER_AGENT,
)

_GRAPHQL_URL = "https://api.github.com/graphql"

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class BatchProjectResult:
    """Aggregated open-board snapshot for a single repository.

    On success, ``tickets`` and ``pull_requests`` are populated and
    ``error`` is ``None``.  On partial failure (the alias in the GraphQL
    response was ``null`` or missing nodes), both lists are ``[]`` and
    ``error`` carries a human-readable description.
    """

    project: ProjectConfig
    tickets: list[Ticket] = field(default_factory=list)
    pull_requests: list[PullRequest] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# HTTP client factory — single monkeypatch point for tests
# ---------------------------------------------------------------------------


def _graphql_client(token: str) -> httpx.Client:
    """Return a bare httpx.Client for the GitHub GraphQL endpoint.

    Deliberately uses no ETag cache transport — GraphQL POST requests are
    not idempotent/cacheable by design.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ACCEPT,
        "User-Agent": USER_AGENT,
    }
    return httpx.Client(headers=headers, timeout=30.0)


# ---------------------------------------------------------------------------
# Response checking
# ---------------------------------------------------------------------------


def _check_graphql(resp: httpx.Response) -> None:
    """Raise an appropriate exception for non-2xx GraphQL responses.

    Mirrors the logic in ``_check`` in ``github.py``:

    - 429  → ``RateLimitError(429, …)`` with ``retry_after`` from
             ``Retry-After`` header, falling back to ``x-ratelimit-reset``.
    - 403 + ``x-ratelimit-remaining: 0``  → ``RateLimitError(403, …)``.
    - 403 (no rate-limit headers)  → ``GitHubError(403, …)``.
    - Other non-2xx  → ``GitHubError(status, …)``.
    """
    if resp.is_success:
        return
    try:
        payload = resp.json()
        msg = payload.get("message") or resp.reason_phrase or "request failed"
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

    if resp.status_code == 403:
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset_hdr = resp.headers.get("x-ratelimit-reset")
            retry_after_403: int | None = None
            if reset_hdr is not None:
                try:
                    retry_after_403 = max(0, int(reset_hdr) - int(time.time()))
                except (ValueError, TypeError):
                    retry_after_403 = None
            raise RateLimitError(403, msg, retry_after=retry_after_403)
        raise GitHubError(403, msg)

    raise GitHubError(resp.status_code, msg)


# ---------------------------------------------------------------------------
# GraphQL query builder
# ---------------------------------------------------------------------------

_ISSUE_FIELDS = """
    number title body url state createdAt updatedAt
    author { login }
    assignees(first: 20) { nodes { login } }
    labels(first: 20) { nodes { name } }
""".strip()

_PR_FIELDS = """
    number title body url state isDraft createdAt updatedAt
    author { login }
    assignees(first: 20) { nodes { login } }
    labels(first: 20) { nodes { name } }
    headRefName headRefOid headRepository { nameWithOwner }
    baseRefName baseRefOid
    merged mergedAt
    requestedReviewers(first: 20) { nodes { ... on User { login } } }
""".strip()


def _build_query(projects: list[ProjectConfig]) -> tuple[str, dict]:
    """Build the batch GraphQL query and its variables dict.

    Returns ``(query_string, variables_dict)``.  Each repo gets its own
    alias ``r0..rN-1`` so all N repos are fetched in one POST.
    """
    aliases: list[str] = []
    variables: dict = {}

    for i, project in enumerate(projects):
        owner_var = f"owner{i}"
        name_var = f"name{i}"
        variables[owner_var] = project.owner or ""
        variables[name_var] = project.repo or ""
        alias = (
            f"  r{i}: repository(owner: ${owner_var}, name: ${name_var}) {{\n"
            f"    issues(states: OPEN, first: 30) {{\n"
            f"      nodes {{ {_ISSUE_FIELDS} }}\n"
            f"    }}\n"
            f"    pullRequests(states: OPEN, first: 100) {{\n"
            f"      nodes {{ {_PR_FIELDS} }}\n"
            f"    }}\n"
            f"  }}"
        )
        aliases.append(alias)

    # Build the variable declaration block for the query signature
    var_decls = ", ".join(
        f"${owner_var}: String!, ${name_var}: String!"
        for i in range(len(projects))
        for owner_var, name_var in [(f"owner{i}", f"name{i}")]
    )

    query = f"query({var_decls}) {{\n" + "\n".join(aliases) + "\n}"
    return query, variables


# ---------------------------------------------------------------------------
# Field mappers
# ---------------------------------------------------------------------------


def _map_graphql_issue(node: dict, project: ProjectConfig) -> Ticket:
    """Map a GraphQL issue node to a ``Ticket``.

    Produces the same ``Ticket`` shape as ``_map_issue`` in ``github.py``.
    GraphQL always returns ``OPEN`` for open issues (we only query
    ``states: OPEN``), so status is always ``"open"``.
    Labels are sorted alphabetically to match ``_map_issue`` behaviour.
    """
    state = (node.get("state") or "OPEN").upper()
    if state == "OPEN":
        status = "open"
    elif state == "CLOSED":
        status = "closed:completed"
    else:
        status = state.lower()

    label_names = sorted(
        n["name"] for n in (node.get("labels") or {}).get("nodes", []) if n.get("name")
    )
    return Ticket(
        id=str(node.get("number", "")),
        title=node.get("title") or "",
        body=node.get("body") or "",
        status=status,
        author=(node.get("author") or {}).get("login", ""),
        assignees=[
            a["login"]
            for a in (node.get("assignees") or {}).get("nodes", [])
            if a.get("login")
        ],
        labels=label_names,
        url=node.get("url") or "",
        created_at=normalize_timestamp(node.get("createdAt") or ""),
        updated_at=normalize_timestamp(node.get("updatedAt") or ""),
    )


def _map_graphql_pr(node: dict, project: ProjectConfig) -> PullRequest:
    """Map a GraphQL PR node to a ``PullRequest``.

    Produces the same ``PullRequest`` shape as ``_map_pr`` in ``github.py``
    for the list path.  Expensive/computed fields (``mergeable``,
    ``mergeable_state``, ``merge_commit_sha``, ``auto_merge``,
    ``review_decision``) are ``None``; ``reviewers`` is ``[]``.
    This matches the contract of the REST ``list_prs`` path.

    GraphQL PR field mapping:
    - ``isDraft`` → ``draft``
    - ``headRefName`` / ``headRefOid`` / ``headRepository.nameWithOwner``
      → ``head`` dict with keys ``ref``, ``sha``, ``repo_full_name``.
    - ``baseRefName`` / ``baseRefOid`` → ``base`` dict with keys
      ``ref``, ``sha``.
    - ``merged`` / ``mergedAt`` → ``merged`` bool and ``status``.
    - ``requestedReviewers.nodes[].login`` → ``requested_reviewers``.
    """
    state = (node.get("state") or "OPEN").upper()
    merged = bool(node.get("merged") or node.get("mergedAt"))

    if state == "OPEN":
        status = "open"
    elif merged:
        status = "merged"
    else:
        status = "closed"

    head_repo_node = node.get("headRepository") or {}
    head: dict = {
        "ref": node.get("headRefName") or "",
        "sha": node.get("headRefOid") or "",
        "repo_full_name": head_repo_node.get("nameWithOwner") or "",
    }
    base: dict = {
        "ref": node.get("baseRefName") or "",
        "sha": node.get("baseRefOid") or "",
    }

    label_names = [
        n["name"]
        for n in (node.get("labels") or {}).get("nodes", [])
        if n.get("name")
    ]

    requested_reviewers = [
        n["login"]
        for n in (node.get("requestedReviewers") or {}).get("nodes", [])
        if n.get("login")
    ]

    number = node.get("number") or 0
    return PullRequest(
        id=str(number),
        number=int(number),
        title=node.get("title") or "",
        body=node.get("body") or "",
        status=status,  # type: ignore[arg-type]
        draft=bool(node.get("isDraft", False)),
        author=(node.get("author") or {}).get("login", ""),
        assignees=[
            a["login"]
            for a in (node.get("assignees") or {}).get("nodes", [])
            if a.get("login")
        ],
        reviewers=[],
        requested_reviewers=requested_reviewers,
        labels=label_names,
        head=head,
        base=base,
        merged=merged,
        mergeable=None,
        url=node.get("url") or "",
        created_at=normalize_timestamp(node.get("createdAt") or ""),
        updated_at=normalize_timestamp(node.get("updatedAt") or ""),
        mergeable_state=None,
        merge_commit_sha=None,
        review_decision=None,
        auto_merge=None,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fetch_open_board(
    projects: list[ProjectConfig], token: str
) -> list[BatchProjectResult]:
    """Fetch open issues and open PRs for all projects in a single GraphQL POST.

    Parameters
    ----------
    projects:
        Repositories to query.  Must be GitHub projects (``provider="github"``).
        An empty list returns ``[]`` without any HTTP request.
    token:
        GitHub personal access token (or fine-grained PAT) with at least
        ``repo:read`` scope on all listed repositories.

    Returns
    -------
    list[BatchProjectResult]
        One entry per project, in the same order as ``projects``.  On
        partial failure (a repository alias is ``null`` in the response),
        the corresponding entry has ``error`` set and empty lists.

    Raises
    ------
    RateLimitError
        On HTTP 429 or 403 with ``x-ratelimit-remaining: 0``.
    GitHubError
        On any other HTTP error (including 403 without rate-limit headers,
        which typically means a fine-grained PAT scope rejection).
    """
    if not projects:
        return []

    query, variables = _build_query(projects)

    with _graphql_client(token) as client:
        resp = client.post(
            _GRAPHQL_URL,
            json={"query": query, "variables": variables},
        )

    _check_graphql(resp)

    try:
        body = resp.json()
    except Exception as exc:
        raise GitHubError(200, f"could not parse GraphQL response as JSON: {exc}") from exc

    # Top-level GraphQL errors (not per-alias): rate-limit or auth errors
    # that GitHub sometimes surfaces as 200 + errors array.
    top_errors = body.get("errors") or []
    if top_errors:
        # Check if they look like rate-limit errors
        combined = " ".join(
            (e.get("message") or "") for e in top_errors if isinstance(e, dict)
        ).lower()
        if "rate limit" in combined or "rate_limit" in combined:
            raise RateLimitError(200, "; ".join(
                (e.get("message") or "rate limit exceeded")
                for e in top_errors
                if isinstance(e, dict)
            ))
        # Otherwise map per-project where possible; if data is entirely
        # absent raise immediately.
        if not body.get("data"):
            raise GitHubError(
                200,
                "; ".join(
                    (e.get("message") or "GraphQL error")
                    for e in top_errors
                    if isinstance(e, dict)
                ),
            )
        # Partial: data present, some aliases might be null — handled below.

    data = body.get("data") or {}
    results: list[BatchProjectResult] = []

    for i, project in enumerate(projects):
        alias = f"r{i}"
        alias_data = data.get(alias)

        if alias_data is None:
            # Alias was null — partial failure for this repo.
            # Look for a matching error message if available.
            err_msg = f"repository data unavailable for {project.path!r}"
            for e in top_errors:
                if isinstance(e, dict):
                    locs = e.get("locations") or e.get("path") or []
                    msg = e.get("message") or ""
                    if alias in str(locs) or alias in str(e.get("path") or []):
                        err_msg = msg or err_msg
                        break
            results.append(BatchProjectResult(project=project, error=err_msg))
            continue

        # Map issues
        issues_data = alias_data.get("issues") or {}
        issue_nodes = issues_data.get("nodes") or []
        tickets = [_map_graphql_issue(n, project) for n in issue_nodes if n]

        # Map pull requests
        prs_data = alias_data.get("pullRequests") or {}
        pr_nodes = prs_data.get("nodes") or []
        pull_requests = [_map_graphql_pr(n, project) for n in pr_nodes if n]

        results.append(
            BatchProjectResult(
                project=project,
                tickets=tickets,
                pull_requests=pull_requests,
            )
        )

    return results
