"""The symptom -> body-system table and its matcher.

The table is data, so the tests are mostly about the contract it advertises: the ten systems and
nothing else, longest-phrase-first matching, a term may belong to two systems, and the file is
consistent with the red-flag table it sits beside.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.skills import SYSTEMS, SymptomMapError, SymptomSystemMap

TABLE_PATH = Path("data/symptom_systems.yaml")


def test_table_covers_the_ten_systems_with_enough_terms_each(symptom_map: SymptomSystemMap) -> None:
    grouped: dict[str, int] = dict.fromkeys(SYSTEMS, 0)
    for term, systems in symptom_map.terms.items():
        assert term == term.lower(), term
        for system in systems:
            grouped[system] += 1
    assert set(grouped) == set(SYSTEMS)
    assert all(15 <= count <= 30 for count in grouped.values()), grouped


def test_longest_phrase_wins_over_a_shorter_one_inside_it(symptom_map: SymptomSystemMap) -> None:
    (match,) = symptom_map.match("I have been coughing up blood")
    assert match.term == "coughing up blood"


def test_a_shared_term_reports_every_system_it_belongs_to(symptom_map: SymptomSystemMap) -> None:
    (match,) = symptom_map.match("chest tightness when I walk")
    assert set(match.systems) == {"cardiovascular", "respiratory"}


def test_grouping_uses_the_canonical_system_order(symptom_map: SymptomSystemMap) -> None:
    grouped = symptom_map.group("a rash, a fever, and palpitations")
    assert list(grouped) == ["cardiovascular", "dermatological", "constitutional"]


def test_matching_is_case_insensitive_and_on_word_boundaries(
    symptom_map: SymptomSystemMap,
) -> None:
    assert symptom_map.match("COUGH")
    assert not symptom_map.match("coughdrop")


def test_labels_are_available_for_every_system(symptom_map: SymptomSystemMap) -> None:
    assert all(symptom_map.label(system) for system in SYSTEMS)
    assert symptom_map.label("constitutional") == "Constitutional (whole-body)"


def test_the_map_carries_no_urgency_and_never_substitutes_for_the_rule_table() -> None:
    """Urgency lives in data/red_flags.yaml and only there.

    Checked structurally rather than by scanning for the word: "urinary urgency" is a legitimate
    genitourinary term, and a substring scan would forbid it.  What must not exist is an urgency
    *field* -- a system block carries a label and a term list, nothing else -- because a second
    place to write an urgency tier is a second place for the two to disagree.
    """
    import yaml

    raw = yaml.safe_load(TABLE_PATH.read_text(encoding="utf-8"))
    for name, block in raw["systems"].items():
        assert set(block) == {"label", "terms"}, name


def test_an_unknown_system_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("systems:\n  cardiology:\n    terms: ['chest pain']\n", encoding="utf-8")
    with pytest.raises(SymptomMapError, match="not one of the ten body systems"):
        SymptomSystemMap.from_yaml(path)


def test_a_system_with_no_terms_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("systems:\n  cardiovascular:\n    terms: []\n", encoding="utf-8")
    with pytest.raises(SymptomMapError, match="has no terms"):
        SymptomSystemMap.from_yaml(path)


def test_a_system_without_a_terms_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("systems:\n  cardiovascular:\n    label: x\n", encoding="utf-8")
    with pytest.raises(SymptomMapError, match="needs a 'terms' list"):
        SymptomSystemMap.from_yaml(path)


def test_a_file_without_a_systems_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("entries: []\n", encoding="utf-8")
    with pytest.raises(SymptomMapError, match="expected a mapping"):
        SymptomSystemMap.from_yaml(path)


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SymptomMapError, match="cannot read"):
        SymptomSystemMap.from_yaml(tmp_path / "absent.yaml")


def test_an_empty_table_is_refused() -> None:
    with pytest.raises(SymptomMapError, match="empty"):
        SymptomSystemMap({}, {})


def test_the_map_is_a_container_of_its_terms(symptom_map: SymptomSystemMap) -> None:
    assert len(symptom_map) == len(set(symptom_map))
    assert "chest tightness" in set(symptom_map)


def test_an_unknown_system_label_falls_back_to_its_name(symptom_map: SymptomSystemMap) -> None:
    assert symptom_map.label("not-a-system") == "not-a-system"


def test_overlapping_matches_are_claimed_once(symptom_map: SymptomSystemMap) -> None:
    """ "Chest pain" inside "chest pain" must not be reported twice by two overlapping terms."""
    matches = symptom_map.match("chest pain, chest pain")
    assert [match.term for match in matches] == ["chest pain", "chest pain"]
    assert matches[0].start != matches[1].start
