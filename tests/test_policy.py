"""The policy loader, and the consistency of `data/policy.yaml` with the rest of the system."""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.safety import Policy, PolicyError
from consilium.skills import SKILL_NAMES

POLICY_PATH = Path("data/policy.yaml")


def test_the_shipped_policy_loads_and_names_the_three_agents(policy: Policy) -> None:
    assert len(policy) == 3
    assert set(policy) == {"consultation", "diagnostic", "research"}
    assert policy.schema_version == 1


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


def test_the_policy_file_does_not_restate_the_red_flag_list() -> None:
    """`policy.yaml` references `data/red_flags.yaml`; it never carries a second copy of it."""
    text = POLICY_PATH.read_text(encoding="utf-8")
    assert "chest pain" not in text
    assert "patterns:" not in text
