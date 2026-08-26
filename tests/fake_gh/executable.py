#!/usr/bin/env python3
"""Standalone stand-in for the real ``gh`` CLI, used only by
``tests/test_ci_entrypoints_subprocess.py`` (#243 generation 2 Layer 2).

Unlike ``tests/fake_gh/model.py``'s ``FakeGitHub`` (the in-process simulator
used for Layer 1, monkeypatched directly over ``ci.gh.run_gh``), this script
is invoked as a real, separate subprocess by ``ci.gh.run_gh`` itself -- it
has to be fully self-contained (no dependency on this repo being on
``sys.path``, since it may be launched with an arbitrary cwd once installed
onto ``PATH`` by the test) rather than importing ``FakeGitHub``.

It answers a small, fixed set of canned responses driven entirely by
``FAKE_GH_*`` env vars -- just enough to drive ``ci.bump_ticket.run`` and
``ci.board.run`` through one real, complete happy path each, end to end
through a real subprocess boundary. It intentionally does not attempt
``FakeGitHub``'s strict per-subcommand argv validation or stateful,
outcome-queryable behaviour -- that discipline is already proven at Layer 1
(``tests/test_bump_ticket.py``, ``tests/test_board_backlog.py``,
``tests/test_ci_gh_discipline.py``); this file's only job is proving the
``python -m ci.<module>`` entry point itself really reads real env vars,
writes a real ``$GITHUB_OUTPUT`` file, and propagates a real exit code.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("gh: no subcommand given", file=sys.stderr)
        return 1

    noun = args[0]
    verb = args[1] if len(args) > 1 else None

    if noun == "release" and verb == "view":
        body = os.environ.get("FAKE_GH_RELEASE_BODY", "some release notes")
        print(json.dumps({"body": body}))
        return 0

    if noun == "issue" and verb == "create":
        print(os.environ.get("FAKE_GH_ISSUE_URL", "https://github.com/Seretos/fake-consumer/issues/999"))
        return 0

    if noun == "api":
        # The idempotency probe (`gh api repos/.../issues?state=open&...`).
        # An empty JSON array means "no existing issue" -- falls through to
        # creation, exercising the same path as the happy-path Layer 1 test.
        print(os.environ.get("FAKE_GH_ISSUES_PAGE_JSON", "[]"))
        return 0

    if noun == "project" and verb == "view":
        print(json.dumps({"id": os.environ.get("FAKE_GH_PROJECT_ID", "PVT_fakeproject")}))
        return 0

    if noun == "project" and verb == "field-list":
        default_fields = json.dumps(
            {
                "fields": [
                    {
                        "id": "PVTSSF_fakestatus",
                        "name": "Status",
                        "options": [{"id": "backlog_opt", "name": "Backlog"}],
                    }
                ],
                "totalCount": 1,
            }
        )
        print(os.environ.get("FAKE_GH_FIELDS_JSON", default_fields))
        return 0

    if noun == "project" and verb == "item-add":
        print(json.dumps({"id": os.environ.get("FAKE_GH_ITEM_ID", "PVTI_fakeitem001")}))
        return 0

    if noun == "project" and verb == "item-edit":
        print("ok")
        return 0

    print(f"gh: unhandled invocation: {args!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
