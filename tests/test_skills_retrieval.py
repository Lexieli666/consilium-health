"""The five retrieval-backed skills, run against the real corpus on the offline seams.

`HashEmbedder` scores weighted token overlap rather than meaning, so nothing here is a retrieval
*quality* measurement -- those come from the eval harness with `BgeEmbedder`.  What these tests
establish is the contract: the category filter narrows, the returned depth is fixed, `sources`
holds the `doc_id` values the answer can cite, and one `retrieval` trace event is emitted per
retrieval with the full fused top-10 that MRR@10 needs.
"""

from __future__ import annotations

import pytest

from consilium.retrieval import RETURNED_K, TRACE_DEPTH
from consilium.skills import SkillContext, SkillRegistry
from consilium.skills.coding import extract_codes
from consilium.skills.research import FALLBACK_ASPECTS, MAX_SUB_QUERIES, plan_sub_queries
from consilium.trace import MemorySink, RetrievalEvent


def test_search_knowledge_returns_passages_and_sources(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    result = registry.run("search_knowledge", {"query": "what is hypertension"}, skill_context)

    assert result.ok is True
    assert len(result.data["passages"]) == RETURNED_K
    assert result.sources == tuple(p["doc_id"] for p in result.data["passages"])
    assert "condition-hypertension" in result.sources

    (event,) = memory_sink.of_type(RetrievalEvent)
    assert event.skill == "search_knowledge"
    assert event.category_filter is None
    assert len(event.fused_topk) == TRACE_DEPTH
    assert event.returned_k == RETURNED_K


def test_search_knowledge_can_narrow_to_a_category(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    result = registry.run(
        "search_knowledge", {"query": "blood pressure", "category": "lifestyle"}, skill_context
    )

    assert {passage["category"] for passage in result.data["passages"]} == {"lifestyle"}
    assert memory_sink.of_type(RetrievalEvent)[0].category_filter == "lifestyle"


def test_search_knowledge_rejects_an_unknown_category(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run("search_knowledge", {"query": "x", "category": "invented"}, skill_context)
    assert result.ok is False
    assert result.error is not None and "category" in result.error


@pytest.mark.parametrize(
    ("name", "args", "expected_category"),
    [
        ("recommend_lifestyle", {"condition": "hypertension", "domain": "diet"}, "lifestyle"),
        ("lookup_disease_code", {"condition": "type 2 diabetes"}, "coding"),
        ("find_guideline", {"topic": "hypertension", "aspect": "targets"}, "guideline"),
    ],
)
def test_category_scoped_skills_stay_inside_their_category(
    registry: SkillRegistry,
    skill_context: SkillContext,
    memory_sink: MemorySink,
    name: str,
    args: dict[str, str],
    expected_category: str,
) -> None:
    """The claim hybrid retrieval rests on: a coding lookup never sees lifestyle prose."""
    result = registry.run(name, args, skill_context)

    assert result.ok is True
    assert {passage["category"] for passage in result.data["passages"]} == {expected_category}
    assert memory_sink.of_type(RetrievalEvent)[0].category_filter == expected_category


def test_recommend_lifestyle_finds_the_domain_note(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "recommend_lifestyle", {"condition": "hypertension", "domain": "diet"}, skill_context
    )
    assert "lifestyle-hypertension-diet" in result.sources


def test_recommend_lifestyle_rejects_a_domain_outside_the_four(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "recommend_lifestyle", {"condition": "asthma", "domain": "supplements"}, skill_context
    )
    assert result.ok is False


def test_lookup_disease_code_extracts_the_code_not_just_the_prose(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run("lookup_disease_code", {"condition": "type 2 diabetes"}, skill_context)

    codes = {mention["code"] for mention in result.data["codes"]}
    assert "E11" in codes
    assert "coding-type-2-diabetes-e11" in result.sources
    for mention in result.data["codes"]:
        assert mention["context"]
        assert mention["doc_id"] in result.sources


def test_lookup_disease_code_accepts_a_code_as_the_query(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """`E11.9` survives the tokenizer intact, which is what makes the lexical half earn it."""
    result = registry.run("lookup_disease_code", {"condition": "E11.9"}, skill_context)

    assert "coding-type-2-diabetes-e11" in result.sources
    assert "E11.9" in {mention["code"] for mention in result.data["codes"]}


def test_code_extraction_rejects_things_that_only_look_like_codes() -> None:
    from consilium.skills.base import Passage

    passage = Passage(
        doc_id="d",
        chunk_index=0,
        title="t",
        category="coding",
        source="s",
        text="An A1c of 7% and COVID19 and U07 are not reportable here, but I10 and E11.9 are.",
        score=1.0,
    )
    assert [mention.code for mention in extract_codes([passage])] == ["I10", "E11.9"]


def test_code_extraction_keeps_one_mention_per_code_and_document() -> None:
    from consilium.skills.base import Passage

    def _passage(doc_id: str, text: str) -> Passage:
        return Passage(
            doc_id=doc_id,
            chunk_index=0,
            title=doc_id,
            category="coding",
            source="s",
            text=text,
            score=1.0,
        )

    mentions = extract_codes(
        [_passage("a", "I10 appears twice: I10."), _passage("b", "I10 again, elsewhere.")]
    )
    assert [(m.code, m.doc_id) for m in mentions] == [("I10", "a"), ("I10", "b")]


def test_find_guideline_flags_notes_that_record_a_disagreement(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "find_guideline",
        {"topic": "hypertension", "aspect": "blood pressure targets"},
        skill_context,
    )

    flagged = {
        hit["doc_id"] for hit in result.data["guidelines"] if hit["has_disagreement_section"]
    }
    assert "guideline-hypertension-diagnosis-and-bp-targets" in flagged
    assert all(hit["source"] for hit in result.data["guidelines"])


def test_find_guideline_aspect_is_optional(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run("find_guideline", {"topic": "insomnia"}, skill_context)
    assert result.ok is True
    assert result.data["aspect"] == ""


def test_deep_research_retrieves_once_per_sub_query(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    result = registry.run(
        "deep_research",
        {
            "question": "what blood pressure target should be used",
            "sub_queries": ["hypertension targets", "hypertension first-line treatment"],
        },
        skill_context,
    )

    assert result.data["sub_queries"] == [
        "what blood pressure target should be used",
        "hypertension targets",
        "hypertension first-line treatment",
    ]
    events = memory_sink.of_type(RetrievalEvent)
    assert [event.query for event in events] == result.data["sub_queries"]
    assert all(event.skill == "deep_research" for event in events)


def test_deep_research_reports_where_guidance_differs(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """The corpus disagrees with itself on purpose; this is the skill that has to surface it."""
    result = registry.run(
        "deep_research",
        {"question": "hypertension diagnosis threshold", "sub_queries": ["blood pressure targets"]},
        skill_context,
    )

    disagreements = {item["doc_id"]: item["text"] for item in result.data["disagreements"]}
    assert "guideline-hypertension-diagnosis-and-bp-targets" in disagreements
    text = disagreements["guideline-hypertension-diagnosis-and-bp-targets"]
    assert text
    assert not text.startswith("#")
    assert "## " not in text  # the section stops at the next heading


def test_deep_research_without_the_corpus_map_degrades_instead_of_failing(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """`SkillContext.documents` is optional, so the skill must still answer without it."""
    without = SkillContext(
        retriever=skill_context.retriever,
        red_flags=skill_context.red_flags,
        symptoms=skill_context.symptoms,
        tracer=skill_context.tracer,
        agent=skill_context.agent,
    )
    result = registry.run("deep_research", {"question": "hypertension targets"}, without)
    assert result.ok is True
    assert result.data["findings"]


def test_sub_query_planning_leads_with_the_question_and_caps_the_count() -> None:
    assert plan_sub_queries("Q", []) == ["Q", *[f"Q {aspect}" for aspect in FALLBACK_ASPECTS]]
    assert plan_sub_queries("Q", ["Q", "a", "a", "b"]) == ["Q", "a", "b"]
    assert len(plan_sub_queries("Q", [f"s{n}" for n in range(9)])) == MAX_SUB_QUERIES


def test_sub_query_planning_ignores_blank_entries() -> None:
    assert plan_sub_queries("Q", ["  ", ""]) == [
        "Q",
        *[f"Q {aspect}" for aspect in FALLBACK_ASPECTS],
    ]
