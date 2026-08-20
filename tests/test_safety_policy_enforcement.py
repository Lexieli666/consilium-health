"""The safety layer: detection, repair, and the two counts that must never be merged.

A safety mechanism with no measured trigger rate is decoration, and a trigger rate with no measured
false-negative rate is worse. These tests pin the mechanism; the rates come from the eval harness.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.safety import (
    UNPERMITTED_SKILL,
    OutputRepair,
    Policy,
    PolicyValidator,
    RedFlagTable,
    escalation_present,
    sentences,
    violation_rules,
)
from consilium.trace import MemorySink, SafetyEvent, Tracer

DISCLAIMER_PATH = Path("data/policy.yaml")


@pytest.fixture
def validator(policy: Policy) -> PolicyValidator:
    return PolicyValidator(policy)


@pytest.fixture
def repair(policy: Policy) -> OutputRepair:
    return OutputRepair(policy)


@pytest.fixture
def disclaimer(policy: Policy) -> str:
    return policy.output.required_element("disclaimer").normalized


def _tracer(sink: MemorySink) -> Tracer:
    return Tracer(session_id="safety", turn_index=0, sink=sink)


def _events(sink: MemorySink, kind: str) -> list[SafetyEvent]:
    return [event for event in sink.of_type(SafetyEvent) if event.event == kind]


# --- tool-call scope -----------------------------------------------------------------------------


def test_a_permitted_skill_passes_and_emits_nothing(
    validator: PolicyValidator, memory_sink: MemorySink
) -> None:
    verdict = validator.check_tool_call(
        agent="diagnostic", skill="assess_risk", tracer=_tracer(memory_sink)
    )

    assert verdict.allowed is True
    assert memory_sink.of_type(SafetyEvent) == []


def test_an_unpermitted_skill_is_blocked_and_counted(
    validator: PolicyValidator, memory_sink: MemorySink
) -> None:
    """The loop refuses it anyway; the validator is what makes the refusal countable."""
    verdict = validator.check_tool_call(
        agent="diagnostic", skill="deep_research", tracer=_tracer(memory_sink)
    )

    assert verdict.allowed is False
    assert verdict.violation is not None
    assert verdict.violation.rule == UNPERMITTED_SKILL
    (event,) = _events(memory_sink, "violation")
    assert event.scope == "tool_call"
    assert event.agent == "diagnostic"
    assert "deep_research" in event.detail


async def test_the_loop_blocks_an_unpermitted_skill_and_the_event_is_written(
    registry: object, policy: Policy, skill_context: object, memory_sink: MemorySink
) -> None:
    """End to end: a model asking for a tool its agent may not use produces a violation event."""
    from consilium.agents.loop import ReActLoop
    from consilium.llm import MockProvider, ScriptedResponse
    from consilium.llm.mock import ScriptedToolCall
    from consilium.skills import SkillContext, SkillRegistry

    assert isinstance(registry, SkillRegistry)
    assert isinstance(skill_context, SkillContext)
    ctx = SkillContext(
        retriever=skill_context.retriever,
        red_flags=skill_context.red_flags,
        symptoms=skill_context.symptoms,
        tracer=_tracer(memory_sink),
        agent="diagnostic",
    )
    loop = ReActLoop(
        provider=MockProvider(
            [
                ScriptedResponse(
                    tool_calls=[ScriptedToolCall(name="deep_research", arguments={"question": "q"})]
                ),
                ScriptedResponse(content="Answered without it."),
            ]
        ),
        registry=registry,
        validator=PolicyValidator(policy),
    )

    result = await loop.run(
        system_prompt="s",
        question="q",
        ctx=ctx,
        permitted=policy.permitted_skills("diagnostic"),
    )

    assert result.tool_results[0].ok is False
    assert [event.rule for event in _events(memory_sink, "violation")] == [UNPERMITTED_SKILL]


# --- output scope: detection ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "rule"),
    [
        ("Guidance describes taking 500 mg twice daily.", "dosing_instruction"),
        ("Take 2 tablets with water.", "dosing_instruction"),
        ("You definitely have asthma.", "definitive_diagnosis"),
        ("Your diagnosis is gastro-oesophageal reflux.", "definitive_diagnosis"),
        ("You should stop your current medication.", "prescription_advice"),
        ("You should switch to a different inhaler.", "prescription_advice"),
        ("This is nothing to worry about.", "false_reassurance"),
        ("There is no need to see a doctor about this.", "false_reassurance"),
    ],
)
def test_forbidden_content_is_detected_per_rule(
    validator: PolicyValidator, answer: str, rule: str
) -> None:
    violations = validator.check_output(answer)
    assert rule in violation_rules(violations)


def test_ordinary_clinical_prose_is_not_flagged(
    validator: PolicyValidator, disclaimer: str
) -> None:
    """False positives here delete real sentences, so the patterns are tight on purpose."""
    answer = (
        "Guidance describes lifestyle change as a first-line option for stage 1 hypertension. "
        "A clinician can discuss whether medication is appropriate. "
        "Blood pressure is usually measured on more than one occasion before a diagnosis is made. "
        f"{disclaimer}"
    )
    assert violation_rules(validator.check_output(answer)) == ()


def test_a_missing_disclaimer_is_a_violation(validator: PolicyValidator) -> None:
    assert "disclaimer" in violation_rules(validator.check_output("An answer with no boilerplate."))


def test_a_wrapped_disclaimer_still_counts_as_present(
    validator: PolicyValidator, disclaimer: str
) -> None:
    """An answer wrapped for a terminal carries it across lines; a second copy would be appended."""
    wrapped = "An answer.\n" + disclaimer.replace(" ", "\n", 3)
    assert "disclaimer" not in violation_rules(validator.check_output(wrapped))


def test_a_red_flag_input_with_no_escalation_is_a_violation(
    validator: PolicyValidator, red_flag_table: RedFlagTable
) -> None:
    assessment = red_flag_table.assess("I have crushing chest pain")

    violations = validator.check_output("Chest discomfort has many causes.", assessment=assessment)

    assert "escalation_required" in violation_rules(violations)


def test_a_red_flag_input_that_already_escalates_is_not_a_violation(
    validator: PolicyValidator, red_flag_table: RedFlagTable
) -> None:
    """This is why red-flag recall cannot be computed from repair events."""
    assessment = red_flag_table.assess("I have crushing chest pain")

    violations = validator.check_output("Call emergency services now.", assessment=assessment)

    assert "escalation_required" not in violation_rules(violations)


def test_escalation_is_decided_on_the_input_not_the_answer(
    validator: PolicyValidator, red_flag_table: RedFlagTable
) -> None:
    """An answer that never mentions the symptom would otherwise pass by saying nothing."""
    assessment = red_flag_table.assess("I have crushing chest pain")

    violations = validator.check_output(
        "Here is some information about diet.", assessment=assessment
    )

    assert "escalation_required" in violation_rules(violations)


def test_a_negated_red_flag_does_not_require_escalation(
    validator: PolicyValidator, red_flag_table: RedFlagTable
) -> None:
    assessment = red_flag_table.assess("I have no chest pain, just a cough")

    violations = validator.check_output("Coughs have many causes.", assessment=assessment)

    assert "escalation_required" not in violation_rules(violations)


def test_every_detection_emits_exactly_one_violation_event(
    validator: PolicyValidator, memory_sink: MemorySink
) -> None:
    validator.check_output("You definitely have asthma.", tracer=_tracer(memory_sink))

    rules = [event.rule for event in _events(memory_sink, "violation")]
    assert sorted(rules) == ["definitive_diagnosis", "disclaimer"]
    assert all(event.scope == "output" for event in _events(memory_sink, "violation"))


def test_sentence_splitting_keeps_punctuation_and_drops_blanks() -> None:
    assert sentences("One. Two!  Three?  ") == ["One.", "Two!", "Three?"]
    assert sentences("   ") == []


# --- output scope: repair ------------------------------------------------------------------------


def test_a_forbidden_sentence_is_removed_and_the_rest_delivered(
    validator: PolicyValidator, repair: OutputRepair, memory_sink: MemorySink
) -> None:
    """Removed, never rephrased: a rewrite produces text nobody wrote and nobody checked."""
    answer = (
        "Asthma is a chronic airway condition. You definitely have asthma. "
        "Guidance describes inhalers."
    )
    tracer = _tracer(memory_sink)

    result = repair.apply(answer, validator.check_output(answer, tracer=tracer), tracer=tracer)

    assert "Asthma is a chronic airway condition." in result.answer
    assert "Guidance describes inhalers." in result.answer
    assert "You definitely have asthma." not in result.answer
    assert "[removed: this system does not diagnose.]" in result.answer
    assert result.redacted == ("You definitely have asthma.",)
    assert "definitive_diagnosis" in result.repairs


def test_the_escalation_banner_is_prepended_and_comes_first(
    validator: PolicyValidator,
    repair: OutputRepair,
    red_flag_table: RedFlagTable,
    policy: Policy,
) -> None:
    assessment = red_flag_table.assess("I have crushing chest pain")
    answer = "Chest discomfort has many causes."

    result = repair.apply(answer, validator.check_output(answer, assessment=assessment))

    assert result.answer.startswith(policy.output.escalation.text)
    assert result.escalation_present_pre_repair is False
    assert result.escalation_present_post_repair is True
    assert escalation_present(result.answer)


def test_the_disclaimer_is_appended_last(
    validator: PolicyValidator,
    repair: OutputRepair,
    red_flag_table: RedFlagTable,
    disclaimer: str,
) -> None:
    """Banner first because it must be read first; disclaimer last because it is boilerplate."""
    assessment = red_flag_table.assess("I have crushing chest pain")
    answer = "Chest discomfort has many causes."

    result = repair.apply(answer, validator.check_output(answer, assessment=assessment))

    assert result.answer.rstrip().endswith(disclaimer)
    assert result.repairs == ("escalation_required", "disclaimer")


def test_a_compliant_answer_is_returned_unchanged(
    validator: PolicyValidator, repair: OutputRepair, disclaimer: str
) -> None:
    answer = f"Guidance describes lifestyle change as a first-line option.\n\n{disclaimer}"

    result = repair.apply(answer, validator.check_output(answer))

    assert result.answer == answer
    assert result.repairs == ()
    assert result.repair_applied is False


def test_violations_and_repairs_are_two_separate_event_streams(
    validator: PolicyValidator, repair: OutputRepair, memory_sink: MemorySink
) -> None:
    """Reported as two rates and never merged."""
    tracer = _tracer(memory_sink)
    answer = "This is nothing to worry about."

    violations = validator.check_output(answer, tracer=tracer)
    repair.apply(answer, violations, tracer=tracer)

    assert {event.rule for event in _events(memory_sink, "violation")} == {
        "false_reassurance",
        "disclaimer",
    }
    assert {event.rule for event in _events(memory_sink, "repair")} == {
        "false_reassurance",
        "disclaimer",
    }
    assert len(memory_sink.of_type(SafetyEvent)) == 4


def test_post_stream_is_recorded_only_when_asked_for(
    validator: PolicyValidator, repair: OutputRepair, memory_sink: MemorySink
) -> None:
    """Only the SSE path sets it: a repair the user has already seen the unrepaired version of."""
    tracer = _tracer(memory_sink)
    answer = "An answer."

    repair.apply(answer, validator.check_output(answer), tracer=tracer, post_stream=True)

    assert all(event.post_stream for event in _events(memory_sink, "repair"))
    assert not any(event.post_stream for event in _events(memory_sink, "violation"))


def test_the_repair_never_produces_an_answer_without_the_disclaimer(
    validator: PolicyValidator, repair: OutputRepair, red_flag_table: RedFlagTable, disclaimer: str
) -> None:
    """The one invariant the whole layer exists to keep, across every combination of repairs."""
    assessment = red_flag_table.assess("I have crushing chest pain and my throat is closing")
    for answer in (
        "",
        "You definitely have a heart attack. Take 300 mg now.",
        "Call emergency services now.",
        "There is nothing to worry about.",
    ):
        result = repair.apply(answer, validator.check_output(answer, assessment=assessment))
        assert disclaimer in " ".join(result.answer.split())
        assert escalation_present(result.answer)
