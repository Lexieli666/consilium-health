"""Safety: the agent policy file and its loader.

``data/policy.yaml`` is the single source of truth for what each agent may do and what any answer
may contain.  Phase 4 gave it per-agent permitted-skill lists; Phase 7 added the ``output`` block
-- required elements, forbidden behaviours, the escalation banner -- and bumped ``schema_version``
to 2.

**The red-flag list is referenced by path and never restated.**  ``policy.yaml`` names the file;
``red_flags.yaml`` holds the phrases.  Two copies would eventually disagree, and the copy that lost
would be the one deciding whether a user is told to seek care.

**Forbidden patterns are regular expressions**, unlike the red-flag table's literal phrases.  Forced
by what is matched: a dose is a number followed by a unit, which no list of literal phrases can
express.  The cost is a list that is less readable by a non-programmer, so it is kept short, each
entry is commented in the YAML, and every pattern is matched one sentence at a time.

**This module does not validate skill names against the registry, deliberately.**  The safety layer
is substrate; the Skills layer sits above it, and substrate may not import a layer above it.  The
check happens where both are visible -- ``BaseAgent`` narrows the registry to its permitted list at
construction, and an unknown name raises there.  That is also the right moment for it: an agent that
cannot be built is a loud failure, whereas a policy validated in isolation could still name a skill
the running system does not have.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: Bumped when the file's required structure changes.  1 was the Phase 4 shape -- agents with a
#: description and a permitted-skill list.  2 adds the ``output`` block and the ``red_flags`` path.
SCHEMA_VERSION = 2

DEFAULT_POLICY_PATH = Path("data/policy.yaml")


class PolicyError(RuntimeError):
    """Raised when the policy file cannot be read or fails validation."""


class RequiredElement(BaseModel):
    """Something every answer must contain, and how to put it back when it does not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    #: Where the repair puts the text back.  The disclaimer is boilerplate and goes last.
    repair: Literal["append", "prepend"] = "append"
    text: str

    @property
    def normalized(self) -> str:
        """The text with runs of whitespace collapsed, which is how presence is checked.

        An answer wrapped for a terminal or for HTML carries the disclaimer across two lines; an
        exact substring check would call that missing and append a second copy.
        """
        return " ".join(self.text.split())


class ForbiddenBehaviour(BaseModel):
    """A pattern an answer must not contain, and the marker that replaces the sentence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    replacement: str
    patterns: tuple[str, ...] = Field(min_length=1)

    @cached_property
    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(pattern, re.IGNORECASE) for pattern in self.patterns)

    def matches(self, sentence: str) -> str | None:
        """The first pattern this sentence trips, or ``None``."""
        for pattern in self.compiled:
            if pattern.search(sentence):
                return pattern.pattern
        return None


class EscalationPolicy(BaseModel):
    """What is prepended when a red-flag input produced an answer that does not escalate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = "escalation_required"
    description: str
    banner: str

    @property
    def text(self) -> str:
        return " ".join(self.banner.split())


class OutputPolicy(BaseModel):
    """Everything that governs a delivered answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    escalation: EscalationPolicy
    required: tuple[RequiredElement, ...] = ()
    forbidden: tuple[ForbiddenBehaviour, ...] = ()

    def required_element(self, element_id: str) -> RequiredElement:
        for element in self.required:
            if element.id == element_id:
                return element
        raise PolicyError(f"no required output element {element_id!r}")


class AgentPolicy(BaseModel):
    """What one agent is permitted to do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    permitted_skills: tuple[str, ...] = Field(min_length=1)


class Policy:
    """The loaded policy file."""

    def __init__(
        self,
        agents: dict[str, AgentPolicy],
        *,
        output: OutputPolicy | None = None,
        red_flags_path: Path | None = None,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        if not agents:
            raise PolicyError("the policy file names no agents; refusing to run unconstrained")
        self.agents = dict(agents)
        self._output = output
        self.red_flags_path = red_flags_path
        self.schema_version = schema_version

    @property
    def output(self) -> OutputPolicy:
        """The output policy.  Raises rather than defaulting to an empty one.

        A permissive default would mean a policy file that failed to load left the system running
        with no output constraints at all -- the failure mode where the guard is silently absent and
        every metric still reports a clean run.
        """
        if self._output is None:
            raise PolicyError(
                "this policy has no output block; schema_version 2 requires one under `output`"
            )
        return self._output

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

        output: OutputPolicy | None = None
        if "output" in raw:
            try:
                output = OutputPolicy.model_validate(raw["output"])
            except ValidationError as exc:
                raise PolicyError(f"{path}: invalid output policy: {exc}") from exc

        red_flags_path: Path | None = None
        if "red_flags" in raw:
            # Resolved relative to the policy file, so moving `data/` moves both together and the
            # reference cannot be broken by running the process from a different directory.
            candidate = Path(str(raw["red_flags"]))
            red_flags_path = candidate if candidate.is_absolute() else Path(path).parent / candidate

        return cls(
            agents,
            output=output,
            red_flags_path=red_flags_path,
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
        )

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
