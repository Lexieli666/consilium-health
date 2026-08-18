"""The red-flag table, the matcher, and the narrow negation guard.

The guard is a safety-relevant policy decision, so its boundaries are pinned by tests rather than
left to the docstring: what it suppresses, what it deliberately does not suppress, and the fact
that both policies survive into the result so the evaluation can compare them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.safety import RedFlagError, RedFlagTable

TABLE_PATH = Path("data/red_flags.yaml")


@pytest.fixture(scope="module")
def table() -> RedFlagTable:
    return RedFlagTable.from_yaml(TABLE_PATH)


def test_table_loads_and_validates(table: RedFlagTable) -> None:
    assert len(table) >= 10
    assert all(rule.patterns for rule in table)
    assert {rule.urgency for rule in table} <= {"routine", "non-urgent", "urgent", "emergency"}


def test_every_rule_points_at_a_corpus_note_that_exists(table: RedFlagTable) -> None:
    """An escalation has to be able to cite a source, so the link is checked, not assumed."""
    corpus = {path.stem for path in Path("data/corpus").glob("*.md")}
    missing = sorted({rule.doc_id for rule in table} - corpus)
    assert not missing, f"red_flags.yaml references corpus notes that do not exist: {missing}"


def test_plain_symptom_matches_and_escalates(table: RedFlagTable) -> None:
    result = table.assess("I have crushing chest pain and my arm hurts")

    assert result.matched is True
    assert result.matched_raw is True
    assert result.negation_suppressed is False
    assert result.urgency == "emergency"
    assert "red-flag-chest-pain" in result.doc_ids
    assert result.action_text() is not None


@pytest.mark.parametrize(
    "text",
    [
        "I have no chest pain",
        "patient denies chest pain",
        "shortness of breath without chest pain",
        "I have never had chest pain",
        "no chest pain at all today",
    ],
)
def test_negation_directly_before_the_term_suppresses(table: RedFlagTable, text: str) -> None:
    result = table.assess(text)

    assert result.matched_raw is True, "the raw match must still be recorded"
    assert result.matched is False
    assert result.negation_suppressed is True
    assert result.urgency == "routine"
    assert result.action_text() is None


@pytest.mark.parametrize(
    "text",
    [
        "I don't have chest pain",
        "I dont have chest pain",
        "I don\u2019t have chest pain",  # typographic apostrophe, as phone keyboards emit
        "I haven't had chest pain",
        "it isn't chest pain",
        "she doesn't have chest pain",
        "he didn't have chest pain",
        "they weren't having chest pain",
        "I couldn't call it chest pain",
        "this wasn't chest pain",
        "I hasnt had chest pain",
    ],
)
def test_contracted_negation_suppresses(table: RedFlagTable, text: str) -> None:
    """Contracted negation is how people actually write, so it must reach the same cue.

    Handled by normalizing ``n't`` to ``not`` in the token stream rather than by listing every
    contracted form, so the existing cue and the existing three-token window both apply unchanged.
    Both apostrophe styles are covered: text from a phone keyboard carries U+2019, not U+0027.
    """
    result = table.assess(text)

    assert result.matched_raw is True, "the raw match must still be recorded"
    assert result.matched is False
    assert result.negation_suppressed is True
    assert result.urgency == "routine"


@pytest.mark.parametrize("word", ["cant", "wont"])
def test_apostrophe_free_words_that_are_also_english_are_not_cues(
    table: RedFlagTable, word: str
) -> None:
    """ "cant" and "wont" are real words, so they are excluded from the apostrophe-free list.

    Suppressing an emergency match on a legitimate word is a worse error than missing an informally
    typed negation, so the ambiguous forms are left out deliberately.
    """
    result = table.assess(f"the {word} chest pain came back")

    assert result.matched is True
    assert result.negation_suppressed is False


@pytest.mark.parametrize(
    "text",
    [
        "I am not sure whether what I am feeling counts as chest pain",
        "there is no doubt in my mind that this is chest pain",
        "no fever, but I do have chest pain",
    ],
)
def test_distant_or_unrelated_negation_does_not_suppress(table: RedFlagTable, text: str) -> None:
    """The guard is narrow on purpose: a cue outside the window is not about the symptom."""
    result = table.assess(text)

    assert result.matched is True
    assert result.negation_suppressed is False
    assert result.urgency == "emergency"


def test_negation_does_not_cross_a_sentence_boundary(table: RedFlagTable) -> None:
    """ "I have chest pain. No fever." must not lose its escalation to the next sentence's "No"."""
    result = table.assess("I have chest pain. No fever.")

    assert result.matched is True
    assert result.negation_suppressed is False


def test_one_negated_symptom_does_not_suppress_another(table: RedFlagTable) -> None:
    result = table.assess("no chest pain, but my face is drooping and my speech is slurred")

    assert result.matched is True, (
        "the stroke match survives even though the cardiac one was negated"
    )
    assert result.matched_raw is True
    assert result.negation_suppressed is False
    assert "red-flag-stroke-symptoms" in result.doc_ids
    assert any(match.suppressed for match in result.matches)


@pytest.mark.parametrize(
    "text",
    [
        # plural
        "I get chest pains when I walk",
        "I have been having palpitations and chest pains",
        "sudden severe headaches for two days",
        "black stool this morning",
        # past tense
        "I vomited blood last night",
        "I threw up blood",
        "her face drooped on one side",
        "his lips turned blue",
        "the throat closed up within minutes",
        # gerund / present participle
        "he is slurring speech",
        "I keep throwing up blood",
        "thinking about killing myself",
        "she is self harming",
        # third person / other inflection
        "her face droops on the left",
        "the pain spreads to my arm",
    ],
)
def test_audited_inflected_forms_match(table: RedFlagTable, text: str) -> None:
    """Inflected forms are enumerated in the YAML, so each one is a pattern a reviewer can read.

    This replaces an earlier optional-``s`` regex, which covered pain/pains and silently failed on
    every irregular verb.  All 116 original patterns were audited against their plural, past-tense,
    third-person and gerund forms; see docs/DESIGN.md for the counts.
    """
    result = table.assess(text)

    assert result.matched is True, f"no red flag matched: {text!r}"


def test_matcher_applies_no_morphology_of_its_own(table: RedFlagTable) -> None:
    """What matches must be exactly what the data file says, or the audit is unverifiable."""
    patterns = {pattern for rule in table for pattern in rule.patterns}

    assert "chest pains" in patterns, "the plural is an explicit pattern, not a regex trick"
    assert "vomited blood" in patterns
    # A plural that was NOT audited in must not match by accident.
    assert table.assess("mottled skins").matched is False


def test_hedging_is_not_negation(table: RedFlagTable) -> None:
    """Uncertainty about an emergency symptom is a reason to escalate, not to suppress."""
    result = table.assess("maybe chest pain, possibly just indigestion")

    assert result.matched is True


def test_guard_can_be_disabled_and_the_result_reports_both_policies(table: RedFlagTable) -> None:
    """The ablation needs the raw policy runnable as a switch, not as a code edit."""
    raw_table = RedFlagTable.from_yaml(TABLE_PATH, negation_guard=False)

    guarded = table.assess("I have no chest pain")
    raw = raw_table.assess("I have no chest pain")

    assert guarded.matched is False and guarded.matched_raw is True
    assert raw.matched is True and raw.matched_raw is True
    assert raw.negation_suppressed is False, "with the guard off, nothing is suppressed"
    # Same underlying matches either way; only the policy applied to them differs.
    assert [m.pattern for m in guarded.matches] == [m.pattern for m in raw.matches]
    assert all(m.suppressed for m in raw.matches), "the cue is still recorded on the match"


def test_longest_overlapping_phrase_wins(table: RedFlagTable) -> None:
    """Overlapping patterns must not double-count one span."""
    result = table.assess("I am having a heart attack")
    spans = [(m.start, m.end) for m in result.matches]

    assert len(spans) == len(set(spans))
    for i, (start_a, end_a) in enumerate(spans):
        for start_b, end_b in spans[i + 1 :]:
            assert not (start_a <= start_b and end_b <= end_a), "nested spans should be collapsed"


def test_clean_input_matches_nothing(table: RedFlagTable) -> None:
    result = table.assess("What foods are high in dietary fiber?")

    assert result.matched is False
    assert result.matched_raw is False
    assert result.negation_suppressed is False
    assert result.urgency == "routine"
    assert result.doc_ids == ()


def test_matching_is_case_insensitive_and_word_bounded(table: RedFlagTable) -> None:
    assert table.assess("CHEST PAIN since this morning").matched is True
    # "suicide" must not fire on a word that merely contains it as a substring.
    assert table.assess("the suicidal ideation screening question").matched is True
    assert table.assess("I read an article about pesticides").matched is False


def test_empty_table_is_refused() -> None:
    with pytest.raises(RedFlagError, match="empty"):
        RedFlagTable([])


def test_missing_file_is_reported_clearly() -> None:
    with pytest.raises(RedFlagError, match="cannot read"):
        RedFlagTable.from_yaml(Path("data/does_not_exist.yaml"))
