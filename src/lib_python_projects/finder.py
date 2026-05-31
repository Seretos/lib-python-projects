"""Free-text project search with a relevance floor.

`find_projects` scores each `ProjectConfig` in a list against a query string
using per-field token-level F1 similarity (precision × recall harmonic mean),
then filters out any project whose best per-field score falls below `min_score`.

Scoring summary
---------------
For each candidate field (``id``, ``description``, ``path`` when
``fields="full"``; only ``id`` when ``fields="id"``):

1. Tokenise both the query and the field value by lowercasing and splitting on
   ``[-_/\\s]+``.
2. Compute *precision* (forward coverage): fraction of query tokens that have a
   ``difflib.SequenceMatcher`` ratio ≥ 0.8 with at least one field token.
3. Compute *recall* (backward coverage): fraction of field tokens that have a
   ratio ≥ 0.8 with at least one query token.
4. F1 = harmonic mean of precision and recall.

The project's final score is the maximum F1 across all scored fields, so a
strong match against *any* single field is sufficient.

This two-directional approach ensures:
- A nonsense query that shares only one common token (e.g. ``"project"``) with
  a project name scores poorly because most of its *other* tokens are unmatched
  (low precision).
- A short query like ``"agent"`` still scores well against a long id like
  ``"agent-project-issues"`` (forward coverage = 1.0) while being penalised
  for not covering the full id (partial backward coverage), giving a
  meaningful mid-range score above the default floor.
- An exact id query achieves F1 = 1.0.
"""
from __future__ import annotations

import difflib
import re
from typing import Sequence

from lib_python_projects.models import FindResult, ProjectConfig, ProjectMatch

DEFAULT_MIN_SCORE: float = 0.3

# When the best match clears this threshold we treat it as a high-confidence
# hit and drop any result scoring below `top_score * RELATIVE_SCORE_CUTOFF`.
# 0.7 sits above moderate fuzzy hits (a single-token partial match against a
# multi-token id scores ~0.4–0.5 F1) but below a full exact-id match (1.0).
HIGH_CONFIDENCE_SCORE: float = 0.7

# When the dominant hit clears HIGH_CONFIDENCE_SCORE, results scoring below
# this fraction of the top score are suppressed.  0.5 keeps a strong second
# match (e.g. 0.85 vs 0.70) while removing incidental low-score noise.
RELATIVE_SCORE_CUTOFF: float = 0.5

_TOKEN_SPLIT = re.compile(r"[-_/\s]+")

# SequenceMatcher ratio threshold at which two individual tokens are
# considered a "match".  0.8 lets near-identical tokens (e.g. "config" /
# "configs") match while still rejecting loosely similar but distinct words.
_TOKEN_MATCH_THRESHOLD: float = 0.8


def _tokenize(text: str) -> list[str]:
    """Lowercase and split *text* on ``[-_/\\s]+``, dropping empty segments."""
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def _f1_field_score(query_tokens: list[str], field_tokens: list[str]) -> float:
    """Return the F1 similarity between *query_tokens* and *field_tokens*.

    Precision = fraction of query tokens matched (≥ threshold) by any field
    token.  Recall = fraction of field tokens matched by any query token.
    F1 = harmonic mean of the two.

    Returns 0.0 when either token list is empty.
    """
    if not query_tokens or not field_tokens:
        return 0.0

    precision_hits = sum(
        1
        for qt in query_tokens
        if max(
            (difflib.SequenceMatcher(None, qt, pt).ratio() for pt in field_tokens),
            default=0.0,
        )
        >= _TOKEN_MATCH_THRESHOLD
    )
    recall_hits = sum(
        1
        for pt in field_tokens
        if max(
            (difflib.SequenceMatcher(None, qt, pt).ratio() for qt in query_tokens),
            default=0.0,
        )
        >= _TOKEN_MATCH_THRESHOLD
    )

    precision = precision_hits / len(query_tokens)
    recall = recall_hits / len(field_tokens)

    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _score_project(
    project: ProjectConfig, query_tokens: list[str], fields: str
) -> float:
    """Return the best per-field F1 score for *project* against *query_tokens*.

    Fields are scored independently and the maximum is returned, so a strong
    match against any single field is sufficient.
    """
    source_texts: list[str] = [project.id]
    if fields == "full":
        if project.description:
            source_texts.append(project.description)
        if project.path:
            source_texts.append(project.path)

    best = 0.0
    for text in source_texts:
        field_tokens = _tokenize(text)
        score = _f1_field_score(query_tokens, field_tokens)
        if score > best:
            best = score
    return best


def find_projects(
    projects: Sequence[ProjectConfig],
    query: str,
    *,
    fields: str = "full",
    min_score: float = DEFAULT_MIN_SCORE,
) -> FindResult:
    """Search *projects* for entries relevant to *query*.

    Parameters
    ----------
    projects:
        The list of `ProjectConfig` objects to search.
    query:
        Free-text query string.  Tokenised on ``[-_/\\s]+`` before scoring.
    fields:
        Which fields to score against.  ``"full"`` scores ``id``,
        ``description``, and ``path``; ``"id"`` scores only ``id``.
        Any other value raises `ValueError`.
    min_score:
        Relevance floor in [0.0, 1.0].  Projects whose best per-field F1
        score is strictly below this value are excluded from ``matches``.
        Defaults to `DEFAULT_MIN_SCORE` (0.3).

    Returns
    -------
    FindResult
        ``matches`` sorted descending by score; ``hint`` set when candidates
        existed but all fell below the floor.

    Raises
    ------
    ValueError
        If *fields* is not ``"full"`` or ``"id"``.
    """
    if fields not in ("full", "id"):
        raise ValueError(
            f"Unknown fields value {fields!r}. Supported values: 'full', 'id'."
        )

    if not projects:
        return FindResult(matches=[], hint=None)

    query_tokens = _tokenize(query)

    scored: list[ProjectMatch] = []
    for project in projects:
        score = _score_project(project, query_tokens, fields)
        scored.append(ProjectMatch(project=project, score=score))

    above_floor = [m for m in scored if m.score >= min_score]

    if not above_floor:
        return FindResult(matches=[], hint="no matches above relevance floor")

    above_floor.sort(key=lambda m: m.score, reverse=True)

    # Noise suppression: when the top score is a high-confidence hit,
    # drop results that score below half of the top score.  This prevents
    # incidental token-overlap hits from cluttering a result where one
    # project is clearly the best match.  When the top score does not
    # clear HIGH_CONFIDENCE_SCORE (ambiguous query), all results above
    # min_score are returned unchanged (current behaviour).
    top_score = above_floor[0].score
    if top_score >= HIGH_CONFIDENCE_SCORE:
        # Honor the caller's explicit floor: never drop anything min_score says
        # to keep.  When the caller has lowered min_score below the relative
        # cutoff (e.g. min_score=0.0 to inspect all candidates), suppression
        # would override their intent, so we skip it in that case.
        # The relative cutoff only applies when it is no stricter than the
        # caller's own floor — i.e. when min_score >= top_score * RELATIVE_SCORE_CUTOFF
        # we keep the caller's floor as-is (nothing extra to suppress); when
        # min_score < top_score * RELATIVE_SCORE_CUTOFF we tighten only if the
        # caller is using the default floor (has not opted into seeing more).
        relative_cutoff = top_score * RELATIVE_SCORE_CUTOFF
        if min_score >= relative_cutoff:
            # Caller's floor is already as tight as or tighter than what the
            # relative cutoff would produce — nothing extra to drop.
            pass
        elif min_score >= DEFAULT_MIN_SCORE:
            # Caller is using the default (or a higher) floor: apply suppression.
            above_floor = [m for m in above_floor if m.score >= relative_cutoff]
        # else: caller explicitly lowered the floor below the default, opting in
        # to see more results — do not suppress.

    return FindResult(matches=above_floor, hint=None)
