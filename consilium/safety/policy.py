"""Safety: the agent policy file and its loader.

``data/policy.yaml`` is the single source of truth for what each agent may do.  Phase 4 gives it
per-agent permitted-skill lists; Phase 7 expands the same file with required output elements,
forbidden behaviours, and a path reference to ``data/red_flags.yaml`` -- referenced, never restated,
so the emergency-symptom list exists exactly once.

**This module does not validate skill names against the registry, deliberately.**  The safety layer
is substrate; the Skills layer sits above it, and substrate may not import a layer above it.  The
check happens where both are visible -- ``BaseAgent`` narrows the registry to its permitted list at
construction, and an unknown name raises there.  That is also the right moment for it: an agent that
cannot be built is a loud failure, whereas a policy validated in isolation could still name a skill
the running system does not have.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: Bumped when the file's required structure changes.  1 is the Phase 4 shape: agents with a
#: description and a permitted-skill list.  Phase 7's expansion bumps it to 2.
SCHEMA_VERSION = 1

DEFAULT_POLICY_PATH = Path("data/policy.yaml")


class PolicyError(RuntimeError):
    """Raised when the policy file cannot be read or fails validation."""


class AgentPolicy(BaseModel):
    """What one agent is permitted to do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    permitted_skills: tuple[str, ...] = Field(min_length=1)


class Policy:
    """The loaded policy file."""

    def __init__(
        self, agents: dict[str, AgentPolicy], *, schema_version: int = SCHEMA_VERSION
    ) -> None:
        if not agents:
            raise PolicyError("the policy file names no agents; refusing to run unconstrained")
        self.agents = dict(agents)
        self.schema_version = schema_version

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_POLICY_PATH) -> Policy:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PolicyError(f"cannot read the policy at {path}: {exc}") from exc
        if not isinstance(raw, dict) or "agents" not in raw:
            raise PolicyError(f"{path}: expected a mapping with an 'agents' key")

        block = raw["agents"]
        if not isinstance(block, dict):
            raise PolicyError(f"{path}: 'agents' must be a mapping of agent name to policy")
        try:
            agents = {name: AgentPolicy.model_validate(entry) for name, entry in block.items()}
        except ValidationError as exc:
            raise PolicyError(f"{path}: invalid agent policy: {exc}") from exc

        return cls(agents, schema_version=int(raw.get("schema_version", SCHEMA_VERSION)))

    def for_agent(self, name: str) -> AgentPolicy:
        try:
            return self.agents[name]
        except KeyError:
            known = ", ".join(sorted(self.agents))
            raise PolicyError(f"no policy for agent {name!r}; the file names: {known}") from None

    def permitted_skills(self, name: str) -> tuple[str, ...]:
        return self.for_agent(name).permitted_skills

    def description(self, name: str) -> str:
        """The capability description, used verbatim in the planner prompt.

        One description, read by both the planner and the policy, so the router cannot be told an
        agent does something the policy does not let it do.
        """
        return " ".join(self.for_agent(name).description.split())

    def __len__(self) -> int:
        return len(self.agents)

    def __iter__(self) -> Iterator[str]:
        return iter(self.agents)

    def __contains__(self, name: object) -> bool:
        return name in self.agents
