"""``FakeGitHub`` -- an in-process simulator standing in for the real ``gh``
CLI, built for #243 generation 2 (see ``.adev/243-1/plan.md``).

Generation 1's fake ``gh`` (``tests/bash_script_harness.py``) was a bash
script that ignored its own argv and blanket-responded from env-var knobs;
tests could therefore only assert *argv shapes*, one bespoke assertion per
review round, and it caught nothing it hadn't been specifically told to
catch (rounds 1, 3, 4, 6 all shipped past it). ``FakeGitHub`` fixes that
structurally:

  (a) every subcommand's argv is parsed strictly with ``argparse`` --
      exactly like real ``gh`` would reject it -- so an unknown flag or a
      missing required flag is a hard error, not a silent no-op;
  (b) ``--jq``, ``-q``, ``--paginate`` and ``--template`` are rejected
      *anywhere* in the argv, regardless of subcommand -- production code
      must never reach for CLI-side filtering;
  (c) ``gh api`` implements real REST pagination over an internal issues
      list, sliced by ``page``/``per_page``;
  (d) state is mutable and outcome-queryable: creating an issue, adding a
      board item, and editing that item's status are all real state
      transitions a test can read back (``board_item_status``,
      ``board_items``, ``issues_matching``, ...) instead of asserting on
      the shape of the call that produced them.

``run_gh`` mirrors the contract ``ci.gh.run_gh`` will eventually have:

    run_gh(args: list[str], *, check: bool = True) -> str

``check=True`` (the default) raises ``GhCallError`` on a simulated
non-zero exit, mirroring ``subprocess.run(..., check=True)``. ``check=False``
never raises for a *simulated gh failure* -- it returns ``""`` instead,
mirroring a real invocation whose stdout is empty because the command
failed; callers that legitimately tolerate failure (release lookup, the
labelled ``issue create`` attempt) are expected to treat an empty return as
"didn't work" themselves, exactly as the original bash did with `$?`.

A forbidden flag (a), however, is *never* tolerated by ``check=False`` --
it always raises, because it represents a programming-discipline violation
in the caller, not a legitimate ``gh`` failure the caller is allowed to
shrug off.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ci.gh import GhError as GhCallError

# ``GhCallError`` is ``ci.gh.GhError`` itself (generation 2 is implemented
# now, so the real production exception type exists) -- this alias keeps
# every existing ``from tests.fake_gh import GhCallError`` /
# ``pytest.raises(GhCallError, ...)`` call site working unchanged while
# guaranteeing production code's ``except ci.gh.GhError`` handlers actually
# catch a simulated `gh` failure (a real-CLI failure and a simulated one are
# now, by construction, the same exception type -- see the module docstring
# above for the original rationale).


_FORBIDDEN_FLAGS = {"--jq", "-q", "--paginate", "--template"}


class _StrictArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that raises instead of printing usage and
    calling ``sys.exit`` -- every parsing failure (missing required
    argument, unknown flag, bad positional count) funnels through
    ``error()`` in stock argparse, so overriding just this one method is
    enough to convert *all* of them into a catchable ``GhCallError``."""

    def error(self, message: str) -> None:  # pragma: no cover - trivial
        raise GhCallError(f"gh: {self.prog}: {message}")


@dataclass
class _Issue:
    id: int
    repo: str
    title: str
    body: str
    url: str
    state: str = "open"
    is_pull_request: bool = False
    label: Optional[str] = None


@dataclass
class _BoardItem:
    id: str
    issue_url: str
    status: Optional[str] = None


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

        self._releases: dict[tuple[str, str], Optional[str]] = {}
        self._tags: dict[str, list[str]] = {}
        self._release_tags: dict[str, list[dict]] = {}

        self._issues: list[_Issue] = []
        self._next_issue_id = 1
        self._issue_create_label_fails: set[str] = set()
        self._issue_create_all_fail: set[str] = set()

        self._project_id = "PVT_fakeproject000"
        self._fields: list[dict] = []
        self._fields_total_count = 0
        self._fields_total_count_present = True

        self._board_items: list[_BoardItem] = []
        self._next_item_id = 1
        self._item_add_fails = False
        self._item_edit_fails = False

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------
    def add_release(self, repo: str, tag: str, body: Optional[str]) -> None:
        """Seed a release ``gh release view <tag> --repo <repo>`` will
        answer for. Not calling this for a given (repo, tag) pair means the
        simulator holds no such release -- a lookup then behaves like a
        real 404 (non-zero exit), never a blanket canned response."""
        self._releases[(repo, tag)] = body

    def add_tag(self, repo: str, name: str) -> None:
        """Seed a Git tag ``name`` that is ALSO backed by a *published*
        GitHub Release -- appears in both ``gh api repos/{repo}/tags`` and
        ``gh api repos/{repo}/releases`` for ``repo``, exactly like a tag
        this repo's own release workflow pushed and then successfully
        published a Release for. Call order determines listing order (the
        real endpoint has its own ordering, but a correct caller sorts by
        semver itself rather than relying on listing order --
        ``ci.prev_tag``'s driving tests seed tags out of order on purpose to
        prove that)."""
        self._tags.setdefault(repo, []).append(name)
        self._release_tags.setdefault(repo, []).append({"tag_name": name, "draft": False})

    def add_unreleased_tag(self, repo: str, name: str) -> None:
        """Seed a Git tag ``name`` that exists (appears in
        ``gh api repos/{repo}/tags``) but has NO backing GitHub Release
        (absent from ``gh api repos/{repo}/releases``) -- the round-3 review
        finding, #251: this repo's release workflow pushes the tag and
        creates the Release in two separate, non-atomic steps, so a runner
        failure or transient ``gh`` outage between them can leave exactly
        this state in production. A caller that (still) lists raw tags
        instead of published releases would wrongly consider this tag a
        valid ``--notes-start-tag`` candidate."""
        self._tags.setdefault(repo, []).append(name)

    def add_draft_release_tag(self, repo: str, name: str) -> None:
        """Seed a *draft* GitHub Release's tag -- present in both listings
        (a real ``gh api repos/{repo}/releases`` response does include
        drafts a token with push access can see) but not yet "published"
        for ``ci.prev_tag``'s purposes, so it must never be selected as
        ``--notes-start-tag``."""
        self._tags.setdefault(repo, []).append(name)
        self._release_tags.setdefault(repo, []).append({"tag_name": name, "draft": True})

    def add_open_issue(self, repo: str, title: str, *, is_pull_request: bool = False) -> str:
        return self._create_issue(repo, title, body="", label=None, is_pull_request=is_pull_request)

    def add_closed_issue(self, repo: str, title: str) -> str:
        url = self._create_issue(repo, title, body="", label=None)
        self._issues[-1].state = "closed"
        return url

    def fail_labelled_create(self, repo: str) -> None:
        """The next (and every subsequent) `gh issue create --label ...`
        against ``repo`` fails, simulating a missing label."""
        self._issue_create_label_fails.add(repo)

    def fail_all_creates(self, repo: str) -> None:
        """Every `gh issue create` against ``repo`` fails, labelled or not
        -- simulates an outage/bad-token/rate-limit, not a missing label."""
        self._issue_create_all_fail.add(repo)

    def fail_item_add(self) -> None:
        self._item_add_fails = True

    def fail_item_edit(self) -> None:
        """The next (and every subsequent) `gh project item-edit` fails,
        simulating a transient API hiccup or the Status field/Backlog
        option getting renamed/deleted AFTER a successful `item-add` -- the
        item genuinely is on the board at that point, just never
        transitioned to Backlog (#243 round 2 blocking finding 1+2), unlike
        ``fail_item_add`` above where the item never lands on the board at
        all."""
        self._item_edit_fails = True

    def set_project_id(self, project_id: str) -> None:
        self._project_id = project_id

    def set_fields(
        self,
        fields: list[dict],
        *,
        total_count: Optional[int] = None,
        omit_total_count: bool = False,
    ) -> None:
        """``fields`` is the full underlying set of Status/etc. fields --
        order and each dict's key order are used exactly as given, so a
        test can deliberately put a decoy field first or reverse an
        object's key order to prove the caller doesn't depend on either.

        A `gh project field-list --limit N` call only ever returns
        ``fields[:N]`` -- exactly like the real gh default (30) truncates a
        board with more custom fields than that. ``total_count`` defaults
        to ``len(fields)`` and is reported as the response's ``totalCount``
        unless ``omit_total_count=True``, which simulates a response that
        doesn't report totalCount at all (so truncation can't even be
        detected from the response shape)."""
        self._fields = fields
        self._fields_total_count = total_count if total_count is not None else len(fields)
        self._fields_total_count_present = not omit_total_count

    # ------------------------------------------------------------------
    # Outcome assertions (what tests read back)
    # ------------------------------------------------------------------
    def issues_matching(self, repo: str, title: str) -> list[dict]:
        return [
            {"url": i.url, "state": i.state, "is_pull_request": i.is_pull_request}
            for i in self._issues
            if i.repo == repo and i.title == title
        ]

    def created_issue_bodies(self, repo: str, title: str) -> list[str]:
        return [i.body for i in self._issues if i.repo == repo and i.title == title and i.state == "open"]

    def board_items(self, issue_url: str) -> list[dict]:
        return [{"id": it.id, "status": it.status} for it in self._board_items if it.issue_url == issue_url]

    def board_item_status(self, issue_url: str) -> Optional[str]:
        items = [it for it in self._board_items if it.issue_url == issue_url]
        if not items:
            return None
        return items[-1].status

    # ------------------------------------------------------------------
    # The ci.gh.run_gh-compatible entry point
    # ------------------------------------------------------------------
    def run_gh(self, args: list[str], *, check: bool = True) -> str:
        self.calls.append(list(args))

        for token in args:
            if token in _FORBIDDEN_FLAGS:
                raise GhCallError(
                    f"gh: forbidden flag {token!r} used in {args!r} -- CLI-side "
                    "filtering/paging/extraction is not allowed; parse the JSON "
                    "in Python instead (see ci/gh.py's discipline rule)"
                )

        if not args:
            raise GhCallError("gh: no subcommand given")

        noun = args[0]
        if noun == "api":
            return self._api(args[1:], check=check)

        if len(args) < 2:
            raise GhCallError(f"gh: unrecognized invocation: {args!r}")

        verb = args[1]
        rest = args[2:]

        handlers = {
            ("release", "view"): self._release_view,
            ("issue", "create"): self._issue_create,
            ("project", "view"): self._project_view,
            ("project", "field-list"): self._project_field_list,
            ("project", "item-add"): self._project_item_add,
            ("project", "item-edit"): self._project_item_edit,
        }
        handler = handlers.get((noun, verb))
        if handler is None:
            raise GhCallError(f"gh: unhandled subcommand: {noun} {verb}")
        return handler(rest, check=check)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _fail(self, check: bool, message: str) -> str:
        if check:
            raise GhCallError(message)
        return ""

    def _create_issue(
        self,
        repo: str,
        title: str,
        *,
        body: str,
        label: Optional[str],
        is_pull_request: bool = False,
    ) -> str:
        issue_id = self._next_issue_id
        self._next_issue_id += 1
        url = f"https://github.com/{repo}/issues/{issue_id}"
        self._issues.append(
            _Issue(
                id=issue_id,
                repo=repo,
                title=title,
                body=body,
                url=url,
                is_pull_request=is_pull_request,
                label=label,
            )
        )
        return url

    def _release_view(self, rest: list[str], *, check: bool) -> str:
        parser = _StrictArgumentParser(prog="gh release view", add_help=False)
        parser.add_argument("tag")
        parser.add_argument("--repo", required=True)
        parser.add_argument("--json", required=True)
        ns = parser.parse_args(rest)

        key = (ns.repo, ns.tag)
        if key not in self._releases:
            return self._fail(check, f"release not found: {ns.repo}@{ns.tag}")
        return json.dumps({"body": self._releases[key]})

    def _issue_create(self, rest: list[str], *, check: bool) -> str:
        parser = _StrictArgumentParser(prog="gh issue create", add_help=False)
        parser.add_argument("--repo", required=True)
        parser.add_argument("--title", required=True)
        parser.add_argument("--body", required=True)
        parser.add_argument("--label", default=None)
        ns = parser.parse_args(rest)

        if ns.repo in self._issue_create_all_fail:
            return self._fail(check, f"gh issue create failed (simulated outage) for {ns.repo}")
        if ns.label is not None and ns.repo in self._issue_create_label_fails:
            return self._fail(check, f"label {ns.label!r} not found in {ns.repo}")

        return self._create_issue(ns.repo, ns.title, body=ns.body, label=ns.label)

    def _api(self, rest: list[str], *, check: bool) -> str:
        parser = _StrictArgumentParser(prog="gh api", add_help=False)
        parser.add_argument("path")
        ns = parser.parse_args(rest)

        parsed = urlparse(ns.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "repos" or parts[3] not in ("issues", "tags", "releases"):
            return self._fail(check, f"unsupported gh api path: {ns.path}")

        repo = f"{parts[1]}/{parts[2]}"
        query = parse_qs(parsed.query)
        per_page = int((query.get("per_page") or ["30"])[0])
        page = int((query.get("page") or ["1"])[0])
        start = (page - 1) * per_page

        if parts[3] == "tags":
            names = self._tags.get(repo, [])
            page_names = names[start : start + per_page]
            return json.dumps([{"name": name} for name in page_names])

        if parts[3] == "releases":
            entries = self._release_tags.get(repo, [])
            page_entries = entries[start : start + per_page]
            return json.dumps(list(page_entries))

        state = (query.get("state") or ["all"])[0]
        matching = [i for i in self._issues if i.repo == repo and (state == "all" or i.state == state)]
        page_items = matching[start : start + per_page]

        payload = [
            {
                "title": i.title,
                "html_url": i.url,
                "state": i.state,
                **({"pull_request": {}} if i.is_pull_request else {}),
            }
            for i in page_items
        ]
        return json.dumps(payload)

    def _project_view(self, rest: list[str], *, check: bool) -> str:
        parser = _StrictArgumentParser(prog="gh project view", add_help=False)
        parser.add_argument("number")
        parser.add_argument("--owner", required=True)
        parser.add_argument("--format", required=True)
        parser.parse_args(rest)
        return json.dumps({"id": self._project_id})

    def _project_field_list(self, rest: list[str], *, check: bool) -> str:
        parser = _StrictArgumentParser(prog="gh project field-list", add_help=False)
        parser.add_argument("number")
        parser.add_argument("--owner", required=True)
        parser.add_argument("--limit", type=int, default=30)
        parser.add_argument("--format", required=True)
        ns = parser.parse_args(rest)

        payload: dict = {"fields": self._fields[: ns.limit]}
        if self._fields_total_count_present:
            payload["totalCount"] = self._fields_total_count
        return json.dumps(payload)

    def _project_item_add(self, rest: list[str], *, check: bool) -> str:
        parser = _StrictArgumentParser(prog="gh project item-add", add_help=False)
        parser.add_argument("number")
        parser.add_argument("--owner", required=True)
        parser.add_argument("--url", required=True)
        parser.add_argument("--format", required=True)
        ns = parser.parse_args(rest)

        if self._item_add_fails:
            return self._fail(check, "item-add failed (simulated)")

        item_id = f"PVTI_fakeitem{self._next_item_id:03d}"
        self._next_item_id += 1
        self._board_items.append(_BoardItem(id=item_id, issue_url=ns.url, status=None))
        return json.dumps({"id": item_id})

    def _project_item_edit(self, rest: list[str], *, check: bool) -> str:
        parser = _StrictArgumentParser(prog="gh project item-edit", add_help=False)
        parser.add_argument("--id", required=True, dest="item_id")
        parser.add_argument("--project-id", required=True)
        parser.add_argument("--field-id", required=True)
        parser.add_argument("--single-select-option-id", required=True)
        ns = parser.parse_args(rest)

        if self._item_edit_fails:
            return self._fail(check, "item-edit failed (simulated)")

        matches = [it for it in self._board_items if it.id == ns.item_id]
        if not matches:
            return self._fail(check, f"item-edit: no such item id {ns.item_id!r}")

        # Validate the field-id/option-id *pairing*, not just that each id
        # independently exists somewhere -- a caller that swaps in the
        # right option id but the wrong field id (or vice versa) must not
        # silently produce "Backlog" status (#243 test-critic finding 1).
        field = next((f for f in self._fields if f.get("id") == ns.field_id), None)
        if field is None:
            return self._fail(check, f"item-edit: no such field id {ns.field_id!r}")

        option = next(
            (o for o in (field.get("options") or []) if o.get("id") == ns.single_select_option_id),
            None,
        )
        if option is None:
            return self._fail(
                check,
                f"item-edit: option id {ns.single_select_option_id!r} does not belong to field "
                f"{ns.field_id!r} ({field.get('name')!r})",
            )

        matches[-1].status = option.get("name")
        return "ok"
