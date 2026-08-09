"""Substrate: the tokenizer shared by BM25 and the offline hash embedder.

This is a specification decision, not a default.  The central claim of the hybrid-retrieval
argument in docs/DESIGN.md is that lexical retrieval carries the coding and guideline categories,
and that claim lives or dies on whether ``E11.9``, ``I10`` and ``SGLT-2`` survive tokenization as
single tokens.  A tokenizer that splits ``E11.9`` into ``e``, ``11``, ``9`` cannot retrieve an
ICD-10 code, and the hybrid result would then be measuring a bug rather than an effect.

Rules, in order:

1. Lowercase.
2. Split on runs of non-alphanumeric characters, except that ``.`` and ``-`` inside a token are
   kept for now.
3. A token that kept a ``.`` or ``-`` is preserved whole **iff it contains a digit**; otherwise it
   is split at those separators.  So ``e11.9``, ``sglt-2`` and ``covid-19`` survive, while
   ``end-stage`` becomes ``end`` and ``stage`` -- which is what a lexical index wants, because a
   query for "stage" should match "end-stage".
4. Drop English stopwords, minus a negation-bearing set kept on purpose (see ``_KEPT``).
5. Drop single-character tokens unless they contain a digit.

No stemming.  Stemming would collapse ``hypertensive``/``hypertension`` (helpful) but also
``coding``/``code`` across categories (unhelpful), and an unstemmed BM25 keeps the failure modes
legible when a retrieval result has to be explained.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")
_DIGIT_RE = re.compile(r"\d")
_SEPARATOR_RE = re.compile(r"[.\-]")

#: A small, explicit English stopword list.  Written out rather than pulled from NLTK so that the
#: offline rule holds with no corpus download and so that the list is reviewable in one screen.
#: Kept as a split string rather than a list literal (SIM905) because the literal form is 160
#: single-element lines that no reviewer will read.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again all am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has
    have having he her here hers herself him himself his how i if in into is it its itself just me
    more most my myself of off on once only or other our ours ourselves out over own same she
    should so some such than that the their theirs them themselves then there these they this those
    through to too under until up very was we were what when where which while who whom why will
    with you your yours yourself yourselves
    """.split()  # noqa: SIM905
)

#: Negation and comparison words that a general stopword list removes and a clinical corpus needs.
#: "no chest pain" and "chest pain" must not tokenize identically; BM25 does not model negation,
#: but discarding the token guarantees the distinction is unrecoverable.
_KEPT: frozenset[str] = frozenset({"no", "not", "nor", "against", "without"})

STOPWORDS: frozenset[str] = _STOPWORDS - _KEPT


def tokenize(text: str, *, remove_stopwords: bool = True, min_length: int = 2) -> list[str]:
    """Tokenize ``text`` per the rules in this module's docstring."""
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        for token in _split_if_wordlike(raw):
            if remove_stopwords and token in STOPWORDS:
                continue
            if len(token) < min_length and not _DIGIT_RE.search(token):
                continue
            tokens.append(token)
    return tokens


def _split_if_wordlike(token: str) -> list[str]:
    """Keep ``.``/``-``-joined tokens whole when they contain a digit; otherwise split them."""
    if not _SEPARATOR_RE.search(token):
        return [token]
    if _DIGIT_RE.search(token):
        return [token.strip(".-")]
    return [part for part in _SEPARATOR_RE.split(token) if part]
