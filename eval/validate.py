"""Cross-file consistency checks over the labelled golden set.

``eval/items.py`` validates one record against its own schema.  This module checks a record
against the **other** files it points at -- the corpus it names ``doc_id`` values from, and the
agent policy its ``expected_route`` implies a set of skills from.  Those checks cannot live in the
schema because the schema does not get to read ``data/``, and they cannot live in a one-off script
because the thing they catch is a label that quietly stops agreeing with a file it never mentions.

:func:`label_note_agent_mentions` is the one check here that reads nothing but the record itself.
It is here anyway, because it is the same *kind* of check -- a claim in one field that quietly stops
agreeing with another -- and because it needs the agent names, which are in ``data/policy.yaml``.

Everything here is a pure function over already-loaded data.  ``tests/test_eval_drafts.py`` is what
runs it, so a label edit that breaks one of these fails in CI rather than in a sweep.

Two severities, and the difference matters:

**Errors.**  :func:`unknown_doc_ids` is one.  A ``doc_id`` naming no corpus note is a label nobody
can retrieve, and there is no reading of the data that makes it acceptable.

**Warnings.**  :func:`route_document_mismatches` and :func:`label_note_agent_mentions` are both
warnings, pinned to reviewed baselines.  The second is a warning because ``diagnostic`` is an
English adjective as well as an agent name and no lexical rule separates the two; its docstring has
the argument.  The first is deliberately *not* an error either.
``data/policy.yaml`` grants six of the seven skills to exactly one agent each, but it grants
``search_knowledge`` -- the one retrieval skill with **no category filter** -- to all three.  So
every agent can reach every corpus note, and **no labelled route is structurally impossible**.  An
earlier version of this module claimed otherwise; the claim was wrong and the wording is corrected
here rather than left to be repeated.

What the check actually detects is a **mismatch between the dedicated skill the labelled route
carries and the corpus category of the labelled documents**.  The dedicated skills are
category-filtered -- ``recommend_lifestyle`` to ``lifestyle``, ``lookup_disease_code`` to
``coding``, ``find_guideline`` to ``guideline``, ``assess_risk`` to ``red_flag`` -- so when the
route holds none of them for the categories the item is labelled with, the item's documents are
reachable only through the unfiltered ``search_knowledge``, competing with the whole corpus for a
top-5 slot instead of with one category of it.  That is a claim about retrieval quality, and it can
also simply mean the route label and the document label disagree about what the question is.  Either
way it is something for a person to look at, not something a lint can decide, which is why it warns
against a reviewed baseline instead of failing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from consilium.retrieval.types import Category
from consilium.safety.policy import Policy
from eval.items import GoldenCategory, GoldenItem

#: The dedicated, category-filtered skill for a corpus category, where exactly one skill filters to
#: it.  ``search_knowledge`` is not in this table and never can be: it takes an optional category
#: argument and defaults to searching everything, which is what makes every note reachable from
#: every agent and this whole check a warning rather than an error.
#:
#: ``condition`` is deliberately absent.  Condition notes are reached both by ``analyze_symptoms``
#: (which filters to them) and by ``search_knowledge`` (which filters to nothing), and every
#: general-explanation item in the set labels one -- so a condition note names no owning agent and
#: a rule built on it would flag most of ``general_health`` for using the corpus as intended.
CATEGORY_SKILL: dict[Category, str] = {
    "lifestyle": "recommend_lifestyle",
    "coding": "lookup_disease_code",
    "guideline": "find_guideline",
    "red_flag": "assess_risk",
}

#: The dedicated skill each block is defined around, where the block is defined around one.
#:
#: ``general_health`` and ``multi_dimensional`` are absent on purpose: the first mixes explanation
#: (unfiltered ``search_knowledge``) with lifestyle, and the second is parallel by construction, so
#: neither names a single skill.  For those two blocks the labelled documents are the only signal,
#: which is why the two sources are unioned rather than one replacing the other.
BLOCK_SKILL: dict[GoldenCategory, str] = {
    "condition_coding": "lookup_disease_code",
    "guideline_evidence": "find_guideline",
    "symptom_urgency": "analyze_symptoms",
}

#: The dedicated skill a red-flag item implies, whatever block it sits in.  ``assess_risk`` is the
#: only skill that consults the rule table, and it is granted to ``diagnostic`` alone.
RED_FLAG_SKILL = "assess_risk"


@dataclass(frozen=True)
class RouteDocumentMismatch:
    """A **warning**: the labelled route carries no dedicated skill for the labelled documents.

    Not an error, and not a claim that the turn cannot answer the question.  ``search_knowledge``
    is unfiltered and every agent holds it, so the documents remain reachable -- through the search
    that competes with the whole corpus rather than through the one filtered to their category.
    """

    item_id: str
    category: GoldenCategory
    agents: tuple[str, ...]
    #: The dedicated skills the labelled documents and block imply, none of which the route holds.
    expected_skills: tuple[str, ...]
    #: The agents that hold those skills.
    owning_agents: tuple[str, ...]
    #: Whether the item's ``relevant_doc_ids`` are machine-written and unverified.  A mismatch
    #: derived from an unverified document list is a disagreement between a hand-written label and
    #: a machine-written one, and reading it as a labelling error would be a guess about which of
    #: the two is wrong.  Where the document list has been verified, both sides are a person's.
    from_unverified_docs: bool

    def __str__(self) -> str:
        return (
            f"{self.item_id} ({self.category}): route names {'+'.join(self.agents)}, which carries "
            f"no dedicated skill for the labelled documents "
            f"({', '.join(self.expected_skills)}, held by {', '.join(self.owning_agents)}); "
            "they are reachable only through the unfiltered search_knowledge"
            + (" [document list is unverified]" if self.from_unverified_docs else "")
        )


def exclusive_skill_owners(policy: Policy) -> dict[str, str]:
    """``skill -> the one agent permitted to call it``, read from ``data/policy.yaml``.

    Derived rather than restated.  A copy of the grants here would be a second source that drifts
    from the file actually governing the agents, and the copy that lost would be the one deciding
    whether a labelled route matches its documents.  ``search_knowledge`` is held by all three
    agents and therefore never appears in the result.
    """
    holders: dict[str, list[str]] = {}
    for agent in policy:
        for skill in policy.permitted_skills(agent):
            holders.setdefault(skill, []).append(agent)
    return {skill: agents[0] for skill, agents in holders.items() if len(agents) == 1}


def granted_skills(policy: Policy, agents: Iterable[str]) -> frozenset[str]:
    """Every skill the labelled agent set can call between them."""
    return frozenset(skill for agent in agents for skill in policy.permitted_skills(agent))


def expected_dedicated_skills(
    item: GoldenItem,
    *,
    doc_categories: Mapping[str, Category],
    exclusive: Mapping[str, str],
) -> frozenset[str]:
    """The dedicated skills this item's labels imply, from three sources unioned.

    The block it sits in, the categories of the documents labelled beside it, and whether it is a
    red-flag item.  Only skills that ``data/policy.yaml`` grants to exactly one agent survive: a
    skill every agent holds cannot make a route mismatch its documents, so it is not evidence of
    anything.
    """
    expected = {
        CATEGORY_SKILL[category]
        for doc_id in item.relevant_doc_ids
        if (category := doc_categories.get(doc_id)) in CATEGORY_SKILL
    }
    if block_skill := BLOCK_SKILL.get(item.category):
        expected.add(block_skill)
    if item.red_flag:
        expected.add(RED_FLAG_SKILL)
    return frozenset(expected & set(exclusive))


def route_document_mismatches(
    items: Sequence[GoldenItem],
    *,
    policy: Policy,
    doc_categories: Mapping[str, Category],
) -> list[RouteDocumentMismatch]:
    """Warn where a labelled route carries **none** of the dedicated skills its labels imply.

    "None", not "not all".  A question can imply two dedicated skills and be well served by either,
    so a route reaching one of them is a defensible label; a route reaching none of them is the one
    worth a person's attention.  Returned in file order, so the report reads in the order the
    labeller worked.
    """
    exclusive = exclusive_skill_owners(policy)
    mismatches: list[RouteDocumentMismatch] = []
    for item in items:
        if item.expected_route is None:
            continue
        expected = expected_dedicated_skills(
            item, doc_categories=doc_categories, exclusive=exclusive
        )
        if not expected or expected & granted_skills(policy, item.expected_route.agents):
            continue
        mismatches.append(
            RouteDocumentMismatch(
                item_id=item.id,
                category=item.category,
                agents=tuple(item.expected_route.agents),
                expected_skills=tuple(sorted(expected)),
                owning_agents=tuple(sorted({exclusive[skill] for skill in expected})),
                from_unverified_docs="relevant_doc_ids" in item.unverified_fields,
            )
        )
    return mismatches


def unknown_doc_ids(
    items: Sequence[GoldenItem], known: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """**Error.** ``item_id -> the labelled doc_ids that name no corpus note``.

    A ``doc_id`` naming no file is a label nobody can retrieve, and it would land in the results as
    a retrieval miss rather than as the typo it is.  ``doc_id`` is the filename stem by contract
    (CLAUDE.md section 7), which is what makes this checkable at all.
    """
    stems = set(known)
    return {
        item.id: tuple(sorted(set(item.relevant_doc_ids) - stems))
        for item in items
        if set(item.relevant_doc_ids) - stems
    }


def ungrounded_items(items: Sequence[GoldenItem]) -> tuple[str, ...]:
    """Items labelled with no relevant document at all, in file order.

    An empty ``relevant_doc_ids`` is a legitimate label -- it says no note in this corpus grounds
    the question -- but it also removes the item from the recall@5 denominator, so which items are
    in this set is part of what recall@5 was computed over and is asserted rather than counted.
    """
    return tuple(item.id for item in items if not item.relevant_doc_ids)


#: The separator between the two halves of ``draft_notes``.  Left of it is the authoring intent,
#: written when the question was drafted; right of it is the labeller's reasoning, appended when
#: the label was decided.
LABEL_NOTE_SEPARATOR = "|| LABEL:"


def label_note(item: GoldenItem) -> str:
    """The labeller's half of ``draft_notes``, or ``""`` if the record has no LABEL section."""
    _, separator, note = item.draft_notes.partition(LABEL_NOTE_SEPARATOR)
    return note if separator else ""


@dataclass(frozen=True)
class LabelNoteAgentMention:
    """A **warning**: a LABEL note names an agent the item is not routed to.

    Usually a stale note -- the labeller changed the route after reading the documents and the
    reasoning stopped at the intermediate state, leaving the record explaining a decision it no
    longer records.  Nothing downstream reads ``draft_notes``, so this costs no measured number; it
    costs the reviewer, who is reading the note precisely to find out why the label is what it is.
    """

    item_id: str
    #: The agent name the note uses, lowercased.
    agent: str
    #: The agents the item is actually routed to.
    route_agents: tuple[str, ...]
    #: The words around the mention, for the log line.  Not part of the reviewed baseline: a note
    #: may be reworded without re-review, and pinning prose here would demand one.
    excerpt: str

    def __str__(self) -> str:
        return (
            f"{self.item_id}: LABEL note names {self.agent!r}, which is not in the labelled route "
            f"({'+'.join(self.route_agents) or 'none'}) -- ...{self.excerpt}..."
        )


def label_note_agent_mentions(
    items: Sequence[GoldenItem], *, policy: Policy
) -> list[LabelNoteAgentMention]:
    """Warn where a LABEL note names an agent that is not in the item's ``expected_route``.

    **Only the LABEL half of ``draft_notes`` is read, and that is the whole design.**  The half to
    the left of the separator is the *drafting plan* -- "urgency plus a guideline target: diagnostic
    and research" -- written before any label existed.  The blind pass was allowed to disagree with
    that plan and did so five times, and those disagreements are kept on purpose (CLAUDE.md §1).
    Scanning the whole field would therefore fire on six ``multi_dimensional`` items for having
    exactly the property the labelling procedure exists to produce.  The labeller's half is
    different: it was written *about* the label, so it disagreeing with the label is a defect.

    **A warning against a reviewed baseline, not an error, because the match cannot be made
    exact.**  ``diagnostic`` is an ordinary English adjective as well as an agent name, and
    ``g-ge-001`` uses it as one ("the diagnostic blood-pressure threshold").  No lexical rule
    separates that from a genuine agent reference: "the diagnostic threshold" and "the consultation
    agent" have the same shape, and a rule tuned to tell them apart would be fitted to the two
    examples in this file and would silence the next stale note that happened to match it.  So the
    ambiguity is left visible and dispositioned by a person once, in the baseline.

    Agent names are read from ``data/policy.yaml`` rather than restated, for the same reason
    :func:`exclusive_skill_owners` reads the grants from it: a second copy of the names is a copy
    that can drift, and the one that drifted would be the one deciding whether a note is stale.
    Returned in file order, one entry per (item, agent) pair.
    """
    mentions: list[LabelNoteAgentMention] = []
    for item in items:
        note = label_note(item)
        if not note:
            continue
        routed = frozenset(item.expected_route.agents if item.expected_route else ())
        for agent in policy:
            if agent in routed:
                continue
            match = re.search(rf"\b{re.escape(agent)}\b", note, re.IGNORECASE)
            if match is None:
                continue
            start, end = max(0, match.start() - 40), min(len(note), match.end() + 40)
            mentions.append(
                LabelNoteAgentMention(
                    item_id=item.id,
                    agent=agent,
                    route_agents=tuple(item.expected_route.agents) if item.expected_route else (),
                    excerpt=" ".join(note[start:end].split()),
                )
            )
    return mentions
