"""The evaluation data files, and the properties that had to survive labelling.

These are lint tests over `eval/data/`, in the same spirit as `tests/test_corpus.py`: the
conventions are asserted here rather than upheld by hand, so that an edit which breaks one fails
here instead of quietly becoming a worse measurement.

Nothing in this file scores anything. It reads the data files, the red-flag table, the corpus and
the agent policy; it calls no model and it writes no label.

**The golden set is labelled now, and this file is what the labelling did not get to change.**
`expected_route` and `red_flag` were written by hand on all 150 items, blind: the labeller could
not see the block, the phrasing stratum or the item id. `relevant_doc_ids` and `reference_answer`
are machine-written and were knowingly accepted **unverified**, which is recorded per record in
`unverified_fields` and republished in every run's `summary.json` and results table.

Three groups of assertions, and each exists for a different failure:

* **The provenance split.** `proposed_fields` gates the load; `unverified_fields` records
  provenance and does not. They are disjoint per record, the counts are exact because they are
  published, and neither may ever name a judgement field.
* **The drafting constraints.** The phrasing strata, the pattern-string rule, the false-positive
  probes and the convention axis of the coding block. These were properties of the draft and
  labelling was not allowed to move them, because published numbers are split on them.
* **Agreement between files.** A label points at corpus notes and implies a set of agent skills,
  and neither of those files is open in front of a labeller. `eval/validate.py` derives the checks
  from the files themselves; this is where they run.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import pytest

from consilium.memory import WINDOW_EXCHANGES
from consilium.retrieval.corpus import Document
from consilium.retrieval.types import Category
from consilium.safety import RedFlagTable
from consilium.safety.policy import Policy
from eval.items import (
    GOLDEN_CATEGORIES,
    ITEMS_PER_CATEGORY,
    JUDGEMENT_FIELDS,
    LABEL_FIELDS,
    LONG_CONVERSATION_TURNS,
    LONG_CONVERSATIONS_REQUIRED,
    EvalDataError,
    GoldenItem,
    load_golden,
    load_multiturn,
    unverified_item_count,
    unverified_label_counts,
)
from eval.validate import (
    route_document_mismatches,
    ungrounded_items,
    unknown_doc_ids,
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

#: What the rule table does with each stratum's phrasing, under both negation policies. Recorded
#: in docs/EVALUATION.md section 1.3 as the designed comparison: the 0 measures what paraphrase
#: does to a pattern table, and the 5 is what makes the 0 attributable to phrasing rather than to
#: a broken table. A question edit that moved either number would invalidate that published table
#: without failing anything else, which is why the numbers are asserted and not just described.
HARD_STRATUM_MATCHES = 0
EASY_STRATUM_MATCHES = 5

#: The labelled route and red-flag counts, published in docs/EVALUATION.md section 1.1. They are
#: the denominators of routing accuracy and red-flag recall; asserting them keeps the frozen
#: record and the file from drifting apart in either direction.
SINGLE_ROUTE_ITEMS = 121
PARALLEL_ROUTE_ITEMS = 29
RED_FLAG_ITEMS = 28

#: How many items hold a machine-written value the owner decided not to verify, and in which
#: fields. Published in every run's `summary.json` and printed in the results table, so it is
#: exact for the same reason the strata are.
#:
#: The two fields differ by four: the owner verified `relevant_doc_ids` on `g-gh-001`, `g-gh-017`,
#: `g-gh-026` and `g-gh-029` on 2026-08-22 while resolving the route-document warnings, and left
#: their reference answers unverified. The counts are per field rather than per item precisely so
#: that a partial verification like that is expressible.
UNVERIFIED_ITEMS = 148
UNVERIFIED_FIELD_COUNTS = {
    "relevant_doc_ids": 144,
    "reference_answer": 148,
}

#: The one item labelled with no relevant document. `g-md-027` describes deep vein thrombosis, and
#: neither `data/corpus/` nor `data/red_flags.yaml` covers it; the empty list is the label, not an
#: omission. It is named here because it is the item recall@5 is *not* computed over.
UNGROUNDED_ITEMS = ("g-md-027",)

#: The frozen record in docs/EVALUATION.md section 1.5. A digest ties a published number to the
#: exact file it was computed over, and a stale digest in a frozen record is worse than no digest --
#: so it is asserted in both directions: the file must hash to this, and this must appear in the
#: document. Changing a data file is a deliberate act that updates both in the same commit.
EVALUATION_DOC = Path("docs/EVALUATION.md")
FROZEN_DIGESTS = {
    GOLDEN_PATH: "d062b6072ab9fb41ea05f7ff8add32ed79b92e878e7763d1e57894218c7383e6",
    MULTITURN_PATH: "a675635212245b1b442bac09fdd1e78ac80f25a891816ede8e09c64e938de2c2",
}

#: The reviewed baseline for the route-versus-document **warning** (`eval/validate.py`). Four items
#: were flagged at the freeze; the owner opened all four documents on 2026-08-22 and resolved
#: three by correcting the route to match the documents (`g-gh-017`, `g-gh-026`, `g-gh-029`). What
#: remains is the baseline: an item a person has looked at and has not yet decided.
#:
#: This is a warning and not an error, so the baseline is asserted as an exact set rather than
#: required to be empty. Empty would be a demand to relabel; unchecked would make the warning
#: invisible. Exact means a *new* mismatch fails and a reviewed one does not.
#:
#: `g-gh-001` is the open one. Its route is `consultation` and its documents are
#: `condition-hypertension` (no dedicated owner) plus
#: `guideline-hypertension-diagnosis-and-bp-targets` (`find_guideline`, held by `research`). Both
#: labels have now been verified by a person, so this is two hand-written labels disagreeing rather
#: than a hand-written one disagreeing with a machine-written one -- which is why it is a decision
#: and not a fix. See docs/EVALUATION.md section 1.6.
REVIEWED_ROUTE_DOCUMENT_MISMATCHES = ("g-gh-001",)


@pytest.fixture(scope="module")
def golden() -> list[GoldenItem]:
    return load_golden(GOLDEN_PATH)


@pytest.fixture(scope="module")
def patterns(red_flag_table: RedFlagTable) -> set[str]:
    return {pattern for rule in red_flag_table for pattern in rule.patterns}


@pytest.fixture(scope="module")
def raw_table() -> RedFlagTable:
    """The same table with the negation guard off, so both policies are measured from one file."""
    return RedFlagTable.from_yaml(RED_FLAGS_PATH, negation_guard=False)


@pytest.fixture(scope="module")
def doc_categories(corpus_documents: list[Document]) -> dict[str, Category]:
    return {document.doc_id: document.category for document in corpus_documents}


# ------------------------------------------------------------------------------------------
# Shape
# ------------------------------------------------------------------------------------------


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


def test_every_item_carries_authoring_intent(golden: list[GoldenItem]) -> None:
    """`draft_notes` says what the item was written to test, and what the labeller decided."""
    assert all(item.draft_notes.strip() for item in golden)


def test_the_frozen_digests_match_the_files_and_the_document() -> None:
    """Checkpoint B's freeze record, checked in both directions.

    A digest written into a document is a claim about a file, and the failure mode of such a claim
    is that it goes stale without anything noticing -- which would leave a published number tied to
    a file that no longer exists. So the file is hashed here, and the value is also required to
    appear in `docs/EVALUATION.md`: editing a data file without updating the record fails, and
    editing the record without the file fails too.
    """
    document = EVALUATION_DOC.read_text(encoding="utf-8")
    for path, digest in FROZEN_DIGESTS.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, path
        assert digest in document, path


# ------------------------------------------------------------------------------------------
# The labelling gate, and the provenance that is not a gate
# ------------------------------------------------------------------------------------------


def test_the_golden_set_is_labelled_and_loads_without_allow_draft() -> None:
    """Checkpoint B, closed. The file no longer needs `allow_draft` and the gate is satisfied."""
    items = load_golden(GOLDEN_PATH)

    assert all(item.labeled for item in items)
    assert not [item.id for item in items if item.missing_labels()]


def test_the_multi_turn_set_is_still_a_draft_and_is_still_refused() -> None:
    """The gate is not retired -- the other file has not been labelled yet."""
    with pytest.raises(EvalDataError, match="labelled by hand"):
        load_multiturn(MULTITURN_PATH)


def test_the_two_judgement_fields_are_labelled_on_every_item(golden: list[GoldenItem]) -> None:
    """`expected_route` and `red_flag` drive routing accuracy and red-flag recall.

    Nothing ever proposed them, so a value here is a person's judgement by construction. They
    were written blind: the labeller saw the question and nothing else -- not the block, not the
    phrasing stratum, not the id.
    """
    assert all(item.expected_route is not None for item in golden)
    assert all(item.red_flag is not None for item in golden)


def test_no_judgement_field_carries_a_provenance_marker(golden: list[GoldenItem]) -> None:
    """A marker on `expected_route` or `red_flag` would say the two numbers the whole checkpoint
    exists to protect rest on a machine-written value.

    The schema refuses one outright (`tests/test_eval_items.py`); this asserts the file agrees.
    """
    for item in golden:
        assert not set(item.proposed_fields) & set(JUDGEMENT_FIELDS), item.id
        assert not set(item.unverified_fields) & set(JUDGEMENT_FIELDS), item.id


def test_the_two_provenance_markers_are_disjoint(golden: list[GoldenItem]) -> None:
    """An item cannot be both verified and not.

    `proposed_fields` means "nobody has dispositioned this, refuse the file"; `unverified_fields`
    means "a machine wrote it, a person decided not to check it, and every number computed against
    it says so". A field in both would make two incompatible claims about the same value.
    """
    for item in golden:
        assert not set(item.proposed_fields) & set(item.unverified_fields), item.id


def test_the_unverified_counts_are_the_ones_the_run_publishes(golden: list[GoldenItem]) -> None:
    """These go into `summary.json` and into the results table, so they are exact.

    A number in the table saying "148 of 150" has to be the number in the file, or the disclosure
    is itself unverified.
    """
    assert unverified_item_count(golden) == UNVERIFIED_ITEMS
    assert unverified_label_counts(golden) == UNVERIFIED_FIELD_COUNTS


def test_every_machine_written_field_declares_itself_unverified(golden: list[GoldenItem]) -> None:
    """A marker may only name a field that actually holds a value.

    Marking an empty field unverified would inflate the published count with items where there is
    nothing for a person to have failed to check.
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
        assert set(item.unverified_fields) <= populated, item.id


def test_an_unverified_reference_answer_names_the_documents_it_was_written_from(
    golden: list[GoldenItem],
) -> None:
    """A machine-written reference answer was written *only* from the notes labelled beside it.

    So an unverified `reference_answer` implies a non-empty `relevant_doc_ids`: a reference answer
    with no source is a claim with nothing behind it, which is the one thing a reference answer
    must never be.

    It does **not** imply that those documents are also unverified. The two fields are verified
    independently and four items now differ -- the owner checked the document lists on `g-gh-001`,
    `g-gh-017`, `g-gh-026` and `g-gh-029` without checking the answers written from them. Knowing
    the sources are right is not knowing the prose is faithful to them.
    """
    for item in golden:
        if "reference_answer" not in item.unverified_fields:
            continue
        assert item.relevant_doc_ids, item.id


def test_the_only_ungrounded_item_is_the_one_the_corpus_cannot_cover(
    golden: list[GoldenItem],
) -> None:
    """An empty `relevant_doc_ids` is a label, and it removes the item from recall@5's denominator.

    `g-md-027` is deep vein thrombosis: no corpus note and no rule in `data/red_flags.yaml` covers
    it, and none was added, because inventing coverage so an item clears inverts what the item
    measures. Which items are in this set is therefore part of what recall@5 was computed over.
    """
    assert ungrounded_items(golden) == UNGROUNDED_ITEMS


# ------------------------------------------------------------------------------------------
# Agreement with the corpus and with the agent policy
# ------------------------------------------------------------------------------------------


def test_every_labelled_doc_id_names_a_corpus_note(
    golden: list[GoldenItem], doc_categories: dict[str, Category]
) -> None:
    assert unknown_doc_ids(golden, doc_categories) == {}


def test_labelled_document_lists_stay_short(golden: list[GoldenItem]) -> None:
    """Over-listing inflates the recall@5 denominator, so a list stays at three or fewer."""
    assert all(len(item.relevant_doc_ids) <= 3 for item in golden)
    assert all(len(set(item.relevant_doc_ids)) == len(item.relevant_doc_ids) for item in golden)


def test_the_route_document_warning_matches_its_reviewed_baseline(
    golden: list[GoldenItem], policy: Policy, doc_categories: dict[str, Category]
) -> None:
    """A warning, pinned to what a person has already looked at.

    `data/policy.yaml` grants six of the seven skills to one agent each, and a labeller does not
    have that file open. What this detects is a route whose **dedicated, category-filtered** skill
    does not match the corpus category of the documents labelled beside it. It is not a claim that
    the route cannot answer the question: `search_knowledge` is unfiltered and every agent holds
    it, so every note stays reachable. What is lost is the filtered path to it.

    The grants are read from the policy and the expected skills from the labelled notes' corpus
    categories, so neither is a copy that can drift. The baseline is exact rather than empty,
    because a warning nobody can leave standing is an error wearing a different name.
    """
    mismatches = route_document_mismatches(golden, policy=policy, doc_categories=doc_categories)

    assert tuple(m.item_id for m in mismatches) == REVIEWED_ROUTE_DOCUMENT_MISMATCHES, [
        str(m) for m in mismatches
    ]


def test_a_red_flag_item_is_routed_somewhere_that_can_assess_risk(
    golden: list[GoldenItem], policy: Policy
) -> None:
    """`assess_risk` is the only skill that consults the rule table, and only `diagnostic` holds it.

    A red-flag item routed away from `diagnostic` would measure red-flag recall over a turn with
    no access to the table the label came from.
    """
    stranded = []
    for item in golden:
        if not item.red_flag or item.expected_route is None:
            continue
        reachable = {
            skill
            for agent in item.expected_route.agents
            for skill in policy.permitted_skills(agent)
        }
        if "assess_risk" not in reachable:
            stranded.append(item.id)
    assert not stranded, stranded


def test_the_labelled_route_and_red_flag_counts_are_the_published_ones(
    golden: list[GoldenItem],
) -> None:
    """The denominators of routing accuracy and red-flag recall, frozen in docs/EVALUATION.md."""
    modes = Counter(item.expected_route.mode for item in golden if item.expected_route)

    assert modes == {"single": SINGLE_ROUTE_ITEMS, "parallel": PARALLEL_ROUTE_ITEMS}
    assert sum(1 for item in golden if item.red_flag) == RED_FLAG_ITEMS


# ------------------------------------------------------------------------------------------
# The drafting constraints that labelling was not allowed to undo
# ------------------------------------------------------------------------------------------


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


def test_the_matcher_hits_per_stratum_are_the_ones_the_report_states(
    golden: list[GoldenItem], red_flag_table: RedFlagTable, raw_table: RedFlagTable
) -> None:
    """0 of 22 hard, 5 of 5 easy, under both negation policies.

    This is the diagnostic table in docs/EVALUATION.md section 1.3, asserted rather than described.
    Both numbers move only if a question changes, and a question changing is exactly what must not
    happen silently: the 0 is the finding the hard-stratum constraint was written to produce, and
    the 5 is what makes the 0 attributable to phrasing rather than to a broken table.
    """
    hits = {
        (stratum, policy): sum(
            1
            for item in golden
            if item.phrasing_stratum == stratum and table.assess(item.question).matched
        )
        for stratum in ("hard", "easy")
        for policy, table in (("guard", red_flag_table), ("raw", raw_table))
    }

    assert hits == {
        ("hard", "guard"): HARD_STRATUM_MATCHES,
        ("hard", "raw"): HARD_STRATUM_MATCHES,
        ("easy", "guard"): EASY_STRATUM_MATCHES,
        ("easy", "raw"): EASY_STRATUM_MATCHES,
    }


def test_the_symptom_block_mixes_emergencies_with_routine_questions(
    golden: list[GoldenItem],
) -> None:
    """A block of only emergencies would measure recall with no false-positive denominator.

    The floor came down from eight to six when the easy-phrasing stratum was added: the five
    dropped items were routine ones, and the false-positive denominator that matters is the whole
    set's 122 non-red-flag items rather than this block's share of them.
    """
    block = [item for item in golden if item.category == "symptom_urgency"]
    candidates = [item for item in block if item.red_flag]

    assert 12 <= len(candidates) <= 25
    assert len(block) - len(candidates) >= 6
    assert len([item for item in golden if not item.red_flag]) >= 120


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


def test_every_item_carrying_a_stratum_was_labelled_a_red_flag(golden: list[GoldenItem]) -> None:
    """The drafting decision and the hand label have to agree, or a stratum is scored over an
    item the labeller decided is not a red flag -- which would put a guaranteed false negative
    into a published per-stratum denominator.

    The reverse does not hold: `g-md-027` is labelled a red flag and carries no stratum on
    purpose, because the corpus and the rule table both lack any coverage of it and pooling it
    into the hard stratum would read its miss as a paraphrase failure.
    """
    disagreeing = [item.id for item in golden if item.phrasing_stratum and not item.red_flag]
    assert not disagreeing, disagreeing

    unstratified = [item.id for item in golden if item.red_flag and not item.phrasing_stratum]
    assert unstratified == list(UNGROUNDED_ITEMS)


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
    both published recall figures with no test failing. The labeller has since appended their own
    reasoning to every note, which is exactly the rewrite this guards against.
    """
    reintroduced = [
        item.id
        for item in golden
        if any(marker in item.draft_notes for marker in RETIRED_STRATUM_MARKERS)
    ]
    assert not reintroduced, reintroduced


def test_the_stratum_is_authoring_intent_and_not_a_label() -> None:
    """It is fixed at drafting time, so it is not the owner's to label, verify or clear.

    Keeping it out of `LABEL_FIELDS` is what keeps it out of both provenance markers and out of
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

    #: The labeller appended their own note after a `||` separator, so the authoring intent is
    #: everything before it. Splitting on `--` alone would fold the label text into the key and
    #: make every convention look distinct.
    conventions = {item.draft_notes.split("||")[0].split("--")[0].strip() for item in block}
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


# ------------------------------------------------------------------------------------------
# The multi-turn set, which is still a draft
# ------------------------------------------------------------------------------------------


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
