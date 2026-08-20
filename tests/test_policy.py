"""The policy loader, and the consistency of `data/policy.yaml` with the rest of the system."""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.safety import Policy, PolicyError, RedFlagTable
from consilium.skills import SKILL_NAMES

POLICY_PATH = Path("data/policy.yaml")


def test_the_shipped_policy_loads_and_names_the_three_agents(policy: Policy) -> None:
    assert len(policy) == 3
    assert set(policy) == {"consultation", "diagnostic", "research"}
    assert policy.schema_version == 2


def test_every_agent_has_a_description_the_planner_can_use(policy: Policy) -> None:
    for name in policy:
        description = policy.description(name)
        assert len(description) > 40
        assert "\n" not in description  # folded to one line for the planner prompt


def test_no_permitted_skill_is_outside_the_seven(policy: Policy) -> None:
    for name in policy:
        unknown = set(policy.permitted_skills(name)) - set(SKILL_NAMES)
        assert not unknown, (name, unknown)


def test_asking_for_an_unknown_agent_names_the_ones_that_exist(policy: Policy) -> None:
    with pytest.raises(PolicyError, match="consultation, diagnostic, research"):
        policy.permitted_skills("triage")


def test_membership_and_iteration(policy: Policy) -> None:
    assert "diagnostic" in policy
    assert "triage" not in policy
    assert sorted(policy) == ["consultation", "diagnostic", "research"]


def test_an_empty_policy_is_refused() -> None:
    with pytest.raises(PolicyError, match="refusing to run unconstrained"):
        Policy({})


def test_an_agent_with_no_permitted_skills_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        "agents:\n  consultation:\n    description: d\n    permitted_skills: []\n", encoding="utf-8"
    )
    with pytest.raises(PolicyError, match="invalid agent policy"):
        Policy.from_yaml(path)


def test_an_unknown_key_in_an_agent_block_is_rejected(tmp_path: Path) -> None:
    """`extra="forbid"`: a misspelt key must not become a silently ignored policy."""
    path = tmp_path / "policy.yaml"
    path.write_text(
        "agents:\n  consultation:\n    description: d\n    permited_skills: [search_knowledge]\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="invalid agent policy"):
        Policy.from_yaml(path)


def test_a_file_without_an_agents_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("entries: []\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="expected a mapping"):
        Policy.from_yaml(path)


def test_an_agents_key_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("agents: [consultation]\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="must be a mapping"):
        Policy.from_yaml(path)


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read the policy"):
        Policy.from_yaml(tmp_path / "absent.yaml")


def test_the_policy_file_references_the_red_flag_list_rather_than_restating_it(
    policy: Policy, red_flag_table: RedFlagTable
) -> None:
    """Two copies of that list would eventually disagree, and one of them decides escalation."""
    assert policy.red_flags_path is not None
    assert policy.red_flags_path.name == "red_flags.yaml"
    assert policy.red_flags_path.exists()

    text = POLICY_PATH.read_text(encoding="utf-8").lower()
    phrases = {pattern for rule in red_flag_table for pattern in rule.patterns}
    restated = sorted(phrase for phrase in phrases if phrase in text)
    assert not restated, f"policy.yaml restates red-flag phrases: {restated}"


def test_the_output_policy_carries_the_escalation_banner_and_the_disclaimer(
    policy: Policy,
) -> None:
    assert policy.output.escalation.id == "escalation_required"
    assert policy.output.escalation.text
    assert policy.output.required_element("disclaimer").normalized.startswith("Not medical advice.")
    assert {rule.id for rule in policy.output.forbidden} == {
        "dosing_instruction",
        "definitive_diagnosis",
        "prescription_advice",
        "false_reassurance",
    }


def test_the_escalation_banner_is_recognised_by_the_escalation_detector(policy: Policy) -> None:
    """Otherwise the repair would prepend a banner and the turn would still record no escalation."""
    from consilium.safety import escalation_present

    assert escalation_present(policy.output.escalation.text)


def test_a_policy_without_an_output_block_refuses_rather_than_permitting_everything(
    tmp_path: Path,
) -> None:
    """A permissive default would leave a failed load running with no output constraints at all."""
    path = tmp_path / "policy.yaml"
    path.write_text(
        "agents:\n  consultation:\n    description: d\n    permitted_skills: [search_knowledge]\n",
        encoding="utf-8",
    )
    loaded = Policy.from_yaml(path)

    with pytest.raises(PolicyError, match="no output block"):
        _ = loaded.output


def test_an_invalid_output_block_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        "agents:\n  consultation:\n    description: d\n    permitted_skills: [search_knowledge]\n"
        "output:\n  escalation:\n    description: d\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="invalid output policy"):
        Policy.from_yaml(path)
