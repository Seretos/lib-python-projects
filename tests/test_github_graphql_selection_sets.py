"""Ticket #257: validate the review-comment mutations' GraphQL selection
sets against a checked-in GitHub introspection snapshot
(`tests/github_graphql_schema.json`), instead of only ever exercising them
against this repo's own self-authored HTTP mocks.

A self-authored mock can only ever confirm "the code asks for whatever
field name is baked into the mock" -- it has no independent knowledge of
what GitHub's schema actually allows. That is exactly how `diffSide` ended
up selected on `PullRequestReviewComment` (it is a `PullRequestReviewThread`
field, not a `PullRequestReviewComment` one) in both
`_ADD_REVIEW_THREAD_MUTATION` and `_ADD_REVIEW_COMMENT_REPLY_MUTATION`,
without either mutation ever failing a test -- right up until GitHub's live
API rejected both with a 400.

This module is a small, stdlib-only recursive-descent parser over the
compact GraphQL query strings the provider builds, plus a validator that
walks each selected field against the snapshot's `{TypeName: {fieldName:
resultTypeName}}` field maps.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib_python_projects.providers import github as github_mod

_SCHEMA_PATH = Path(__file__).parent / "github_graphql_schema.json"
_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _skip_parens(text: str, pos: int) -> int:
    """`text[pos] == '('`. Returns the index just past the matching `)`.

    Only `(`/`)` are counted -- an `input:{...}` object argument's `{`/`}`
    braces pass through untouched, so they are never mistaken for the
    field's own sub-selection (which only ever starts right after the
    matching `)`)."""
    depth = 0
    while True:
        c = text[pos]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1


def _parse_selection_set(text: str, pos: int) -> tuple[list[dict], int]:
    """`text[pos] == '{'`. Returns `(fields, index_after_closing_brace)`.

    Each field is `{"name": str, "children": list[dict] | None}` --
    `children` is `None` for a leaf field (no sub-selection)."""
    assert text[pos] == "{"
    pos += 1
    fields: list[dict] = []
    while True:
        while text[pos].isspace():
            pos += 1
        if text[pos] == "}":
            return fields, pos + 1
        m = _FIELD_NAME_RE.match(text, pos)
        assert m, f"expected a field name at position {pos}: {text[pos:pos + 30]!r}"
        name = m.group(0)
        pos = m.end()
        if pos < len(text) and text[pos] == "(":
            pos = _skip_parens(text, pos)
        while pos < len(text) and text[pos].isspace():
            pos += 1
        children = None
        if pos < len(text) and text[pos] == "{":
            children, pos = _parse_selection_set(text, pos)
        fields.append({"name": name, "children": children})


def _root_selection_set(query: str) -> list[dict]:
    """Strip the leading `mutation(...)` / `query(...)` operation header
    and return the top-level selection set's fields."""
    m = re.match(r"\s*(mutation|query)", query)
    assert m, f"expected the query to start with 'mutation'/'query': {query[:30]!r}"
    pos = m.end()
    if pos < len(query) and query[pos] == "(":
        pos = _skip_parens(query, pos)
    while query[pos].isspace():
        pos += 1
    fields, _ = _parse_selection_set(query, pos)
    return fields


def _validate(fields: list[dict], type_name: str, types: dict, path: str) -> None:
    """Recursively assert every selected field name exists on the type
    reached by traversal.

    A field with a sub-selection whose result type is missing from the
    snapshot is a **failure**, not a silently-skipped case -- a validator
    that shrugs at an unknown type would defeat the whole point of
    grounding this in a real schema instead of the mocks."""
    type_fields = types.get(type_name)
    assert type_fields is not None, (
        f"type {type_name!r} is not in the schema snapshot (path: {path}) -- "
        "add it to tests/github_graphql_schema.json rather than skipping it"
    )
    for field in fields:
        name = field["name"]
        assert name in type_fields, (
            f"field {name!r} does not exist on {type_name} (path: {path})"
        )
        if field["children"] is not None:
            _validate(field["children"], type_fields[name], types, f"{path}.{name}")


# ---------- R1 driving test --------------------------------------------------


def test_review_comment_mutations_only_select_existing_fields() -> None:
    """Every field selected by `_ADD_REVIEW_THREAD_MUTATION` and
    `_ADD_REVIEW_COMMENT_REPLY_MUTATION` must exist on the GraphQL type
    reached by traversing the schema snapshot from `Mutation`.

    RED reason (current code): `_REVIEW_COMMENT_NODE_FIELDS` selects
    `diffSide` on the comment node in both mutations
    (`.../nodes{...diffSide...}` for the thread mutation, `comment{...
    diffSide...}` for the reply mutation), but `diffSide` is a field of
    `PullRequestReviewThread`, not `PullRequestReviewComment` -- the
    snapshot has no `diffSide` entry under `PullRequestReviewComment`, so
    traversal into that selection fails on it with
    `AssertionError: field 'diffSide' does not exist on
    PullRequestReviewComment`. This is the exact shape of the production
    400 GitHub returns today for both `add_pr_review_comment` code paths.
    """
    schema = _load_schema()
    types = schema["types"]
    for query in (
        github_mod._ADD_REVIEW_THREAD_MUTATION,
        github_mod._ADD_REVIEW_COMMENT_REPLY_MUTATION,
    ):
        _validate(_root_selection_set(query), "Mutation", types, "Mutation")


# ---------- edge-case coverage: the validator itself is not vacuous --------


def test_validator_rejects_an_invented_field_name() -> None:
    """Proves the validator actually looks at field names rather than
    passing vacuously: an invented field name must be rejected.

    Test-critic round 1: goes through the real parsing entry point
    (`_root_selection_set`, which drives `_parse_selection_set`) on an
    actual (deliberately corrupted) GraphQL query string, instead of
    hand-building the `{"name": ..., "children": ...}` shape directly --
    an under-parsing implementation (e.g. one that silently returns an
    empty selection list instead of actually walking the query text)
    would make this test fail rather than vacuously pass, since `fields`
    is now produced by the parser under test rather than authored by
    hand."""
    types = {"Mutation": {"foo": "FooType"}, "FooType": {"bar": "String"}}
    query = "mutation{foo{notReal}}"
    fields = _root_selection_set(query)
    with pytest.raises(AssertionError, match="notReal"):
        _validate(fields, "Mutation", types, "Mutation")


def test_validator_rejects_a_subselection_on_a_type_missing_from_the_snapshot() -> None:
    """Proves a sub-selection into a type absent from the snapshot fails
    loudly instead of being silently skipped (per the plan: "a field with
    a sub-selection whose result type is missing from the snapshot is a
    failure, not a skip").

    Test-critic round 1: same rationale as the sibling test above -- the
    selection is produced by parsing an actual query string through the
    real entry point rather than hand-built, so an under-parsing
    implementation would fail this test instead of passing vacuously."""
    types = {"Mutation": {"foo": "TypeNotInSnapshot"}}
    query = "mutation{foo{bar}}"
    fields = _root_selection_set(query)
    with pytest.raises(AssertionError, match="TypeNotInSnapshot"):
        _validate(fields, "Mutation", types, "Mutation")
