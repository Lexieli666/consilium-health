"""The tokenizer decides whether lexical retrieval can win on the coding category, which is the
central claim of the hybrid-retrieval argument.  These assertions are the claim's precondition.
"""

from __future__ import annotations

import pytest

from consilium.retrieval import tokenize
from consilium.retrieval.tokenize import STOPWORDS


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The ICD-10 code is E11.9", "e11.9"),
        ("Start an SGLT-2 inhibitor", "sglt-2"),
        ("Code I10 applies here", "i10"),
        ("COVID-19 vaccination status", "covid-19"),
        ("Vitamin B12 deficiency", "b12"),
    ],
)
def test_code_like_tokens_survive_intact(text: str, expected: str) -> None:
    assert expected in tokenize(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("end-stage renal disease", ["end", "stage", "renal", "disease"]),
        ("first-line therapy", ["first", "line", "therapy"]),
    ],
)
def test_hyphenated_words_without_digits_are_split(text: str, expected: list[str]) -> None:
    """A query for "stage" should match "end-stage"; keeping the pair fused would prevent that."""
    assert tokenize(text) == expected


def test_tokens_are_lowercased_and_punctuation_is_dropped() -> None:
    assert tokenize("Hypertension, sometimes called HBP.") == [
        "hypertension",
        "sometimes",
        "called",
        "hbp",
    ]


def test_stopwords_are_removed() -> None:
    assert tokenize("the patient is on a statin") == ["patient", "statin"]


def test_negation_words_are_kept_deliberately() -> None:
    """ "no chest pain" and "chest pain" must not tokenize identically."""
    assert "no" not in STOPWORDS
    assert "not" not in STOPWORDS
    assert tokenize("no chest pain") == ["no", "chest", "pain"]
    assert tokenize("chest pain") == ["chest", "pain"]


def test_single_letters_are_dropped_but_short_codes_are_not() -> None:
    assert tokenize("a b c") == []
    assert tokenize("type 2 diabetes") == ["type", "2", "diabetes"]


def test_stopword_removal_can_be_disabled() -> None:
    assert tokenize("the statin", remove_stopwords=False) == ["the", "statin"]


def test_empty_text_yields_no_tokens() -> None:
    assert tokenize("") == []
    assert tokenize("   ---   ") == []
