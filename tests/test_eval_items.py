"""The golden set's schema and the gate that stops an unlabelled draft being scored.

The gate is the mechanism behind the whole checkpoint: an eval set the system wrote and graded
itself against is worth nothing, so the refusal lives in the loader rather than in a document.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eval.items import (
    GOLDEN_CATEGORIES,
    ITEMS_PER_CATEGORY,
    JUDGEMENT_FIELDS,
    LONG_CONVERSATION_TURNS,
    LONG_CONVERSATIONS_REQUIRED,
    EvalDataError,
    ExpectedRoute,
    GoldenItem,
    MultiturnConversation,
    MultiturnTurn,
    load_golden,
    load_multiturn,
    write_jsonl,
)


def _labeled(item_id: str = "g-001") -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question="what is hypertension",
        category="general_health",
        expected_route=ExpectedRoute(mode="single", agents=("consultation",)),
        relevant_doc_ids=("condition-hypertension",),
        reference_answer="Persistently raised blood pressure.",
        red_flag=False,
        labeled=True,
    )


def _draft(item_id: str = "g-001") -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question="what is hypertension",
        category="general_health",
        draft_notes="general health, condition explanation",
    )


def test_a_draft_reports_every_empty_label() -> None:
    assert set(_draft().missing_labels()) == {
        "expected_route",
        "relevant_doc_ids",
        "reference_answer",
        "red_flag",
    }
    assert _labeled().missing_labels() == ()


def test_a_field_holding_a_machine_written_candidate_still_counts_as_missing() -> None:
    """A candidate is something to verify, not a label.

    Without this, an owner who filled in the two judgement fields and flipped ``labeled: true``
    would silently promote every machine-written reference answer into ground truth.
    """
    item = _labeled().model_copy(update={"proposed_fields": ("reference_answer",)})

    assert item.missing_labels() == ("reference_answer",)


def test_clearing_the_marker_is_what_turns_a_candidate_into_a_label() -> None:
    item = _labeled().model_copy(update={"proposed_fields": ("relevant_doc_ids",)})

    assert item.model_copy(update={"proposed_fields": ()}).missing_labels() == ()


def test_a_file_still_holding_a_candidate_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    write_jsonl(path, [_labeled().model_copy(update={"proposed_fields": ("relevant_doc_ids",)})])

    with pytest.raises(EvalDataError, match="relevant_doc_ids"):
        load_golden(path)


def test_a_provenance_marker_naming_something_that_is_not_a_label_is_refused(
    tmp_path: Path,
) -> None:
    """A typo in the marker would disable the gate it exists to hold open."""
    path = tmp_path / "golden.jsonl"
    path.write_text(
        '{"id":"x","question":"q","category":"general_health","proposed_fields":["notes"]}\n',
        "utf-8",
    )

    with pytest.raises(EvalDataError, match="invalid golden item"):
        load_golden(path, allow_draft=True)


def test_an_unverified_field_records_provenance_and_does_not_gate_the_load() -> None:
    """The two markers are the whole point of `eval/items.py` and they do opposite things.

    `proposed_fields` says "nobody has dispositioned this, refuse the file". `unverified_fields`
    says "a machine wrote it, a person decided not to check it, and every number computed against
    it says so". Clearing the first marker instead of migrating to the second would have made the
    file assert a verification that did not happen; leaving it set would have blocked the load
    forever.
    """
    gated = _labeled().model_copy(update={"proposed_fields": ("reference_answer",)})
    recorded = _labeled().model_copy(update={"unverified_fields": ("reference_answer",)})

    assert gated.missing_labels() == ("reference_answer",)
    assert recorded.missing_labels() == ()
    assert recorded.reference_answer == gated.reference_answer


def test_a_field_cannot_be_both_dispositioned_and_not() -> None:
    """Two incompatible claims about one value, and whichever a reader believed would be a toss."""
    with pytest.raises(ValidationError, match="both proposed_fields and unverified_fields"):
        GoldenItem(
            id="x",
            question="q",
            category="general_health",
            proposed_fields=("reference_answer",),
            unverified_fields=("reference_answer",),
        )


def test_neither_marker_may_name_a_judgement_field() -> None:
    """`expected_route` and `red_flag` are hand-written or they are nothing.

    They are the labels routing accuracy and red-flag recall are computed against, so a marker
    saying either was machine-written must be impossible rather than merely absent.
    """
    for field in JUDGEMENT_FIELDS:
        with pytest.raises(ValidationError, match="judgement field"):
            GoldenItem(id="x", question="q", category="general_health", proposed_fields=(field,))
        with pytest.raises(ValidationError, match="judgement field"):
            GoldenItem(id="x", question="q", category="general_health", unverified_fields=(field,))


def test_an_empty_document_list_beside_a_reference_answer_is_a_label() -> None:
    """ "No note in this corpus grounds this question" is a finding, not a blank.

    Requiring a non-empty list would make inventing a source the only way to load the file, which
    is the reverse of what an item like `g-md-027` is in the set to measure. An empty list with no
    reference answer beside it is still an unlabelled item.
    """
    ungrounded = _labeled().model_copy(
        update={"relevant_doc_ids": (), "reference_answer": "The corpus cannot ground this."}
    )
    blank = _labeled().model_copy(update={"relevant_doc_ids": (), "reference_answer": ""})

    assert ungrounded.missing_labels() == ()
    assert set(blank.missing_labels()) == {"relevant_doc_ids", "reference_answer"}


def test_red_flag_false_is_a_label_and_not_an_absence() -> None:
    """`None` means unlabelled; `False` means a person decided this is not a red-flag item."""
    item = _labeled().model_copy(update={"red_flag": False})
    assert "red_flag" not in item.missing_labels()
    assert GoldenItem(id="x", question="q", category="general_health").red_flag is None


def test_loading_a_draft_is_refused_with_a_message_that_says_why(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    write_jsonl(path, [_draft()])

    with pytest.raises(EvalDataError, match="labelled by hand"):
        load_golden(path)


def test_a_file_claiming_to_be_labelled_while_missing_a_label_is_refused(tmp_path: Path) -> None:
    """The flag is trusted *and* checked, so `labeled: true` cannot be a lie."""
    path = tmp_path / "golden.jsonl"
    write_jsonl(path, [_labeled().model_copy(update={"reference_answer": "  "})])

    with pytest.raises(EvalDataError, match="reference_answer"):
        load_golden(path)


def test_a_draft_can_be_loaded_deliberately(tmp_path: Path) -> None:
    """Linting and labelling tools operate on a draft; `eval/run.py` never passes this flag."""
    path = tmp_path / "golden.jsonl"
    write_jsonl(path, [_draft()])

    items = load_golden(path, allow_draft=True)
    assert items[0].labeled is False


def test_a_labelled_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    write_jsonl(path, [_labeled("g-001"), _labeled("g-002")])

    items = load_golden(path)
    assert [item.id for item in items] == ["g-001", "g-002"]


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    write_jsonl(path, [_labeled("g-001"), _labeled("g-001")])

    with pytest.raises(EvalDataError, match="duplicate item id"):
        load_golden(path, allow_draft=True)


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text('{"id":"x","question":"q","category":"general_health","note":"?"}\n', "utf-8")

    with pytest.raises(EvalDataError, match="invalid golden item"):
        load_golden(path, allow_draft=True)


def test_an_unknown_category_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text('{"id":"x","question":"q","category":"coding"}\n', "utf-8")

    with pytest.raises(EvalDataError, match="invalid golden item"):
        load_golden(path, allow_draft=True)


def test_malformed_json_names_its_line(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text('{"id":"a","question":"q","category":"general_health"}\n{not json\n', "utf-8")

    with pytest.raises(EvalDataError, match=":2: invalid JSON"):
        load_golden(path, allow_draft=True)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text("\n\n", encoding="utf-8")

    with pytest.raises(EvalDataError, match="no items"):
        load_golden(path, allow_draft=True)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(EvalDataError, match="cannot read"):
        load_golden(tmp_path / "absent.jsonl", allow_draft=True)


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(
        '{"id":"a","question":"q","category":"general_health"}\n\n'
        '{"id":"b","question":"q","category":"general_health"}\n',
        encoding="utf-8",
    )
    assert len(load_golden(path, allow_draft=True)) == 2


def test_the_five_categories_and_the_target_count_are_stated_once() -> None:
    assert len(GOLDEN_CATEGORIES) == 5
    assert "multi_dimensional" in GOLDEN_CATEGORIES
    assert ITEMS_PER_CATEGORY * len(GOLDEN_CATEGORIES) == 150


# --- multi-turn ----------------------------------------------------------------------------------


def _conversation(turns: int = 2, *, labeled: bool = True) -> MultiturnConversation:
    items = [MultiturnTurn(question=f"turn {index}") for index in range(turns)]
    if labeled:
        items[-1] = MultiturnTurn(
            question="what about diet?", depends_on_turn=0, expected_referent="hypertension"
        )
    return MultiturnConversation(id="m-001", turns=tuple(items), labeled=labeled)


def test_a_conversation_is_labelled_when_a_turn_carries_a_resolved_referent() -> None:
    assert _conversation().missing_labels() == ()
    assert _conversation(labeled=False).missing_labels() == (
        "depends_on_turn",
        "expected_referent",
    )


def test_an_unlabelled_conversation_set_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "multiturn.jsonl"
    write_jsonl(path, [_conversation(labeled=False)])

    with pytest.raises(EvalDataError, match="labelled by hand"):
        load_multiturn(path)


def test_a_conversation_needs_at_least_two_turns(tmp_path: Path) -> None:
    path = tmp_path / "multiturn.jsonl"
    path.write_text('{"id":"m","turns":[{"question":"one"}]}\n', encoding="utf-8")

    with pytest.raises(EvalDataError, match="invalid conversation"):
        load_multiturn(path, allow_draft=True)


def test_the_window_exceeding_requirement_is_stated_once() -> None:
    """Thirty two-turn conversations would test the window by never reaching it."""
    from consilium.memory import WINDOW_EXCHANGES

    assert LONG_CONVERSATIONS_REQUIRED == 10
    assert LONG_CONVERSATION_TURNS > WINDOW_EXCHANGES
