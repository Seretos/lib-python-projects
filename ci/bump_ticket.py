"""Files (or reuses) a ``chore(deps)`` ticket in a downstream consumer repo
announcing a new release of the source repo, embedding that release's
changelog verbatim under a ``### What changed`` heading (falling back to a
link to the release page when the changelog can't be fetched or is
empty/whitespace-only). Idempotent: an existing open issue with the exact
title is reused rather than re-created.

Entry point: :func:`run` (``ci.bump_ticket.run(env) -> int``), reading
``VERSION``, ``SOURCE_REPO``, ``CONSUMER_REPO``, ``GH_TOKEN``,
``GITHUB_OUTPUT`` from the passed-in mapping -- see ``REQUIRED_ENV`` (which
does not include ``GITHUB_OUTPUT``: that path is provided ambiently by the
GitHub Actions runner to every step, not declared in the composite action's
own ``env:`` block -- see ``tests/test_bump_workflow_wiring.py``).
"""

from __future__ import annotations

import json
import os
import sys

import ci.actions_io as actions_io
import ci.gh as gh

REQUIRED_ENV = ("VERSION", "SOURCE_REPO", "CONSUMER_REPO", "GH_TOKEN")

# Roughly GitHub's own issue-body size ceiling, with headroom. If embedding
# the changelog verbatim would push the body over this, the notes are
# truncated and a link to the full release page is appended instead of
# silently exceeding the limit (or silently dropping content with no trace).
_BODY_BUDGET = 60_000
_TRUNCATION_MARKER_TEMPLATE = "\n\n…truncated — full release notes: {url}\n"

_DEPENDENCIES_LABEL = "dependencies"


def _release_page_url(source_repo: str, tag: str) -> str:
    return f"https://github.com/{source_repo}/releases/tag/{tag}"


def _fetch_release_notes(source_repo: str, tag: str) -> str | None:
    """The release's own body, verbatim, or ``None`` if the lookup failed
    (non-zero exit, unparseable JSON) or the body is missing/empty/
    whitespace-only -- any of which is treated as "no usable changelog"."""
    raw = gh.run_gh(["release", "view", tag, "--repo", source_repo, "--json", "body"], check=False)
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except ValueError:
        return None

    body = payload.get("body") if isinstance(payload, dict) else None
    if body is None or not str(body).strip():
        return None

    return str(body)


def _render_body(*, package_name: str, tag: str, source_repo: str, what_changed: str) -> str:
    return (
        "## Dependency update\n\n"
        f"A new release of **{package_name}** has been published: `{tag}`.\n\n"
        "### What changed\n\n"
        f"{what_changed}\n\n"
        "### Action required\n\n"
        f"1. Locate where `{package_name}` is pinned in this repository\n"
        "   (e.g. `pyproject.toml`, `requirements.txt`, `backend/requirements.txt`, `setup.cfg`)\n"
        "   and update the pin to:\n"
        "   ```\n"
        f"   {package_name} @ git+https://github.com/{source_repo}@{tag}\n"
        "   ```\n"
        "2. Run the project's own test suite to verify nothing is broken.\n"
        "3. If the project has a build step, confirm the build succeeds.\n"
        "4. Commit and open a PR.\n"
    )


def _compose_body(*, package_name: str, tag: str, source_repo: str, notes: str | None, release_page_url: str) -> str:
    if notes is None:
        actions_io.warn(
            f"Could not fetch release notes for {tag} from {source_repo}; linking to the release page instead."
        )
        return _render_body(package_name=package_name, tag=tag, source_repo=source_repo, what_changed=release_page_url)

    body = _render_body(package_name=package_name, tag=tag, source_repo=source_repo, what_changed=notes)
    if len(body) <= _BODY_BUDGET:
        return body

    marker = _TRUNCATION_MARKER_TEMPLATE.format(url=release_page_url)
    overage = len(body) - _BODY_BUDGET + len(marker)
    truncated_notes = notes[: max(len(notes) - overage, 0)]
    what_changed = truncated_notes + marker
    return _render_body(package_name=package_name, tag=tag, source_repo=source_repo, what_changed=what_changed)


def _find_existing_open_issue(consumer_repo: str, title: str) -> str | None:
    """Reuse an existing open issue with the exact title, excluding pull
    requests (which also show up on the issues REST endpoint -- detected by
    *key presence*, not truthiness: GitHub reports an empty-but-present
    ``pull_request`` object, which is falsy in Python but not in the source
    of truth). A plain probe failure (a single ``gh api`` call erroring out,
    e.g. a transient hiccup) never silently succeeds with nothing filed: it
    warns and falls through to creation, since creating a new ticket is the
    safe default when we simply couldn't reach the API at all.

    A page-cap *exhaustion* (:class:`ci.gh.GhPaginationExhausted`) is
    handled differently and NOT caught here -- it is deliberately let
    through to the caller as a :class:`ci.actions_io.ScriptError` instead of
    being folded into the same warn-and-fall-through path. Exhaustion means
    the existing-open-issues list is known-incomplete, not merely that one
    request failed; falling through to creation in that case risks filing a
    duplicate bump ticket, which is worse than the plain-failure case above
    (#243 round 2 blocking finding 3)."""
    try:
        items = gh.gh_paginate_rest(f"repos/{consumer_repo}/issues?state=open")
    except gh.GhPaginationExhausted as exc:
        raise actions_io.ScriptError(
            f"Could not confirm whether an open issue titled {title!r} already exists in "
            f"{consumer_repo}: the open-issues listing did not finish paginating ({exc}). "
            "Refusing to create a new issue since that could file a duplicate -- investigate "
            "and re-run manually."
        ) from exc
    except gh.GhError as exc:
        actions_io.warn(
            f"Could not check {consumer_repo} for an existing open issue ({exc}); attempting to create a new one."
        )
        return None

    for item in items:
        if "pull_request" in item:
            continue
        if item.get("title") == title:
            return item.get("html_url")

    return None


def _attempt_create_issue(consumer_repo: str, title: str, body: str) -> str | None:
    """Labelled attempt first; if it fails (e.g. the ``dependencies`` label
    doesn't exist in the consumer repo) or comes back empty, retry
    unlabelled. Both are ``check=False`` -- a `gh` failure here is a
    legitimate, tolerated outcome the caller decides how to handle, not a
    programming error."""
    url = gh.run_gh(
        [
            "issue",
            "create",
            "--repo",
            consumer_repo,
            "--title",
            title,
            "--body",
            body,
            "--label",
            _DEPENDENCIES_LABEL,
        ],
        check=False,
    )
    if url:
        return url.strip()

    url = gh.run_gh(
        ["issue", "create", "--repo", consumer_repo, "--title", title, "--body", body],
        check=False,
    )
    if url:
        return url.strip()

    return None


def run(env: dict[str, str]) -> int:
    version = env["VERSION"]
    source_repo = env["SOURCE_REPO"]
    consumer_repo = env["CONSUMER_REPO"]

    package_name = source_repo.split("/", 1)[-1]
    tag = f"v{version}"
    title = f"chore(deps): bump {package_name} to {tag}"

    notes = _fetch_release_notes(source_repo, tag)
    body = _compose_body(
        package_name=package_name,
        tag=tag,
        source_repo=source_repo,
        notes=notes,
        release_page_url=_release_page_url(source_repo, tag),
    )

    try:
        issue_url = _find_existing_open_issue(consumer_repo, title)
    except actions_io.ScriptError as exc:
        actions_io.error(str(exc))
        return 1

    if issue_url is None:
        issue_url = _attempt_create_issue(consumer_repo, title, body)
        if issue_url is None:
            actions_io.error(f"Failed to create issue in {consumer_repo}")
            return 1

    actions_io.set_output("issue_url", issue_url, env)
    return 0


def main() -> int:
    return actions_io.run_main(lambda: run(dict(os.environ)))


if __name__ == "__main__":
    sys.exit(main())
