"""Tests for ticket #265 -- every affected write method documents the
`light=True` contract (plan R6) via a **parseable, structured block**,
mirroring the plan's own format: a literal marker line followed by
labelled `Returns:`/`Source:`/`None:`/`Labels:`/`Replay:` lines.

This file was rewritten (plan's Affected-files: "today's prose/proximity
matchers are the tautology trap") to replace `_mentions_together`/
`_mentions_field` free-prose proximity matching with a real parser tied
back to the dataclasses/signatures the docstrings describe. A docstring
that merely mentions the right words in the right neighbourhood no
longer passes -- it must supply a `Returns:`/`None:` field list whose
union is checked against `dataclasses.fields()`, a `Source:` line
containing the two required literal tokens, and (create methods only) a
`Replay:` line naming both replay directions.

None of the 18 docstrings carry this block today (no docstring mentions
`light` at all), so every assertion here is expected RED until
phase=implement documents each method in this exact format.
"""
from __future__ import annotations

import dataclasses
import inspect
import re

import pytest

from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers.base import CommentRef, PullRequestRef, TicketRef
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider

GH = GitHubProvider
GL = GitLabProvider
ADO = AzureDevOpsProvider

PROVIDERS = [GH, GL, ADO]
WRITE_METHODS = [
    "create_ticket", "update_ticket", "add_comment",
    "create_pr", "update_pr", "merge_pr",
]
CREATE_METHODS = ["create_ticket", "create_pr"]

REF_TYPE_FOR_METHOD: dict[str, type] = {
    "create_ticket": TicketRef,
    "update_ticket": TicketRef,
    "add_comment": CommentRef,
    "create_pr": PullRequestRef,
    "update_pr": PullRequestRef,
    "merge_pr": PullRequestRef,
}

# ---------------------------------------------------------------------------
# The plan's own per-(provider, method) field-sourcing rules (R1-R4's
# "Behaviour" bullets), restated here as the disjoint Returns/None
# partition the light docstring block must declare. A field belongs in
# `returns` when the plan documents it as populated from this call's own
# write response under the method's normal/designed outcome; it belongs
# in `none` when the plan documents it as structurally unsourceable (AC4)
# -- always, or (GitHub update_ticket's board-only outcome, ADO merge_pr's
# still-unsettled outcome) conditionally, with the condition itself
# documented in prose via `Source:`/`None:`.
#
# Every entry's two sets must partition the ref class's real
# `dataclasses.fields()` names exactly -- enforced below, not just
# asserted here, so a future field addition/removal on the dataclass
# without a matching docstring update fails the suite.
# ---------------------------------------------------------------------------

_COMMENT_ALL = {"id", "url", "created_at"}
_TICKET_ALL = {"id", "url", "status", "labels", "updated_at", "custom_fields", "idempotent_replay"}
_PR_ALL = {
    "id", "number", "url", "state", "merged", "head_sha",
    "mergeable_state", "warnings", "idempotent_replay",
}

FIELD_PARTITION: dict[tuple[type, str], dict[str, set[str]]] = {
    # ---- add_comment: id/url/created_at always sourced from the create
    # response on every provider (R3) -- nothing is ever None.
    (GH, "add_comment"): {"returns": _COMMENT_ALL, "none": set()},
    (GL, "add_comment"): {"returns": _COMMENT_ALL, "none": set()},
    (ADO, "add_comment"): {"returns": _COMMENT_ALL, "none": set()},

    # ---- create_ticket: every provider always returns id/url/status/
    # labels/updated_at from the create response; custom_fields is None
    # only when this call wrote none (R4).
    (GH, "create_ticket"): {
        "returns": {"id", "url", "status", "labels", "updated_at", "idempotent_replay"},
        "none": {"custom_fields"},
    },
    (GL, "create_ticket"): {
        "returns": {"id", "url", "status", "labels", "updated_at", "idempotent_replay"},
        "none": {"custom_fields"},
    },
    (ADO, "create_ticket"): {
        "returns": {"id", "url", "status", "labels", "updated_at", "idempotent_replay"},
        "none": {"custom_fields"},
    },

    # ---- update_ticket: GitHub's board-only/no-write outcomes (b'/c)
    # leave url/status/labels/updated_at/custom_fields None (R2); GitLab/
    # ADO are already reload-free and always populate them, with only
    # custom_fields conditional on this call having written some.
    (GH, "update_ticket"): {
        "returns": {"id", "idempotent_replay"},
        "none": {"url", "status", "labels", "updated_at", "custom_fields"},
    },
    (GL, "update_ticket"): {
        "returns": {"id", "url", "status", "labels", "updated_at", "idempotent_replay"},
        "none": {"custom_fields"},
    },
    (ADO, "update_ticket"): {
        "returns": {"id", "url", "status", "labels", "updated_at", "idempotent_replay"},
        "none": {"custom_fields"},
    },

    # ---- create_pr: GitHub's create response is a full PR object, so
    # every AC1 field (including mergeable_state) is genuinely populated
    # (R4). GitLab/ADO never populate mergeable_state (base.py:685 /
    # the ADO equivalent).
    (GH, "create_pr"): {"returns": set(_PR_ALL), "none": set()},
    (GL, "create_pr"): {
        "returns": _PR_ALL - {"mergeable_state"}, "none": {"mergeable_state"},
    },
    (ADO, "create_pr"): {
        "returns": _PR_ALL - {"mergeable_state"}, "none": {"mergeable_state"},
    },

    # ---- update_pr: GitHub's no-PATCH outcome is identity-only (R4) --
    # id/url/state/merged/head_sha/mergeable_state all None, only number/
    # warnings/idempotent_replay are guaranteed. GitLab/ADO always PATCH
    # a full PR-shape response; only mergeable_state is never populated.
    (GH, "update_pr"): {
        "returns": {"number", "warnings", "idempotent_replay"},
        "none": {"id", "url", "state", "merged", "head_sha", "mergeable_state"},
    },
    (GL, "update_pr"): {
        "returns": _PR_ALL - {"mergeable_state"}, "none": {"mergeable_state"},
    },
    (ADO, "update_pr"): {
        "returns": _PR_ALL - {"mergeable_state"}, "none": {"mergeable_state"},
    },

    # ---- merge_pr: GitHub's merge PUT body is `{sha, merged, message}`
    # -- only number (echoed)/merged are ever populated, the other five
    # AC1 fields are structurally unsourceable (R1: "the other five
    # None"). GitLab's merge PUT returns the full MR (all AC1 fields
    # except mergeable_state, which GitLab never populates). ADO's merge
    # response is a PR-shape object for everything except `merged`,
    # which is None whenever the one conditional status read still shows
    # the merge unsettled (R1) -- documented, not raised.
    (GH, "merge_pr"): {
        "returns": {"number", "merged", "warnings", "idempotent_replay"},
        "none": {"id", "url", "state", "head_sha", "mergeable_state"},
    },
    (GL, "merge_pr"): {
        "returns": _PR_ALL - {"mergeable_state"}, "none": {"mergeable_state"},
    },
    (ADO, "merge_pr"): {
        "returns": _PR_ALL - {"mergeable_state", "merged"},
        "none": {"mergeable_state", "merged"},
    },
}

assert set(FIELD_PARTITION) == {
    (p, m) for p in PROVIDERS for m in WRITE_METHODS
}, "FIELD_PARTITION must cover exactly the 18 (provider, method) pairs"
for _key, _parts in FIELD_PARTITION.items():
    _all_fields = {
        TicketRef: _TICKET_ALL, CommentRef: _COMMENT_ALL, PullRequestRef: _PR_ALL,
    }[REF_TYPE_FOR_METHOD[_key[1]]]
    assert _parts["returns"] | _parts["none"] == _all_fields, _key
    assert _parts["returns"] & _parts["none"] == set(), _key


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

_MARKER = "Light mode (`light=True`)"
_LABEL_PATTERNS = {
    "Returns": re.compile(r"^[ \t]*Returns:[ \t]*(.*)$", re.MULTILINE),
    "Source": re.compile(r"^[ \t]*Source:[ \t]*(.*)$", re.MULTILINE),
    "None": re.compile(r"^[ \t]*None:[ \t]*(.*)$", re.MULTILINE),
    "Labels": re.compile(r"^[ \t]*Labels:[ \t]*(.*)$", re.MULTILINE),
    "Replay": re.compile(r"^[ \t]*Replay:[ \t]*(.*)$", re.MULTILINE),
}
_RETURNS_SHAPE_RE = re.compile(r"^(\w+)\(([^)]*)\)")


def _doc(cls: type, method_name: str) -> str:
    doc = inspect.getdoc(getattr(cls, method_name))
    assert doc is not None, f"{cls.__name__}.{method_name} has no docstring at all"
    return doc


def _assert_has_light_bool_parameter(cls: type, method_name: str) -> None:
    """Ties the "documents `light`" claim to the method's real signature.
    `light` doesn't exist on any provider method yet, so this raises here
    -- a valid, distinct RED reason from the `TypeError` in
    `test_write_light_mode.py`, both genuine evidence the parameter is
    missing."""
    method = getattr(cls, method_name)
    sig = inspect.signature(method)
    assert "light" in sig.parameters, (
        f"{cls.__name__}.{method_name} must declare a `light` parameter"
    )
    param = sig.parameters["light"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{cls.__name__}.{method_name}'s `light` parameter must be keyword-only"
    )
    assert param.default is False, (
        f"{cls.__name__}.{method_name}'s `light` parameter must default to "
        f"False (opt-in), got {param.default!r}"
    )


def _light_block(doc: str, cls: type, method_name: str) -> str:
    idx = doc.find(_MARKER)
    assert idx != -1, (
        f'{cls.__name__}.{method_name} has no "{_MARKER}" block in its docstring'
    )
    return doc[idx:]


def _parse_block(block: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for name, pattern in _LABEL_PATTERNS.items():
        m = pattern.search(block)
        if m:
            parsed[name] = m.group(1).strip()
    return parsed


def _parse_returns(raw: str) -> tuple[str, set[str]]:
    m = _RETURNS_SHAPE_RE.match(raw)
    assert m is not None, (
        f'Returns: line must be shaped "ClassName(field1, field2, ...)", got {raw!r}'
    )
    class_name = m.group(1)
    fields = {f.strip() for f in m.group(2).split(",") if f.strip()}
    return class_name, fields


def _parse_none(raw: str) -> set[str]:
    if raw.strip().lower() == "none":
        return set()
    return {f.strip() for f in raw.split(",") if f.strip()}


# ---------------------------------------------------------------------------
# The 18-way structural check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_cls", PROVIDERS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("method_name", WRITE_METHODS)
def test_light_block_parses_and_matches_ref_dataclass(
    provider_cls: type, method_name: str,
) -> None:
    _assert_has_light_bool_parameter(provider_cls, method_name)
    doc = _doc(provider_cls, method_name)
    block = _light_block(doc, provider_cls, method_name)
    parsed = _parse_block(block)

    for label in ("Returns", "Source", "None", "Labels"):
        assert label in parsed, (
            f"{provider_cls.__name__}.{method_name}'s light block is missing "
            f"a `{label}:` line"
        )

    ref_type = REF_TYPE_FOR_METHOD[method_name]
    class_name, returns_fields = _parse_returns(parsed["Returns"])
    assert class_name == ref_type.__name__, (
        f"{provider_cls.__name__}.{method_name}'s Returns: line must name "
        f"{ref_type.__name__}, got {class_name!r}"
    )
    # Tie "names its light result type" back to the method's REAL return
    # type via the signature, not just prose (test-critic CRITICAL 1 on
    # the original file's equivalent check).
    sig = inspect.signature(getattr(provider_cls, method_name))
    assert ref_type.__name__ in str(sig.return_annotation), (
        f"{provider_cls.__name__}.{method_name}'s return annotation must "
        f"mention {ref_type.__name__} (got {sig.return_annotation!r}) -- "
        "the Returns: line's class must match the real signature, not "
        "just be asserted in prose"
    )

    none_fields = _parse_none(parsed["None"])
    real_fields = {f.name for f in dataclasses.fields(ref_type)}
    for name in returns_fields | none_fields:
        assert name in real_fields, (
            f"{provider_cls.__name__}.{method_name} names {name!r} in its "
            f"light block, but {ref_type.__name__} has no such field "
            f"(real fields: {sorted(real_fields)})"
        )
    assert returns_fields | none_fields == real_fields, (
        f"{provider_cls.__name__}.{method_name}'s Returns: + None: fields "
        f"must cover every {ref_type.__name__} field exactly once -- "
        f"missing: {sorted(real_fields - (returns_fields | none_fields))}, "
        f"unknown: {sorted((returns_fields | none_fields) - real_fields)}"
    )
    assert returns_fields & none_fields == set(), (
        f"{provider_cls.__name__}.{method_name} lists "
        f"{sorted(returns_fields & none_fields)} in BOTH Returns: and "
        "None: -- each field belongs in exactly one"
    )

    source_lower = parsed["Source"].lower()
    assert "no reload" in source_lower, (
        f"{provider_cls.__name__}.{method_name}'s Source: line must "
        "contain the literal substring 'no reload'"
    )
    assert "pre-cascade" in source_lower, (
        f"{provider_cls.__name__}.{method_name}'s Source: line must "
        "contain the literal substring 'pre-cascade'"
    )

    assert parsed["Labels"], (
        f"{provider_cls.__name__}.{method_name}'s Labels: line must not be empty"
    )


@pytest.mark.parametrize("provider_cls", PROVIDERS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("method_name", WRITE_METHODS)
def test_light_block_field_partition_matches_the_plan(
    provider_cls: type, method_name: str,
) -> None:
    """Beyond "covers every field exactly once" (structural, above), pin
    down WHICH fields land in Returns: vs None: per the plan's own R1-R4
    per-(provider, method) field lists (`FIELD_PARTITION`) -- so a
    docstring that mis-classifies, say, GitHub merge_pr's `state` as
    Returns: (rather than the documented None:, since the merge PUT body
    carries no PR state at all) is a test failure, not a silent pass
    just because SOME valid-looking partition was supplied."""
    doc = _doc(provider_cls, method_name)
    block = _light_block(doc, provider_cls, method_name)
    parsed = _parse_block(block)
    _, returns_fields = _parse_returns(parsed["Returns"])
    none_fields = _parse_none(parsed["None"])

    expected = FIELD_PARTITION[(provider_cls, method_name)]
    assert returns_fields == expected["returns"], (
        f"{provider_cls.__name__}.{method_name}'s Returns: fields "
        f"{sorted(returns_fields)} must equal the plan's "
        f"{sorted(expected['returns'])}"
    )
    assert none_fields == expected["none"], (
        f"{provider_cls.__name__}.{method_name}'s None: fields "
        f"{sorted(none_fields)} must equal the plan's "
        f"{sorted(expected['none'])}"
    )


# ---------------------------------------------------------------------------
# update_ticket: labels are still applied/synced on a light column move
# (Q1 -> (a)) -- now checked against the Labels: line specifically,
# rather than a proximity scan of the whole docstring (GitLab's
# docstring already says "label" elsewhere for its unrelated
# add_labels/remove_labels parameters, which is exactly the false-
# positive risk a bare presence check has; the structured Labels: line
# doesn't have that problem since it's a dedicated field, not prose).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_cls", PROVIDERS, ids=lambda c: c.__name__)
def test_update_ticket_labels_line_documents_still_applied(provider_cls: type) -> None:
    doc = _doc(provider_cls, "update_ticket")
    block = _light_block(doc, provider_cls, "update_ticket")
    labels_line = _parse_block(block)["Labels"].lower()
    assert any(
        phrase in labels_line
        for phrase in ("still applied", "still added", "still synced")
    ), (
        f"{provider_cls.__name__}.update_ticket's Labels: line must state "
        "labels are STILL applied/synced on a light column move (Q1 -> "
        f"(a)), got {labels_line!r}"
    )


# ---------------------------------------------------------------------------
# Methods whose light path never touches labels at all state the
# literal fallback phrase the plan names ("Labels: none applied by this
# call") rather than leaving the line to accidentally satisfy the
# above's "still applied" check by omission.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_cls", PROVIDERS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("method_name", ["add_comment", "merge_pr"])
def test_no_label_methods_use_the_literal_none_applied_phrase(
    provider_cls: type, method_name: str,
) -> None:
    doc = _doc(provider_cls, method_name)
    block = _light_block(doc, provider_cls, method_name)
    labels_line = _parse_block(block)["Labels"].lower()
    assert "none applied by this call" in labels_line, (
        f"{provider_cls.__name__}.{method_name} never touches labels under "
        "light -- its Labels: line must use the literal phrase "
        f"'none applied by this call', got {labels_line!r}"
    )


# ---------------------------------------------------------------------------
# create_ticket / create_pr: Replay: names both replay directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_cls", PROVIDERS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("method_name", CREATE_METHODS)
def test_create_methods_replay_line_documents_both_directions(
    provider_cls: type, method_name: str,
) -> None:
    """The mixed-`light` idempotency-replay rule (`resolve_replay`): a
    `light=True` retry of a `light=True`-created key issues no new
    request; a `light=False` retry of that SAME key reloads once and
    returns the full model. Checked against the dedicated `Replay:` line
    -- no proximity scanning needed now that the block format gives the
    rule its own labelled line."""
    doc = _doc(provider_cls, method_name)
    block = _light_block(doc, provider_cls, method_name)
    replay_line = _parse_block(block)
    assert "Replay" in replay_line, (
        f"{provider_cls.__name__}.{method_name} (a create method) must "
        "carry a Replay: line"
    )
    text = replay_line["Replay"].lower()
    assert "no" in text and ("request" in text or "reload" in text), (
        f"{provider_cls.__name__}.{method_name}'s Replay: line must state "
        f"that a light->light replay issues no new request, got {text!r}"
    )
    assert "reload" in text, (
        f"{provider_cls.__name__}.{method_name}'s Replay: line must state "
        f"that a light=False retry of a light-created key reloads, got {text!r}"
    )
    assert "mixed" in text or "first call" in text, (
        f"{provider_cls.__name__}.{method_name}'s Replay: line must name "
        f"the MIXED-light rule specifically, got {text!r}"
    )


@pytest.mark.parametrize("provider_cls", PROVIDERS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("method_name", [m for m in WRITE_METHODS if m not in CREATE_METHODS])
def test_non_create_methods_have_no_replay_line(
    provider_cls: type, method_name: str,
) -> None:
    """Only the six create methods do idempotency replay -- a `Replay:`
    line on `update_ticket`/`add_comment`/`update_pr`/`merge_pr` would be
    documenting a rule that doesn't apply to them."""
    doc = _doc(provider_cls, method_name)
    block = _light_block(doc, provider_cls, method_name)
    parsed = _parse_block(block)
    assert "Replay" not in parsed, (
        f"{provider_cls.__name__}.{method_name} is not a create method and "
        "must not carry a Replay: line"
    )


# ---------------------------------------------------------------------------
# ADO merge_pr: the still-unsettled outcome and the handshake GET
# ---------------------------------------------------------------------------


def test_azuredevops_merge_pr_documents_unsettled_outcome_and_handshake_get() -> None:
    """ADO's light merge takes exactly one conditional status read and,
    if the merge is STILL unsettled after it, returns `merged=None` --
    it never raises the full-object poll's exhausted-retry
    `AzureDevOpsError(202)` and never polls further (plan's approach
    section: "the light path never raises 202 and never polls"). The
    docstring must document the outcome as `None`/"still unsettled", not
    as a 202 error -- an earlier draft of this test (mirroring an
    earlier draft of the behavioural test) asserted the docstring must
    name "the 202 'merge in progress' outcome", which described the
    FULL-object path's behaviour, not light's; corrected here to match
    the plan's own stated design (see change report)."""
    doc = _doc(AzureDevOpsProvider, "merge_pr")
    block = _light_block(doc, AzureDevOpsProvider, "merge_pr")
    parsed = _parse_block(block)
    doc_lower = doc.lower()
    assert "still unsettled" in parsed["None"].lower() or "unsettled" in doc_lower, (
        "must document the still-unsettled outcome"
    )
    assert "202" not in parsed.get("Source", "") + parsed.get("None", ""), (
        "must not describe light's still-unsettled outcome as a 202 error "
        "-- that's the full-object poll's behaviour, not light's"
    )
    assert "handshake" in doc_lower or "lastmergesourcecommit" in doc_lower, (
        "must name the handshake GET that the completion PATCH depends on"
    )
