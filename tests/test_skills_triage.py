"""The two triage skills: `assess_risk` and `analyze_symptoms`.

`assess_risk` is the skill red-flag recall ultimately depends on, so its contract is pinned hard:
the tier comes from the rule table and not from retrieved prose, the negation guard's decision is
reported rather than hidden, and a non-match says in words that it is not a clearance.
"""

from __future__ import annotations

from consilium.skills import NO_MATCH_ACTION, SkillContext, SkillRegistry
from consilium.trace import MemorySink, RetrievalEvent


def test_emergency_phrase_produces_an_emergency_tier_and_an_action(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "assess_risk",
        {"symptoms": "I have crushing chest pain spreading to my arm"},
        skill_context,
    )

    assert result.ok is True
    assert result.data["urgency"] == "emergency"
    assert "emergency services" in result.data["action"]
    assert {match["rule_id"] for match in result.data["matched"]} == {"chest_pain_cardiac"}
    # The rule's own note leads the citations; the retrieved prose follows it.
    assert result.sources[0] == "red-flag-chest-pain"


def test_the_tier_comes_from_the_table_not_from_the_retrieved_text(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    """With retrieval switched off the tier is unchanged, which is the point of the rule table."""
    without_corpus = SkillContext(
        red_flags=skill_context.red_flags,
        symptoms=skill_context.symptoms,
        tracer=skill_context.tracer,
        agent=skill_context.agent,
    )
    result = registry.run("assess_risk", {"symptoms": "my throat is closing up"}, without_corpus)

    assert result.ok is True
    assert result.data["urgency"] == "emergency"
    assert result.data["passages"] == []
    assert result.sources == ("red-flag-anaphylaxis",)


def test_negated_phrase_is_reported_as_suppressed_rather_than_dropped(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "assess_risk", {"symptoms": "I have no chest pain but I am very tired"}, skill_context
    )

    assert result.data["urgency"] == "routine"
    assert result.data["matched"] == []
    (suppressed,) = result.data["negation_suppressed"]
    assert suppressed["rule_id"] == "chest_pain_cardiac"
    assert suppressed["negated_by"] == "no"


def test_no_match_says_explicitly_that_it_is_not_a_clearance(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "assess_risk", {"symptoms": "my left knee aches after gardening"}, skill_context
    )

    assert result.data["urgency"] == "routine"
    assert result.data["action"] == NO_MATCH_ACTION
    assert "not a clearance" in result.data["action"]


def test_supporting_retrieval_is_scoped_to_red_flag_notes_only_when_one_fired(
    registry: SkillRegistry, skill_context: SkillContext, memory_sink: MemorySink
) -> None:
    registry.run("assess_risk", {"symptoms": "my throat is closing up"}, skill_context)
    registry.run("assess_risk", {"symptoms": "my left knee aches"}, skill_context)

    fired, not_fired = memory_sink.of_type(RetrievalEvent)
    assert fired.category_filter == "red_flag"
    assert not_fired.category_filter is None


def test_assess_risk_without_the_rule_table_fails_loudly(registry: SkillRegistry) -> None:
    result = registry.run("assess_risk", {"symptoms": "chest pain"}, SkillContext())
    assert result.ok is False
    assert result.error is not None and "red-flag table" in result.error


def test_symptoms_in_one_system_are_reported_as_single_system(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "analyze_symptoms",
        {"symptoms": "I have a cough and a wheeze and a sore throat"},
        skill_context,
    )

    assert result.data["pattern"] == "single-system"
    (group,) = result.data["systems"]
    assert group["system"] == "respiratory"
    assert set(group["terms"]) >= {"cough", "wheeze", "sore throat"}


def test_symptoms_across_systems_are_reported_as_multi_system(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "analyze_symptoms",
        {"symptoms": "I have swollen ankles, a cough, and I feel dizzy on standing"},
        skill_context,
    )

    assert result.data["pattern"] == "multi-system"
    systems = [group["system"] for group in result.data["systems"]]
    assert systems == ["cardiovascular", "respiratory"]  # canonical order, not match order


def test_a_term_belonging_to_two_systems_is_reported_under_both(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run("analyze_symptoms", {"symptoms": "chest tightness"}, skill_context)

    systems = {group["system"]: group["terms"] for group in result.data["systems"]}
    assert systems["cardiovascular"] == ["chest tightness"]
    assert systems["respiratory"] == ["chest tightness"]
    assert result.data["pattern"] == "multi-system"


def test_unrecognized_input_says_so_instead_of_claiming_no_systems(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run("analyze_symptoms", {"symptoms": "qqqq zzzz"}, skill_context)

    assert result.data["pattern"] == "unrecognized"
    assert result.data["systems"] == []
    assert result.data["matched_terms"] == []


def test_candidate_conditions_are_documents_to_read_not_a_differential(
    registry: SkillRegistry, skill_context: SkillContext
) -> None:
    result = registry.run(
        "analyze_symptoms", {"symptoms": "wheeze and breathlessness at night"}, skill_context
    )

    assert all(
        candidate["doc_id"].startswith("condition-")
        for candidate in result.data["candidate_conditions"]
    )
    assert set(result.sources) == {c["doc_id"] for c in result.data["candidate_conditions"]}


def test_analyze_symptoms_without_the_table_fails_loudly(registry: SkillRegistry) -> None:
    result = registry.run("analyze_symptoms", {"symptoms": "cough"}, SkillContext())
    assert result.ok is False
    assert result.error is not None and "symptom-system table" in result.error
