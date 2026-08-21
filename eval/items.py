"""The golden set and the multi-turn set: their schema, their loaders, and the labelling gate.

**An unlabelled draft cannot be scored, and that is enforced in code.**  ``load_golden`` refuses a
file whose items are not labelled unless the caller explicitly asks for drafts.  The reason is the
whole point of the checkpoint this file was written for: a golden set the system wrote and graded
itself against is worth nothing, and the only thing that makes the numbers mean anything is that a
person annotated the labels by hand.  A gate in a document is a request; a gate in the loader is a
constraint.

The draft that ships alongside this module carries ``labeled: false`` and ``draft_notes`` -- one
line per item saying what the item was written to test.  That is authoring intent, not an answer
key.

**Two of the four label fields ship holding a machine-written candidate, and two ship empty.**
``relevant_doc_ids`` and ``reference_answer`` are mechanical: proposing them costs the labeller
nothing in independence, because reading the corpus notes is what filling them in consists of
either way.  ``expected_route`` and ``red_flag`` ship empty and stay empty until a person writes
them, because they are pure judgement and they are the two fields that drive routing accuracy and
red-flag recall -- a candidate there would be an anchor on exactly the numbers the checkpoint
exists to protect.

A candidate is not a label, so ``proposed_fields`` names every field that holds one and
:meth:`GoldenItem.missing_labels` reports those fields as still missing.  The labeller verifies the
candidate, corrects it, and removes the field's name from ``proposed_fields``; until then the
loader refuses the file exactly as it refuses an empty one.  Without that marker, flipping
``labeled: true`` after writing the two judgement fields would silently promote 150 machine-written
reference answers into ground truth, which is the failure this whole gate exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from consilium.trace import RouteMode

#: The four label fields, in the order a labeller works through them (see docs/EVALUATION.md).
#: ``proposed_fields`` is validated against this tuple, so a typo in a provenance marker cannot
#: silently disable the gate it exists to hold open.
LABEL_FIELDS: tuple[str, ...] = (
    "expected_route",
    "relevant_doc_ids",
    "reference_answer",
    "red_flag",
)

#: The five blocks of the golden set.  ``multi_dimensional`` is the block labelled
#: ``mode: parallel`` and it is not optional: with only single-specialty questions, a router that
#: always answers "single" scores 100% and the routing metric measures nothing.
GoldenCategory = Literal[
    "general_health",
    "symptom_urgency",
    "condition_coding",
    "guideline_evidence",
    "multi_dimensional",
]

#: How many items each block holds in a complete golden set.
ITEMS_PER_CATEGORY = 30

GOLDEN_CATEGORIES: tuple[GoldenCategory, ...] = (
    "general_health",
    "symptom_urgency",
    "condition_coding",
    "guideline_evidence",
    "multi_dimensional",
)

#: The two phrasing strata a red-flag item can be drafted in, and the marker each carries in
#: ``draft_notes``.  Red-flag recall is reported per stratum and never pooled: a pooled figure
#: would move with the ratio between the strata, which is a drafting choice rather than a property
#: of the system.  See docs/EVALUATION.md section 1.3.
PHRASING_STRATA: dict[str, str] = {
    "hard": "HARD-PHRASING STRATUM",
    "easy": "EASY-PHRASING STRATUM",
}

#: How many of the 30 multi-turn conversations must exceed the 5-exchange working-memory window.
#: Thirty two-turn conversations would test the window by never reaching it.
LONG_CONVERSATIONS_REQUIRED = 10
LONG_CONVERSATION_TURNS = 7


class EvalDataError(RuntimeError):
    """Raised when an evaluation data file cannot be read, or is a draft where labels are needed."""


class ExpectedRoute(BaseModel):
    """The routing label: which mode, and which agents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: RouteMode
    agents: tuple[str, ...] = Field(min_length=1)


class GoldenItem(BaseModel):
    """One labelled question.

    Every field below ``category`` is a label.  In a draft they are empty and ``labeled`` is false.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    question: str = Field(min_length=1)
    category: GoldenCategory

    expected_route: ExpectedRoute | None = None
    relevant_doc_ids: tuple[str, ...] = ()
    reference_answer: str = ""
    red_flag: bool | None = None

    #: Set to true by the person who labelled the item.  The loader trusts this flag *and* checks
    #: the fields, so a file cannot claim to be labelled while leaving a label empty.
    labeled: bool = False
    #: Label fields holding a machine-written **candidate** rather than a label.  A candidate is
    #: something for the labeller to verify or overwrite; the field is reported as missing until
    #: its name is removed from here, so a proposal cannot be promoted to a label by silence.
    proposed_fields: tuple[str, ...] = ()
    #: What the item was written to test.  Authoring intent, never an answer key.
    draft_notes: str = ""

    @field_validator("proposed_fields")
    @classmethod
    def _known_label_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(LABEL_FIELDS))
        if unknown:
            raise ValueError(f"proposed_fields names fields that are not labels: {unknown}")
        return value

    @property
    def phrasing_stratum(self) -> str | None:
        """Which phrasing stratum this item was drafted in, or ``None`` if it is not a red-flag
        candidate.

        The marker lives in ``draft_notes`` because it is authoring intent rather than a label --
        it describes how the question was written, which is fixed at drafting time and is not the
        labeller's to decide.  It is read here rather than re-derived by each consumer so that the
        metric and the draft lint cannot disagree about which stratum an item is in.
        """
        for name, marker in PHRASING_STRATA.items():
            if marker in self.draft_notes:
                return name
        return None

    def missing_labels(self) -> tuple[str, ...]:
        """Which label fields still need the labeller.

        A field is missing when it is empty **or** when it holds an unverified machine-written
        candidate.  The second half is what makes ``proposed_fields`` load-bearing: a candidate
        that nobody has looked at is not a label, and the loader must refuse it for the same
        reason it refuses an empty one.
        """
        proposed = set(self.proposed_fields)
        missing: list[str] = []
        if self.expected_route is None or "expected_route" in proposed:
            missing.append("expected_route")
        if not self.relevant_doc_ids or "relevant_doc_ids" in proposed:
            missing.append("relevant_doc_ids")
        if not self.reference_answer.strip() or "reference_answer" in proposed:
            missing.append("reference_answer")
        if self.red_flag is None or "red_flag" in proposed:
            missing.append("red_flag")
        return tuple(missing)


class MultiturnTurn(BaseModel):
    """One turn of a multi-turn conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1)
    #: Zero-based index of the earlier turn this one resolves against.  A label.
    depends_on_turn: int | None = None
    #: What the pronoun or ellipsis refers to, in the labeller's words.  A label.
    expected_referent: str = ""


class MultiturnConversation(BaseModel):
    """One conversation, where a later turn only resolves against an earlier one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    turns: tuple[MultiturnTurn, ...] = Field(min_length=2)
    labeled: bool = False
    draft_notes: str = ""

    @property
    def length(self) -> int:
        return len(self.turns)

    def missing_labels(self) -> tuple[str, ...]:
        """A conversation is labelled when at least one turn carries a resolved referent."""
        if any(turn.depends_on_turn is not None and turn.expected_referent for turn in self.turns):
            return ()
        return ("depends_on_turn", "expected_referent")


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, object]]]:
    """Yield ``(line_number, object)`` for each non-blank line."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalDataError(f"cannot read {path}: {exc}") from exc

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalDataError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise EvalDataError(f"{path}:{lineno}: expected a JSON object")
        yield lineno, parsed


def write_jsonl(path: Path, records: Sequence[BaseModel]) -> None:
    """Write one JSON object per line, with keys in model order for a readable diff."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        "".join(record.model_dump_json() + "\n" for record in records), encoding="utf-8"
    )


def load_golden(path: Path, *, allow_draft: bool = False) -> list[GoldenItem]:
    """Load the golden set, refusing an unlabelled draft unless asked.

    ``allow_draft`` exists for the tools that operate on a draft -- a linter, a labelling helper --
    and for the tests. ``eval/run.py`` never passes it.
    """
    items: list[GoldenItem] = []
    seen: set[str] = set()
    for lineno, record in read_jsonl(path):
        try:
            item = GoldenItem.model_validate(record)
        except ValidationError as exc:
            raise EvalDataError(f"{path}:{lineno}: invalid golden item: {exc}") from exc
        if item.id in seen:
            raise EvalDataError(f"{path}:{lineno}: duplicate item id {item.id!r}")
        seen.add(item.id)
        items.append(item)

    if not items:
        raise EvalDataError(f"{path}: no items")
    if not allow_draft:
        _require_labeled(path, items)
    return items


def load_multiturn(path: Path, *, allow_draft: bool = False) -> list[MultiturnConversation]:
    """Load the multi-turn set, refusing an unlabelled draft unless asked."""
    conversations: list[MultiturnConversation] = []
    seen: set[str] = set()
    for lineno, record in read_jsonl(path):
        try:
            conversation = MultiturnConversation.model_validate(record)
        except ValidationError as exc:
            raise EvalDataError(f"{path}:{lineno}: invalid conversation: {exc}") from exc
        if conversation.id in seen:
            raise EvalDataError(f"{path}:{lineno}: duplicate conversation id {conversation.id!r}")
        seen.add(conversation.id)
        conversations.append(conversation)

    if not conversations:
        raise EvalDataError(f"{path}: no conversations")
    if not allow_draft:
        _require_labeled(path, conversations)
    return conversations


def _require_labeled(
    path: Path, records: Sequence[GoldenItem] | Sequence[MultiturnConversation]
) -> None:
    """Refuse a draft.  The gate that makes "the owner labelled it" a property of the run."""
    unlabeled = [record for record in records if not record.labeled or record.missing_labels()]
    if not unlabeled:
        return

    first = unlabeled[0]
    raise EvalDataError(
        f"{path}: {len(unlabeled)} of {len(records)} records are unlabelled drafts "
        f"(first: {first.id!r}, missing {', '.join(first.missing_labels()) or 'labeled=true'}). "
        "This file is a draft. It has to be labelled by hand before anything is scored against it: "
        "a golden set the system wrote and graded itself against measures nothing. A field holding "
        "a machine-written candidate counts as missing until its name is removed from "
        "proposed_fields."
    )
