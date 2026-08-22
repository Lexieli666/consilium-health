"""Cross-file consistency checks over the labelled golden set.

``eval/items.py`` validates one record against its own schema.  This module checks a record
against the **other** files it points at -- the corpus it names ``doc_id`` values from, and the
agent policy its ``expected_route`` implies a set of skills from.  Those checks cannot live in the
schema because the schema does not get to read ``data/``, and they cannot live in a one-off script
because the thing they catch is a label that quietly stops agreeing with a file it never mentions.

Everything here is a pure function over already-loaded data.  ``tests/test_eval_drafts.py`` is what
runs it, so a label edit that breaks one of these fails in CI rather than in a sweep.

**The check that earned this module.** ``data/policy.yaml`` grants six of the seven skills to
exactly one agent each.  A labeller working from the labelling guide does not have that file open,
so nothing stops a route label from naming a set of agents that between them hold none of the
skills the question needs -- and the run would then be scored against a route the system is
structurally unable to answer the question through.  One such conflict was found by hand.  A check
found by hand once is a check that will be missed the next time, so it is derived here from the two
files themselves rather than restated as a list:

* which skills are exclusive, and to whom, is read from ``data/policy.yaml``;
* which skill a labelled document needs is read from that document's corpus ``category``;
* which skill a block needs is the one thing stated here, because it is what the block *is*.

**"Holds none", not "holds all."**  A question can need two exclusive skills and be answerable
through either; a route that reaches one of them is a defensible label.  A route that reaches none
of them is the failure, and it is the only thing this reports.  ``search_knowledge`` is granted to
every agent and is unfiltered, so no corpus note is ever strictly unreachable -- what a conflict
means is that the **category-filtered specialist skill** for the document is out of reach and the
document can only be found by an unfiltered search.  That is a retrieval-quality claim, not an
impossibility claim, and it is reported as one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from consilium.retrieval.types import Category
from consilium.safety.policy import Policy
from eval.items import GoldenCategory, GoldenItem

#: The skill that retrieves a corpus category, where exactly one skill is filtered to it.
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

#: The exclusive skill each block is defined to need, where the block is defined by one.
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

#: The skill a red-flag item needs, whatever block it sits in.  ``assess_risk`` is the only skill
#: that consults the rule table, and it is granted to ``diagnostic`` alone.
RED_FLAG_SKILL = "assess_risk"


@dataclass(frozen=True)
class SkillGrantConflict:
    """A labelled route whose agents hold none of the exclusive skills the item needs."""

    item_id: str
    category: GoldenCategory
    agents: tuple[str, ...]
    #: The exclusive skills the item needs, none of which the labelled agents hold.
    needed_skills: tuple[str, ...]
    #: The agents that would have to appear for one of those skills to be reachable.
    owning_agents: tuple[str, ...]
    #: Whether the item's ``relevant_doc_ids`` are themselves machine-written and unverified.  A
    #: conflict derived from an unverified document list is a disagreement between a hand-written
    #: label and a machine-written one, and reading it as a labelling error would be a guess about
    #: which of the two is wrong.
    from_unverified_docs: bool

    def __str__(self) -> str:
        return (
            f"{self.item_id} ({self.category}): route names {'+'.join(self.agents)}, "
            f"which holds none of {', '.join(self.needed_skills)} "
            f"(granted to {', '.join(self.owning_agents)})"
            + (" [document list is unverified]" if self.from_unverified_docs else "")
        )


def exclusive_skill_owners(policy: Policy) -> dict[str, str]:
    """``skill -> the one agent permitted to call it``, read from ``data/policy.yaml``.

    Derived rather than restated.  A copy of the grants here would be a second source that drifts
    from the file actually governing the agents, and the copy that lost would be the one deciding
    whether a labelled route is answerable.
    """
    holders: dict[str, list[str]] = {}
    for agent in policy:
        for skill in policy.permitted_skills(agent):
            holders.setdefault(skill, []).append(agent)
    return {skill: agents[0] for skill, agents in holders.items() if len(agents) == 1}


def granted_skills(policy: Policy, agents: Iterable[str]) -> frozenset[str]:
    """Every skill the labelled agent set can call between them."""
    return frozenset(skill for agent in agents for skill in policy.permitted_skills(agent))


def needed_exclusive_skills(
    item: GoldenItem,
    *,
    doc_categories: Mapping[str, Category],
    exclusive: Mapping[str, str],
) -> frozenset[str]:
    """The exclusive skills this item plausibly needs, from three sources unioned.

    The block it sits in, the categories of the documents labelled beside it, and whether it is a
    red-flag item.  Only skills that ``data/policy.yaml`` grants to exactly one agent survive: a
    skill every agent holds cannot make a route unanswerable, so it is not evidence of anything.
    """
    needed = {
        CATEGORY_SKILL[category]
        for doc_id in item.relevant_doc_ids
        if (category := doc_categories.get(doc_id)) in CATEGORY_SKILL
    }
    if block_skill := BLOCK_SKILL.get(item.category):
        needed.add(block_skill)
    if item.red_flag:
        needed.add(RED_FLAG_SKILL)
    return frozenset(needed & set(exclusive))


def skill_grant_conflicts(
    items: Sequence[GoldenItem],
    *,
    policy: Policy,
    doc_categories: Mapping[str, Category],
) -> list[SkillGrantConflict]:
    """Every item whose labelled agents hold **none** of the exclusive skills it needs.

    Returned in file order, so the report reads in the order the labeller worked.
    """
    exclusive = exclusive_skill_owners(policy)
    conflicts: list[SkillGrantConflict] = []
    for item in items:
        if item.expected_route is None:
            continue
        needed = needed_exclusive_skills(item, doc_categories=doc_categories, exclusive=exclusive)
        if not needed or needed & granted_skills(policy, item.expected_route.agents):
            continue
        conflicts.append(
            SkillGrantConflict(
                item_id=item.id,
                category=item.category,
                agents=tuple(item.expected_route.agents),
                needed_skills=tuple(sorted(needed)),
                owning_agents=tuple(sorted({exclusive[skill] for skill in needed})),
                from_unverified_docs="relevant_doc_ids" in item.unverified_fields,
            )
        )
    return conflicts


def unknown_doc_ids(
    items: Sequence[GoldenItem], known: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """``item_id -> the labelled doc_ids that name no corpus note``.

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
