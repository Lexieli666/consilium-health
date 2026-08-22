"""The golden set and the multi-turn set: their schema, their loaders, and the labelling gate.

**An unlabelled draft cannot be scored, and that is enforced in code.**  ``load_golden`` refuses a
file whose items are not labelled unless the caller explicitly asks for drafts.  The reason is the
whole point of the checkpoint this file was written for: a golden set the system wrote and graded
itself against is worth nothing, and the only thing that makes the numbers mean anything is that a
person annotated the labels by hand.  A gate in a document is a request; a gate in the loader is a
constraint.

**Two of the four label fields were written by hand and two were not, and the record says which.**
``expected_route`` and ``red_flag`` are pure judgement and are the labels routing accuracy and
red-flag recall are computed against, so nothing ever proposed them: they were written by a person,
blind to the category and to the phrasing stratum.  ``relevant_doc_ids`` and ``reference_answer``
shipped as machine-written candidates, and the owner decided **not** to verify them item by item.

That decision forced the split below, and the split is the point of this module:

``proposed_fields`` is a **gate**.  A field named there holds a candidate nobody has dispositioned,
:meth:`GoldenItem.missing_labels` reports it as missing, and ``load_golden`` refuses the file.  It
is what stops flipping ``labeled: true`` from promoting a machine-written answer key into ground
truth by silence.  The multi-turn set is still unlabelled, so the mechanism stays.

``unverified_fields`` is **provenance**.  Same field names, same values, no gate.  It records that
a machine wrote the value and no person checked it, and it is carried into ``summary.json`` and
printed in the results table beside every number computed against it.  Clearing the marker instead
would have made the file assert a verification that did not happen; leaving the gate set would have
blocked the load forever.  The two sets are disjoint per record, because a field cannot be both
dispositioned and not.

What that costs is stated wherever the affected numbers appear rather than in a footnote:
**recall@5 and faithfulness are measured against a machine-constructed reference, while routing
accuracy and red-flag recall are not.**

**The phrasing stratum is a field, not a marker in the prose.**  Red-flag recall is reported split
by ``phrasing_stratum``, and ``draft_notes`` is the field the labeller edits while working.  A
dimension a metric splits on cannot live inside prose somebody is about to rewrite: trimming a note
would move an item between strata, change both per-stratum numbers, and fail nothing, because the
item would still be valid.  So the stratum is its own field, and ``draft_notes`` no longer says it.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from consilium.trace import RouteMode

#: The four label fields, in the order a labeller works through them (see docs/EVALUATION.md).
#: ``proposed_fields`` and ``unverified_fields`` are both validated against this tuple, so a typo
#: in a provenance marker cannot silently disable the gate it holds open, nor silently drop a
#: field out of the disclosure that says which numbers rest on a machine-written reference.
LABEL_FIELDS: tuple[str, ...] = (
    "expected_route",
    "relevant_doc_ids",
    "reference_answer",
    "red_flag",
)

#: The two label fields that are pure judgement.  Nothing ever proposed them and nothing may ever
#: mark them unverified: they are the labels routing accuracy and red-flag recall are computed
#: against, and the whole value of those two numbers is that a person wrote them by hand.
JUDGEMENT_FIELDS: tuple[str, ...] = ("expected_route", "red_flag")

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

#: The two phrasing strata a red-flag item can be drafted in.  Red-flag recall is reported per
#: stratum and never pooled: a pooled figure would move with the ratio between the strata, which is
#: a drafting choice rather than a property of the system.  See docs/EVALUATION.md section 1.3.
type PhrasingStratum = Literal["hard", "easy"]

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
    #: **The gate.**  Label fields holding a machine-written candidate that nobody has
    #: dispositioned.  A field named here is reported as missing by :meth:`missing_labels` even
    #: though it is populated, so ``load_golden`` refuses the file: a proposal cannot be promoted
    #: to a label by silence.  The multi-turn set is still unlabelled, so the mechanism stays.
    proposed_fields: tuple[str, ...] = ()
    #: **Provenance, not a gate.**  Label fields whose value a machine wrote and no person
    #: verified.  Same field names as ``proposed_fields`` and the same values; the difference is
    #: that this one does not block the load.  It exists because the owner decided not to verify
    #: ``relevant_doc_ids`` and ``reference_answer`` item by item: clearing the marker would have
    #: made the file assert a verification that did not happen, and leaving the gate set would
    #: have blocked loading forever.  The count is carried into ``summary.json`` and printed in
    #: the results table beside every number computed against these fields.
    unverified_fields: tuple[str, ...] = ()
    #: Which phrasing stratum a red-flag candidate was drafted in, or ``None`` where the item is
    #: not a red-flag candidate.  A first-class field rather than a marker inside ``draft_notes``,
    #: because red-flag recall is *split* on it: a dimension a metric splits on cannot depend on
    #: prose the labeller is invited to rewrite, or trimming a note would move an item between
    #: strata, change the per-stratum numbers, and fail no test.  It is authoring intent, fixed at
    #: drafting time and not the labeller's to decide, so it is never named in ``proposed_fields``.
    phrasing_stratum: PhrasingStratum | None = None
    #: What the item was written to test.  Authoring intent, never an answer key.
    draft_notes: str = ""

    @field_validator("proposed_fields", "unverified_fields")
    @classmethod
    def _known_label_fields(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(LABEL_FIELDS))
        if unknown:
            raise ValueError(f"{info.field_name} names fields that are not labels: {unknown}")
        judgement = sorted(set(value) & set(JUDGEMENT_FIELDS))
        if judgement:
            raise ValueError(
                f"{info.field_name} names a judgement field: {judgement}. "
                "expected_route and red_flag are written by a person by hand and by nothing else; "
                "a marker on either would mean the two numbers the labelling gate exists to "
                "protect rest on a machine-written value."
            )
        return value

    @model_validator(mode="after")
    def _provenance_markers_are_disjoint(self) -> GoldenItem:
        """A field cannot be both dispositioned and not.

        ``proposed_fields`` says "nobody has looked at this yet, refuse the file";
        ``unverified_fields`` says "a machine wrote this, a person decided not to check it, and
        every number computed against it says so".  A field in both would be making two
        incompatible claims, and whichever one a reader believed would be a coin toss.
        """
        both = sorted(set(self.proposed_fields) & set(self.unverified_fields))
        if both:
            raise ValueError(
                f"{both} are named in both proposed_fields and unverified_fields. "
                "A label field is either still waiting on the labeller or knowingly unverified; "
                "it cannot be both."
            )
        return self

    def missing_labels(self) -> tuple[str, ...]:
        """Which label fields still need the labeller.

        A field is missing when it is empty **or** when it is named in ``proposed_fields`` --  a
        candidate nobody has dispositioned is not a label, and the loader must refuse it for the
        same reason it refuses an empty one.  ``unverified_fields`` deliberately does **not**
        appear here: it records provenance for a value the owner has decided to accept as-is, and
        a provenance marker that blocked the load would be a gate nobody could ever clear without
        asserting a verification that did not happen.

        ``relevant_doc_ids`` is the one field where empty is a legitimate label: it means "no note
        in this corpus grounds this question", which `g-md-027` is in the set to record.  An empty
        list *beside a blank reference answer* is still an unlabelled item, so the two are checked
        together rather than dropping the check.
        """
        proposed = set(self.proposed_fields)
        ungrounded = not self.relevant_doc_ids and not self.reference_answer.strip()
        missing: list[str] = []
        if self.expected_route is None or "expected_route" in proposed:
            missing.append("expected_route")
        if ungrounded or "relevant_doc_ids" in proposed:
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


def unverified_label_counts(items: Sequence[GoldenItem]) -> dict[str, int]:
    """How many items hold a machine-written, unverified value in each label field.

    This is the number ``eval/run.py`` carries into ``summary.json`` and ``report.md`` prints in
    the results table.  It is reported per field rather than as one total because the fields are
    not interchangeable: ``relevant_doc_ids`` is what recall@5 is computed against and
    ``reference_answer`` is what the faithfulness judge reads, so which field is unverified
    determines which number is affected.

    Fields are returned in ``LABEL_FIELDS`` order, and a field nothing marks is absent rather than
    present with a zero -- an empty mapping means every label in the set was verified by hand.
    """
    counts = Counter(field for item in items for field in item.unverified_fields)
    return {field: counts[field] for field in LABEL_FIELDS if counts[field]}


def unverified_item_count(items: Sequence[GoldenItem]) -> int:
    """How many items hold at least one unverified label field."""
    return sum(1 for item in items if item.unverified_fields)


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
