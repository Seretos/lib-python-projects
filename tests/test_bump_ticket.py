"""Driving tests for R1/R2/R4 (#243 generation 2, bundling #230's
changelog-in-body requirement) -- the not-yet-existing ``ci.bump_ticket``
module, replacing
``.github/actions/file-consumer-ticket/file-ticket.sh``.

Entry-point API decision (documented here since the plan asked for it to be
made explicit): ``ci.bump_ticket.run(env: dict[str, str]) -> int``. It reads
its inputs (``VERSION``, ``SOURCE_REPO``, ``CONSUMER_REPO``, ``GH_TOKEN``,
``GITHUB_OUTPUT``) from the ``env`` mapping passed in -- this is what makes
it unit-testable without touching ``os.environ`` -- and calls
``ci.gh.run_gh`` for every `gh` invocation, so tests intercept those calls
via ``monkeypatch.setattr(ci.gh, "run_gh", fake.run_gh)``. A thin
``main()`` (not exercised here) is expected to call
``run_main(lambda: run(os.environ))`` at ``python3 -m ci.bump_ticket`` time
-- that's Layer 2 / R5 territory, out of scope for this dispatch. Module
scope also exports ``REQUIRED_ENV`` (asserted against the action.yml
contract in R5, also out of scope here).

RED reason for every test below: ``ci.bump_ticket`` (and, transitively,
``ci.gh``) do not exist yet in this dispatch -- ``ModuleNotFoundError`` at
fixture setup. Imports are lazy (inside the fixture, not at module level)
so this file collects cleanly and each test fails individually for the
same, correctly-attributed reason.

Do not implement ``ci/`` in this dispatch; that is a separate
(``phase=implement``) dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fake_gh import FakeGitHub

PACKAGE_NAME = "lib-python-projects"
SOURCE_REPO = "Seretos/lib-python-projects"
VERSION = "0.2.0"
TAG = f"v{VERSION}"
EXPECTED_TITLE = f"chore(deps): bump {PACKAGE_NAME} to {TAG}"

CONSUMER_A = "Seretos/agent-project-issues"
CONSUMER_B = "Seretos/workboard"


@pytest.fixture
def fake(monkeypatch):
    """A fresh FakeGitHub wired in as ci.gh.run_gh. RED via
    ModuleNotFoundError: No module named 'ci' until ci.gh exists."""
    import ci.gh  # noqa: PLC0415 -- deliberately lazy, see module docstring

    sim = FakeGitHub()
    monkeypatch.setattr(ci.gh, "run_gh", sim.run_gh)
    return sim


def _base_env(tmp_path: Path, *, consumer_repo: str, version: str = VERSION, source_repo: str = SOURCE_REPO) -> dict[str, str]:
    github_output = tmp_path / "github_output.txt"
    github_output.write_text("", encoding="utf-8")
    return {
        "VERSION": version,
        "SOURCE_REPO": source_repo,
        "CONSUMER_REPO": consumer_repo,
        "GH_TOKEN": "fake-token",
        "GITHUB_OUTPUT": str(github_output),
    }


def _read_outputs(github_output_path: str) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for line in Path(github_output_path).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            outputs[k] = v
    return outputs


def _run(env: dict[str, str]) -> int:
    import ci.bump_ticket  # noqa: PLC0415

    return ci.bump_ticket.run(env)


# ---------------------------------------------------------------------
# R1 -- changelog verbatim under "### What changed", before
# "### Action required"
# ---------------------------------------------------------------------


def test_body_embeds_release_notes_verbatim_between_headings(fake, tmp_path):
    release_notes = (
        "## v0.2.0\n\n"
        "- Added the `frobnicator()` helper\n\n"
        "```python\n"
        "def frobnicate(x):\n"
        "    return x * 2\n"
        "```\n\n"
        "- Honours $HOME and a literal backslash: \\\n"
        "- A line that just says EOF, on its own\n"
        "EOF\n"
    )
    fake.add_release(SOURCE_REPO, TAG, release_notes)
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code == 0
    bodies = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)
    assert bodies, f"expected an issue titled {EXPECTED_TITLE!r} to have been created"
    body = bodies[0]

    assert release_notes in body, f"release notes must appear verbatim in the body; body={body!r}"
    assert "### What changed" in body
    assert "### Action required" in body
    assert body.index("### What changed") < body.index("### Action required")

    between_headings = body[body.index("### What changed") : body.index("### Action required")]
    assert release_notes in between_headings, (
        "release notes must sit literally between the two headings, not "
        f"merely somewhere earlier in the body; between_headings={between_headings!r}"
    )

    release_calls = [c for c in fake.calls if c[:2] == ["release", "view"]]
    assert release_calls, f"expected a gh release view call; calls={fake.calls!r}"
    release_call = release_calls[0]
    assert TAG in release_call, f"gh release view must be called with the {TAG!r} tag; call={release_call!r}"
    repo_flag_index = release_call.index("--repo")
    assert release_call[repo_flag_index + 1] == SOURCE_REPO, (
        f"gh release view must be called with --repo {SOURCE_REPO!r}; call={release_call!r}"
    )

    outputs = _read_outputs(env["GITHUB_OUTPUT"])
    assert outputs.get("issue_url") == fake.issues_matching(CONSUMER_A, EXPECTED_TITLE)[0]["url"]


def test_title_and_action_required_block_are_intact(fake, tmp_path):
    fake.add_release(SOURCE_REPO, TAG, "notes")
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    _run(env)

    bodies = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)
    assert bodies
    body = bodies[0]

    assert TAG in body, "the pin line must reference the version tag"
    assert f"{PACKAGE_NAME} @ git+https://github.com/{SOURCE_REPO}@{TAG}" in body
    assert "### Action required" in body
    action_required = body[body.index("### Action required") :]
    assert "1." in action_required
    assert "2." in action_required
    assert "3." in action_required
    assert "4." in action_required


def test_both_consumers_produce_byte_identical_bodies(fake, tmp_path):
    fake.add_release(SOURCE_REPO, TAG, "identical notes across consumers")

    (tmp_path / "a").mkdir(exist_ok=True)
    (tmp_path / "b").mkdir(exist_ok=True)
    env_a = _base_env(tmp_path / "a", consumer_repo=CONSUMER_A)
    env_b = _base_env(tmp_path / "b", consumer_repo=CONSUMER_B)

    _run(env_a)
    _run(env_b)

    body_a = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)[0]
    body_b = fake.created_issue_bodies(CONSUMER_B, EXPECTED_TITLE)[0]
    assert body_a == body_b


def test_over_budget_release_notes_are_truncated_with_release_page_link(fake, tmp_path):
    huge_notes = "x" * 70_000
    fake.add_release(SOURCE_REPO, TAG, huge_notes)
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code == 0
    bodies = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)
    assert bodies, "the issue must still be created even when the changelog is huge"
    body = bodies[0]
    # Below the untruncated length (not above -- an untruncated body here
    # would be ~70000 + a few hundred chars of scaffolding, still well under
    # "+5000"; a threshold above the untruncated length would pass even a
    # missing-truncation implementation, defeating the point of this test.
    # #243 test-critic finding 2.
    assert len(body) < len(huge_notes) - 5_000, "body must be truncated, not carry the full 70000-char blob"
    release_page_url = f"https://github.com/{SOURCE_REPO}/releases/tag/{TAG}"
    assert release_page_url in body, "truncated body must still link to the full release notes"
    assert "truncated" in body.lower()


# ---------------------------------------------------------------------
# R2 -- missing/empty release -> ::warning:: + release-page link fallback
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_repo,version",
    [
        (SOURCE_REPO, VERSION),
        ("Seretos/some-other-lib", "1.4.2"),
    ],
)
def test_missing_release_warns_and_links_to_release_page(fake, tmp_path, source_repo, version, capsys):
    # Deliberately do NOT seed a release for (source_repo, f"v{version}") --
    # the simulator then answers "not found" exactly like a real 404,
    # rather than a blanket canned response.
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A, version=version, source_repo=source_repo)

    exit_code = _run(env)

    captured = capsys.readouterr()
    assert "::warning::" in captured.out, f"expected ::warning:: on stdout; stdout={captured.out!r}"

    tag = f"v{version}"
    # The package name in the title/body must track the *actual* source_repo
    # passed in this parametrize case, not the module-level PACKAGE_NAME
    # constant (which only describes the default/first case) -- mirrors the
    # production `source_repo.split("/", 1)[-1]` derivation.
    package_name = source_repo.split("/", 1)[-1]
    release_page_url = f"https://github.com/{source_repo}/releases/tag/{tag}"
    bodies = fake.created_issue_bodies(CONSUMER_A, f"chore(deps): bump {package_name} to {tag}")
    assert bodies, "the ticket must still be filed when the release lookup fails"
    body = bodies[0]
    assert release_page_url in body
    what_changed_idx = body.index("### What changed")
    assert what_changed_idx < body.index(release_page_url)

    assert exit_code == 0


def test_release_body_null_takes_fallback_without_leaking_literal_null(fake, tmp_path, capsys):
    fake.add_release(SOURCE_REPO, TAG, None)  # models `{"body": null}`
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code == 0
    body = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)[0]
    assert "null" not in body.lower()
    release_page_url = f"https://github.com/{SOURCE_REPO}/releases/tag/{TAG}"
    assert release_page_url in body
    # The release-page link could in principle appear unconditionally in a
    # template regardless of whether the fallback path actually triggered --
    # the ::warning:: annotation is the signal that it genuinely did.
    # #243 test-critic finding 3.
    captured = capsys.readouterr()
    assert "::warning::" in captured.out, f"expected ::warning:: on stdout; stdout={captured.out!r}"


def test_whitespace_only_release_body_takes_fallback(fake, tmp_path, capsys):
    fake.add_release(SOURCE_REPO, TAG, "   \n\t  \n  ")
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code == 0
    body = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)[0]
    release_page_url = f"https://github.com/{SOURCE_REPO}/releases/tag/{TAG}"
    assert release_page_url in body
    # #243 test-critic finding 3: assert the warning actually fired, not
    # just that the fallback link is present.
    captured = capsys.readouterr()
    assert "::warning::" in captured.out, f"expected ::warning:: on stdout; stdout={captured.out!r}"


# ---------------------------------------------------------------------
# R4 -- idempotency (beyond the first page, PR/superstring-title
# exclusion) and label fallback, failures made loud
# ---------------------------------------------------------------------


def test_existing_open_issue_is_reused_even_beyond_the_first_page(fake, tmp_path):
    fake.add_release(SOURCE_REPO, TAG, "some notes")

    # Both decoys are seeded BEFORE the genuine match (lower index / earlier
    # page) so a naive first-match-wins probe without proper PR-exclusion
    # and exact-title-matching would latch onto one of these and fail this
    # test, instead of silently passing regardless of exclusion logic.
    # #243 test-critic finding 4.
    # Decoy PR with the exact same title -- issues search must exclude PRs.
    fake.add_open_issue(CONSUMER_A, EXPECTED_TITLE, is_pull_request=True)
    # Decoy issue whose title is a superstring of the real one -- must not
    # fuzzy-match.
    fake.add_open_issue(CONSUMER_A, EXPECTED_TITLE + " (backport)")

    existing_url = None
    for i in range(250):
        title = EXPECTED_TITLE if i == 210 else f"decoy issue #{i}"
        url = fake.add_open_issue(CONSUMER_A, title)
        if i == 210:
            existing_url = url
    assert existing_url is not None

    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code == 0
    create_calls = [c for c in fake.calls if c[:2] == ["issue", "create"]]
    assert create_calls == [], f"an existing open issue must be reused, not recreated; calls={create_calls!r}"

    outputs = _read_outputs(env["GITHUB_OUTPUT"])
    assert outputs.get("issue_url") == existing_url
    # $GITHUB_OUTPUT must carry exactly one line for issue_url.
    lines = [l for l in Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8").splitlines() if l.startswith("issue_url=")]
    assert len(lines) == 1, f"expected exactly one issue_url= line; got {lines!r}"


@pytest.mark.parametrize("consumer_repo", [CONSUMER_A, CONSUMER_B])
def test_idempotency_reuses_existing_issue_for_both_consumers(fake, tmp_path, consumer_repo):
    fake.add_release(SOURCE_REPO, TAG, "notes")
    existing_url = fake.add_open_issue(consumer_repo, EXPECTED_TITLE)
    env = _base_env(tmp_path, consumer_repo=consumer_repo)

    exit_code = _run(env)

    assert exit_code == 0
    create_calls = [c for c in fake.calls if c[:2] == ["issue", "create"]]
    assert create_calls == []
    outputs = _read_outputs(env["GITHUB_OUTPUT"])
    assert outputs.get("issue_url") == existing_url


def test_closed_issue_with_same_title_is_not_reused(fake, tmp_path):
    fake.add_release(SOURCE_REPO, TAG, "notes")
    fake.add_closed_issue(CONSUMER_A, EXPECTED_TITLE)
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code == 0
    create_calls = [c for c in fake.calls if c[:2] == ["issue", "create"]]
    assert len(create_calls) == 1, (
        f"a closed issue with the same title must not be reused -- a new one "
        f"must be created; calls={create_calls!r}"
    )


def test_label_missing_falls_back_to_unlabelled_create(fake, tmp_path):
    fake.add_release(SOURCE_REPO, TAG, "notes")
    fake.fail_labelled_create(CONSUMER_A)
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code == 0
    create_calls = [c for c in fake.calls if c[:2] == ["issue", "create"]]
    assert len(create_calls) == 2, (
        f"expected a labelled attempt followed by an unlabelled fallback; calls={create_calls!r}"
    )
    assert "--label" in create_calls[0]
    assert "--label" not in create_calls[1]

    bodies = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)
    assert len(bodies) == 1, "exactly one issue must actually have been created (the unlabelled attempt)"

    outputs = _read_outputs(env["GITHUB_OUTPUT"])
    assert outputs.get("issue_url") == fake.issues_matching(CONSUMER_A, EXPECTED_TITLE)[0]["url"]


def test_both_create_attempts_failing_errors_out_without_writing_issue_url(fake, tmp_path, capsys):
    fake.add_release(SOURCE_REPO, TAG, "notes")
    fake.fail_all_creates(CONSUMER_A)
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "::error::" in captured.out, f"expected ::error::; stdout={captured.out!r}"

    outputs = _read_outputs(env["GITHUB_OUTPUT"])
    assert "issue_url" not in outputs, f"issue_url must not be written when creation failed; outputs={outputs!r}"


def test_idempotency_pagination_exhaustion_refuses_to_create_a_possible_duplicate(fake, tmp_path, monkeypatch, capsys):
    """Distinct from test_idempotency_probe_failure_warns_and_falls_through_to_creation
    below: a genuine gh_paginate_rest page-cap exhaustion means the
    existing-open-issues list is known-incomplete, not just that one `gh
    api` call blipped. Silently falling through to creation in that case
    risks filing a duplicate bump ticket -- worse than the plain-failure
    fallback below, where the API simply couldn't be reached at all (#243
    round 2 blocking finding 3)."""
    import ci.gh  # noqa: PLC0415

    fake.add_release(SOURCE_REPO, TAG, "notes")

    real_run_gh = fake.run_gh

    def exhausting_probe(args, *, check=True):
        if args and args[0] == "api":
            raise ci.gh.GhPaginationExhausted("simulated page-cap exhaustion")
        return real_run_gh(args, check=check)

    monkeypatch.setattr(ci.gh, "run_gh", exhausting_probe)
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "::error::" in captured.out, f"expected ::error::; stdout={captured.out!r}"

    create_calls = [c for c in fake.calls if c[:2] == ["issue", "create"]]
    assert create_calls == [], (
        "pagination exhaustion must never fall through to creation -- that "
        f"risks filing a duplicate bump ticket; calls={create_calls!r}"
    )
    outputs = _read_outputs(env["GITHUB_OUTPUT"])
    assert "issue_url" not in outputs, f"issue_url must not be written when the probe couldn't complete; outputs={outputs!r}"


def test_idempotency_probe_failure_warns_and_falls_through_to_creation(fake, tmp_path, monkeypatch, capsys):
    """Simulate the `gh api` idempotency probe itself erroring out (e.g. a
    transient API failure unrelated to whether the issue exists) --  must
    never silently succeed with no ticket filed; it warns and still
    attempts creation."""
    import ci.gh  # noqa: PLC0415

    fake.add_release(SOURCE_REPO, TAG, "notes")

    real_run_gh = fake.run_gh

    def failing_probe(args, *, check=True):
        if args and args[0] == "api":
            if check:
                raise ci.gh.GhError("simulated transient api failure")
            return ""
        return real_run_gh(args, check=check)

    monkeypatch.setattr(ci.gh, "run_gh", failing_probe)
    env = _base_env(tmp_path, consumer_repo=CONSUMER_A)

    exit_code = _run(env)

    captured = capsys.readouterr()
    assert "::warning::" in captured.out, f"expected ::warning:: when the idempotency probe fails; stdout={captured.out!r}"
    assert exit_code == 0
    bodies = fake.created_issue_bodies(CONSUMER_A, EXPECTED_TITLE)
    assert bodies, "a failing idempotency probe must fall through to creation, never a silent no-op"
