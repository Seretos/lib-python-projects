"""Computes the previous released ``v*`` tag (semver-sorted) so
``release.yml`` can pass it to ``gh release create --notes-start-tag``,
scoping the auto-generated release notes to only the delta since the
previous release (#251) instead of every commit since the repo's very
first release.

Entry point: :func:`run` (``ci.prev_tag.run(env) -> int``), reading
``VERSION`` and ``SOURCE_REPO`` from the passed-in mapping (see
``REQUIRED_ENV``, which also lists ``GH_TOKEN`` -- consumed implicitly by
``gh`` itself out of the ambient environment, exactly like
``ci.bump_ticket``/``ci.board`` never read their own ``GH_TOKEN`` directly
either).

Candidates are listed from ``repos/{repo}/releases`` (published GitHub
Releases), not ``repos/{repo}/tags`` (round-3 review finding, #251): the two
endpoints are not equivalent -- ``tags`` lists every Git tag in the repo,
regardless of whether a Release was ever published for it. Within this
repo's own release workflow, the tag push (``git push origin "$TAG"``, in
the "Stamp pyproject.toml, commit, tag, force-push release branch" step) and
the Release creation (``gh release create``, several steps later) are two
separate, non-atomic steps -- a runner failure, a cancelled workflow run, or
a transient ``gh`` outage between them leaves a real, pushed ``v*`` tag with
no Release ever published for it. Sourcing candidates from ``tags`` would
let such an orphaned tag be silently selected as ``--notes-start-tag``,
truncating the generated notes to omit commits actually shipped in the true
previous release. Draft releases (``draft: true``) are filtered out --
undrafting is a deliberate, sometimes-delayed human action, so a draft is
not yet "published" for this purpose -- but prereleases are kept, since this
repo's own ``-rc.N`` versions are published (non-draft) Releases via this
very workflow (see the ``--prerelease`` flag in "Create GitHub Release").

Listed via ``ci.gh.gh_paginate_rest`` (the single choke point) and the
result is written to ``$GITHUB_OUTPUT`` via ``ci.actions_io.set_output`` as
``previous_tag`` -- empty string when there is no published-release tag
strictly below ``VERSION`` (first release of a line) or when the release
listing fails. A notes-range lookup must never fail a release: a
``ci.gh.GhError`` (``ci.gh.GhPaginationExhausted`` is a subclass, so this
also covers page exhaustion) or a bare ``ValueError`` (covers
``json.JSONDecodeError``, raised by ``ci.gh.gh_paginate_rest`` itself when a
`gh api` call exits 0 but returns a malformed/non-JSON body) is caught,
warned about via ``ci.actions_io.warn``, and the step still exits 0 with an
empty ``previous_tag``.
"""

from __future__ import annotations

import os
import re
import sys

import ci.actions_io as actions_io
import ci.gh as gh

REQUIRED_ENV = ("VERSION", "SOURCE_REPO", "GH_TOKEN")

# Mirrors release.yml's own version-validation regex exactly
# (`^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`), prefixed with the `v`
# every tag in this repo carries.
_TAG_RE = re.compile(r"^v(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)(-(?P<pre>[0-9A-Za-z.-]+))?$")
_VERSION_RE = re.compile(r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)(-(?P<pre>[0-9A-Za-z.-]+))?$")


def _prerelease_key(pre: str) -> tuple:
    """Per-dot-separated-identifier precedence: a numeric identifier
    compares numerically and sorts below any alphanumeric identifier;
    alphanumeric identifiers compare lexically (ASCII) -- mirrors common
    semver precedence closely enough for this repo's own prerelease tags
    (simple ``-rc.N`` style)."""
    key = []
    for part in pre.split("."):
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part))
    return tuple(key)


def _sort_key(major: str, minor: str, patch: str, pre: str | None) -> tuple:
    # A release (no prerelease suffix) sorts *above* every prerelease of
    # the same (major, minor, patch); ``has_no_prerelease`` is 1 for a
    # release and 0 for a prerelease, so ascending order puts the
    # prerelease before its release.
    has_no_prerelease = 1 if pre is None else 0
    return (
        int(major),
        int(minor),
        int(patch),
        has_no_prerelease,
        _prerelease_key(pre) if pre is not None else (),
    )


def _previous_tag(tags: list[dict], version: str) -> str:
    """``tags`` is a list of ``{"name": ...}`` dicts -- callers pass in
    already-published-release tag names (see :func:`run`), pre-filtered so
    every entry here genuinely backs a published GitHub Release."""
    version_match = _VERSION_RE.match(version)
    if version_match is None:
        # VERSION failed to validate as semver upstream (release.yml's own
        # "Validate version is semver" step would already have failed the
        # workflow before this step ever runs) -- degrade gracefully rather
        # than crash.
        return ""
    version_key = _sort_key(**version_match.groupdict())

    best_name: str | None = None
    best_key: tuple | None = None
    for entry in tags:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            continue
        match = _TAG_RE.match(name)
        if match is None:
            continue
        key = _sort_key(**match.groupdict())
        if key >= version_key:
            continue
        if best_key is None or key > best_key:
            best_key = key
            best_name = name

    return best_name or ""


def run(env: dict[str, str]) -> int:
    version = env["VERSION"]
    source_repo = env["SOURCE_REPO"]

    try:
        releases = gh.gh_paginate_rest(f"repos/{source_repo}/releases")
    except (gh.GhError, ValueError) as exc:
        # ``ValueError`` here covers ``json.JSONDecodeError`` specifically:
        # ``gh_paginate_rest`` calls ``json.loads`` on each page's stdout,
        # and a `gh api` invocation that exits 0 but returns a malformed or
        # non-JSON body (e.g. an HTML error page during an outage) raises a
        # bare ``json.JSONDecodeError`` -- a ``ValueError`` subclass,
        # unrelated to ``GhError`` -- which must degrade exactly like a
        # ``GhError`` does rather than crash the release step (round-1
        # review finding 1).
        actions_io.warn(f"Could not list releases for {source_repo} ({exc}); omitting --notes-start-tag.")
        actions_io.set_output("previous_tag", "", env)
        return 0

    # Published-release tags only (round-3 review finding): drop drafts, and
    # rename each entry's ``tag_name`` to ``name`` so it fits the shape
    # ``_previous_tag`` already expects. A release with no ``tag_name`` (or a
    # falsy one) is dropped harmlessly by ``_previous_tag``'s own
    # ``if not name: continue`` guard rather than crashing here.
    tags = [
        {"name": release.get("tag_name")}
        for release in releases
        if isinstance(release, dict) and not release.get("draft")
    ]

    actions_io.set_output("previous_tag", _previous_tag(tags, version), env)
    return 0


def main() -> int:
    return actions_io.run_main(lambda: run(dict(os.environ)))


if __name__ == "__main__":
    sys.exit(main())
