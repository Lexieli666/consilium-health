"""The escalation detector.

Three fields of the `turn` event are this function, and `escalation_present_post_repair` IS
red-flag recall. So the tests are about the instrument's bias: it must not count a mention of the
word "emergency" as an instruction to seek care, because that would flatter the one number in the
project that must not be flattered.
"""

from __future__ import annotations

import pytest

from consilium.safety import escalation_phrases_found, escalation_present


@pytest.mark.parametrize(
    "answer",
    [
        "Call emergency services now.",
        "Please call 911 immediately.",
        "You should seek immediate medical care.",
        "Seek urgent medical attention today.",
        "Go to the emergency department now.",
        "This is a medical emergency.",
        "See a doctor right away.",
    ],
)
def test_explicit_instructions_are_detected(answer: str) -> None:
    assert escalation_present(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Asthma is a common cause of emergency department visits in children.",
        "Emergency medicine guidance describes this presentation.",
        "Urgent care centres are a part of the health system.",
        "Your doctor can discuss this at your next appointment.",
        "This is usually not serious.",
        "",
    ],
)
def test_mentions_that_are_not_instructions_are_not_counted(answer: str) -> None:
    """A false positive here would inflate red-flag recall, which is the error to avoid."""
    assert escalation_present(answer) is False


def test_detection_survives_a_line_break_inside_the_phrase() -> None:
    """Answers get wrapped for terminals and for HTML; that must not read as a recall failure."""
    assert escalation_present("You should seek immediate\nmedical attention.") is True


def test_detection_is_case_insensitive() -> None:
    assert escalation_present("CALL EMERGENCY SERVICES NOW") is True


def test_the_matching_phrases_are_reportable() -> None:
    found = escalation_phrases_found("Call 911 and seek immediate medical attention.")
    assert "call 911" in found
    assert "seek immediate medical" in found


def test_no_phrase_is_a_bare_keyword() -> None:
    """A one-word phrase would turn any mention of the word into a detected escalation."""
    from consilium.safety import ESCALATION_PHRASES

    assert all(len(phrase.split()) >= 2 for phrase in ESCALATION_PHRASES)
    assert len(set(ESCALATION_PHRASES)) == len(ESCALATION_PHRASES)
