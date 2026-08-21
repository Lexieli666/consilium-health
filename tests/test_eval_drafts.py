"""The shipped drafts, and the drafting constraints that must survive labelling.

These are lint tests over `eval/data/`, in the same spirit as `tests/test_corpus.py`: the
conventions are asserted here rather than upheld by hand, so that an edit which breaks one fails
here instead of quietly becoming a worse measurement.

Nothing in this file scores anything. It reads the drafts and the red-flag table; it calls no model
and it writes no label.

Two conventions here are newer than the rest and are worth naming. The shipped draft carries
**machine-written candidates** in `relevant_doc_ids` and `reference_answer`, each declared in
`proposed_fields`, while `expected_route` and `red_flag` ship empty; the tests below hold that
split in place. And every red-flag candidate declares one of two **phrasing strata** in its own
`phrasing_stratum` field, because recall is reported per stratum and a stratum defined by negation
would absorb any item nobody classified. That field is deliberately not part of `draft_notes`: the
labeller rewrites those notes while working, and a dimension a metric splits on cannot depend on
prose that is about to change under it.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from consilium.memory import WINDOW_EXCHANGES
from consilium.safety import RedFlagTable
from eval.items import (
    GOLDEN_CATEGORIES,
    ITEMS_PER_CATEGORY,
    LABEL_FIELDS,
    LONG_CONVERSATION_TURNS,
    LONG_CONVERSATIONS_REQUIRED,
    EvalDataError,
    GoldenItem,
    load_golden,
    load_multiturn,
)

GOLDEN_PATH = Path("eval/data/golden.jsonl")
MULTITURN_PATH = Path("eval/data/multiturn.jsonl")
RED_FLAGS_PATH = Path("data/red_flags.yaml")

#: Marks an item written to be a red-flag presentation. The drafting constraint applies to these.
CANDIDATE_MARKER = "red-flag candidate"

#: Marks an item that reuses a red-flag pattern string **on purpose**, in a context where
#: escalating would be wrong. These probe the matcher's false-positive behaviour.
PROBE_MARKER = "FALSE-POSITIVE PROBE"

#: The strings that used to carry the stratum inside `draft_notes`, kept only so the lint can
#: assert they never come back. The stratum is a field now, and two sources for one dimension is
#: the defect the field was introduced to remove.
RETIRED_STRATUM_MARKERS = ("HARD-PHRASING STRATUM", "EASY-PHRASING STRATUM")

#: How many red-flag candidates are written in each phrasing stratum. These are exact, not floors:
#: per-stratum recall is reported over these denominators, so a change in either is a change in
#: what the two published numbers are computed over and belongs in the same commit as this line.
HARD_STRATUM_ITEMS = 22
EASY_STRATUM_ITEMS = 5

#: The corpus notes a proposal may point at. A proposed `doc_id` that names no file would be a
#: label nobody can retrieve, and it would fail as a retrieval miss rather than as a typo.
CORPUS_DIR = Path("data/corpus")


@pytest.fixture(scope="module")
def golden() -> list[GoldenItem]:
    return load_golden(GOLDEN_PATH, allow_draft=True)


@pytest.fixture(scope="module")
def patterns(red_flag_table: RedFlagTable) -> set[str]:
    return {pattern for rule in red_flag_table for pattern in rule.patterns}


def test_the_golden_set_is_150_items_in_five_blocks_of_thirty(golden: list[GoldenItem]) -> None:
    assert len(golden) == ITEMS_PER_CATEGORY * len(GOLDEN_CATEGORIES) == 150
    assert Counter(item.category for item in golden) == dict.fromkeys(
        GOLDEN_CATEGORIES, ITEMS_PER_CATEGORY
    )


def test_ids_are_unique_and_name_their_block(golden: list[GoldenItem]) -> None:
    assert len({item.id for item in golden}) == len(golden)
    prefixes = {
        "general_health": "g-gh-",
        "symptom_urgency": "g-su-",
        "condition_coding": "g-cc-",
        "guideline_evidence": "g-ge-",
        "multi_dimensional": "g-md-",
    }
    for item in golden:
        assert item.id.startswith(prefixes[item.category]), item.id


def test_the_shipped_golden_set_is_an_unlabelled_draft(golden: list[GoldenItem]) -> None:
    """Checkpoint B: the owner labels it, and no item in the shipped file is labelled."""
    assert all(item.labeled is False for item in golden)
    assert all(item.missing_labels() for item in golden)


def test_the_two_judgement_fields_ship_empty_and_are_never_proposed(
    golden: list[GoldenItem],
) -> None:
    """`expected_route` and `red_flag` drive routing accuracy and red-flag recall.

    They are the two fields where a machine-written candidate would be an anchor on the numbers
    the checkpoint exists to protect, so nothing proposes them -- not even as a suggestion in a
    different field.
    """
    assert all(item.expected_route is None for item in golden)
    assert all(item.red_flag is None for item in golden)
    assert all("expected_route" not in item.proposed_fields for item in golden)
    assert all("red_flag" not in item.proposed_fields for item in golden)


def test_every_candidate_field_is_declared_as_a_candidate(golden: list[GoldenItem]) -> None:
    """The mechanical fields ship holding proposals, and each says so.

    `relevant_doc_ids` and `reference_answer` are machine-written throughout, so a populated one
    that did not name itself in `proposed_fields` would read as a verified label.
    """
    for item in golden:
        populated = {
            name
            for name, value in (
                ("relevant_doc_ids", item.relevant_doc_ids),
                ("reference_answer", item.reference_answer.strip()),
            )
            if value
        }
        assert set(item.proposed_fields) == populated, item.id


def test_a_proposed_reference_answer_is_grounded_in_a_proposed_document(
    golden: list[GoldenItem],
) -> None:
    """The two mechanical fields are proposed together or not at all.

    A reference answer with no proposed source is a claim with nothing behind it, which is the
    one thing a reference answer must never be.
    """
    for item in golden:
        assert bool(item.relevant_doc_ids) == bool(item.reference_answer.strip()), item.id


def test_every_proposed_doc_id_names_a_corpus_note(golden: list[GoldenItem]) -> None:
    stems = {path.stem for path in CORPUS_DIR.glob("*.md")}
    unknown = {
        item.id: sorted(set(item.relevant_doc_ids) - stems)
        for item in golden
        if set(item.relevant_doc_ids) - stems
    }
    assert not unknown, unknown


def test_proposals_are_short_lists(golden: list[GoldenItem]) -> None:
    """Over-listing inflates the recall@5 denominator, so a proposal stays at three or fewer."""
    assert all(len(item.relevant_doc_ids) <= 3 for item in golden)
    assert all(len(set(item.relevant_doc_ids)) == len(item.relevant_doc_ids) for item in golden)


def test_loading_the_shipped_draft_without_allow_draft_is_refused() -> None:
    with pytest.raises(EvalDataError, match="labelled by hand"):
        load_golden(GOLDEN_PATH)
    with pytest.raises(EvalDataError, match="labelled by hand"):
        load_multiturn(MULTITURN_PATH)


def test_every_item_carries_authoring_intent(golden: list[GoldenItem]) -> None:
    """`draft_notes` says what the item was written to test, and what a proposal left uncertain."""
    assert all(item.draft_notes.strip() for item in golden)


def test_no_red_flag_candidate_reuses_a_pattern_string(
    golden: list[GoldenItem], patterns: set[str]
) -> None:
    """The frozen drafting constraint, which binds the hard-phrasing stratum.

    If a hard-stratum red-flag item echoed a string from `data/red_flags.yaml`, that stratum's
    recall would measure only whether the matcher matches itself. The easy stratum is exempt by
    construction: it exists to measure what canonical phrasing does, and the comparison between
    the two is the finding.
    """
    offenders = {
        item.id: sorted(p for p in patterns if p in item.question.lower())
        for item in golden
        if item.phrasing_stratum == "hard" and any(p in item.question.lower() for p in patterns)
    }
    assert not offenders, offenders


def test_the_only_items_reusing_a_pattern_string_are_marked_probes(
    golden: list[GoldenItem], patterns: set[str]
) -> None:
    """Outside the easy stratum, a pattern string appears only where escalating would be wrong."""
    reusing = [item for item in golden if any(p in item.question.lower() for p in patterns)]

    assert reusing, "the set needs at least one probe of the matcher's false-positive behaviour"
    for item in reusing:
        assert PROBE_MARKER in item.draft_notes or item.phrasing_stratum == "easy", item.id


def test_the_symptom_block_mixes_emergencies_with_routine_questions(
    golden: list[GoldenItem],
) -> None:
    """A block of only emergencies would measure recall with no false-positive denominator.

    The floor came down from eight to six when the easy-phrasing stratum was added: the five
    dropped items were routine ones, and the false-positive denominator that matters is the whole
    set's 123 non-red-flag items rather than this block's share of them.
    """
    block = [item for item in golden if item.category == "symptom_urgency"]
    candidates = [item for item in block if CANDIDATE_MARKER in item.draft_notes]

    assert 12 <= len(candidates) <= 25
    assert len(block) - len(candidates) >= 6
    assert len([item for item in golden if CANDIDATE_MARKER not in item.draft_notes]) >= 120


def test_the_red_flag_candidates_span_the_phrasing_styles_the_constraint_names(
    golden: list[GoldenItem],
) -> None:
    """Hedged, contracted, inflected, misspelled, described, and buried in a longer question."""
    notes = " ".join(
        item.draft_notes.lower() for item in golden if CANDIDATE_MARKER in item.draft_notes
    )
    for style in ("hedged", "contracted", "inflected", "misspelled", "described", "buried"):
        assert style in notes, style


def test_every_red_flag_candidate_declares_a_phrasing_stratum(golden: list[GoldenItem]) -> None:
    """Recall is reported per stratum, so an unclassified candidate would land in neither."""
    candidates = [item for item in golden if CANDIDATE_MARKER in item.draft_notes]

    unclassified = [item.id for item in candidates if item.phrasing_stratum is None]
    assert not unclassified, unclassified


def test_the_strata_hold_the_counts_the_published_numbers_are_computed_over(
    golden: list[GoldenItem],
) -> None:
    """22 and 5 are the denominators of the two recall figures, so they are asserted exactly.

    A floor would let an item drift between strata without failing anything, and the per-stratum
    numbers would move for a reason no diff records. Changing either count is a deliberate act and
    updates this test in the same commit.
    """
    counts = Counter(item.phrasing_stratum for item in golden if item.phrasing_stratum)

    assert counts == {"hard": HARD_STRATUM_ITEMS, "easy": EASY_STRATUM_ITEMS}


def test_only_red_flag_candidates_carry_a_stratum(golden: list[GoldenItem]) -> None:
    """`None` means "not a red-flag candidate", and nothing else may claim a stratum.

    A stratum on a routine item would add a denominator to a recall figure that is computed over
    red-flag items only.
    """
    stray = [
        item.id
        for item in golden
        if item.phrasing_stratum is not None and CANDIDATE_MARKER not in item.draft_notes
    ]
    assert not stray, stray


def test_the_stratum_is_declared_in_one_place_only(golden: list[GoldenItem]) -> None:
    """`draft_notes` is the labeller's working prose; the stratum is not in it.

    It used to be, and a trimmed note would then have moved an item between strata and changed
    both published recall figures with no test failing.
    """
    reintroduced = [
        item.id
        for item in golden
        if any(marker in item.draft_notes for marker in RETIRED_STRATUM_MARKERS)
    ]
    assert not reintroduced, reintroduced


def test_the_stratum_is_authoring_intent_and_not_a_label() -> None:
    """It is fixed at drafting time, so it is not the owner's to label, verify or clear.

    Keeping it out of `LABEL_FIELDS` is what keeps it out of `proposed_fields` and out of
    `missing_labels()`: a drafting decision is not a thing the labelling gate can be waiting on.
    """
    assert "phrasing_stratum" not in LABEL_FIELDS


def test_the_easy_stratum_is_phrased_the_way_the_rule_table_expects(
    golden: list[GoldenItem], patterns: set[str]
) -> None:
    """The point of the stratum is canonical phrasing, so it has to actually be canonical.

    This is not a measurement -- it asserts that the questions name their symptoms the ordinary
    way, which is what makes the per-stratum comparison a comparison of phrasing rather than of
    two arbitrary samples.
    """
    easy = [item for item in golden if item.phrasing_stratum == "easy"]

    assert all(any(p in item.question.lower() for p in patterns) for item in easy)


def test_the_coding_block_varies_along_the_convention_axis_not_the_condition_axis(
    golden: list[GoldenItem],
) -> None:
    """Thirty questions differing only in the condition would be near-duplicates of seven notes."""
    block = [item for item in golden if item.category == "condition_coding"]
    assert all(item.draft_notes.startswith("convention:") for item in block)

    conventions = {item.draft_notes.split("--")[0].strip() for item in block}
    assert len(conventions) >= 6, conventions
    for axis in (
        "'with' presumption",
        "combination codes",
        "second codes",
        "three-character",
        "root versus",
        "chapter boundaries",
    ):
        assert any(axis in item.draft_notes for item in block), axis


def test_the_multi_turn_set_is_thirty_conversations_with_ten_long_ones() -> None:
    """Ten must exceed the working-memory window, or the compaction path is never exercised."""
    conversations = load_multiturn(MULTITURN_PATH, allow_draft=True)

    assert len(conversations) == 30
    assert len({c.id for c in conversations}) == 30
    long_ones = [c for c in conversations if c.length >= LONG_CONVERSATION_TURNS]
    assert len(long_ones) >= LONG_CONVERSATIONS_REQUIRED
    assert all(c.length > WINDOW_EXCHANGES for c in long_ones)
    assert all(c.length >= 2 for c in conversations)


def test_every_conversation_is_an_unlabelled_draft_with_a_stated_shape() -> None:
    conversations = load_multiturn(MULTITURN_PATH, allow_draft=True)

    assert all(c.labeled is False for c in conversations)
    assert all(c.missing_labels() for c in conversations)
    assert all(turn.depends_on_turn is None for c in conversations for turn in c.turns)
    assert all(turn.expected_referent == "" for c in conversations for turn in c.turns)
    assert all(c.draft_notes.strip() for c in conversations)
