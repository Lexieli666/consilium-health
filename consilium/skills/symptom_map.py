"""Skills layer: the symptom -> body-system table and its matcher.

Loads ``data/symptom_systems.yaml`` for the ``analyze_symptoms`` skill.

**Why this lives in the Skills layer and the red-flag table does not.**  ``RedFlagTable`` sits in
``consilium/safety/`` because two layers consume it -- the ``assess_risk`` skill and
``OutputRepair`` -- and a second copy of the matching logic would let a symptom escalate through
one path and not the other.  This table has exactly one consumer, so the layer that consumes it
is the layer it belongs to.  Promoting it to substrate on symmetry alone would put a table in the
foundation that nothing in the foundation reads.

**A term may belong to more than one system, and the matcher reports all of them.**  "Chest
tightness" is cardiovascular and respiratory.  Forcing a single owner would make the table encode a
diagnosis, which is the one thing this skill is not entitled to do; reporting both is what lets the
single-versus-multi-system output stay descriptive.

Matching is the same contract as the red-flag table -- literal lowercase phrases, case-insensitive,
on word boundaries, longest first -- so that a reviewer who has read one file can read the other.
The negation guard is deliberately absent: this table says where a symptom is felt, and "no chest
pain" still tells you the person is describing a cardiovascular concern.  Urgency, where negation
changes the answer, lives in the red-flag table.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from consilium.safety.red_flags import BodySystem

#: The ten systems from the brief.  ``constitutional`` is the whole-body bucket -- fever, weight
#: loss, fatigue -- and is not a fallback for terms that fit nowhere: those are reported as
#: unmapped, because silently filing an unrecognized symptom under a real system invents a finding.
SYSTEMS: tuple[BodySystem, ...] = (
    "cardiovascular",
    "respiratory",
    "gastrointestinal",
    "neurological",
    "musculoskeletal",
    "genitourinary",
    "dermatological",
    "endocrine",
    "psychiatric",
    "constitutional",
)

SCHEMA_VERSION = 1


class SymptomMapError(RuntimeError):
    """Raised when the symptom table cannot be loaded or fails validation."""


class SymptomMatch(BaseModel):
    """One table term found in the input, with the systems it maps to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    term: str
    systems: tuple[BodySystem, ...] = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class SymptomSystemMap:
    """The loaded term table and the matcher over it."""

    def __init__(
        self, systems: Mapping[BodySystem, Sequence[str]], labels: Mapping[str, str]
    ) -> None:
        if not systems:
            raise SymptomMapError("the symptom table is empty")
        self.labels = dict(labels)

        term_systems: dict[str, list[BodySystem]] = {}
        for system, terms in systems.items():
            for term in terms:
                term_systems.setdefault(term, []).append(system)
        self.terms: dict[str, tuple[BodySystem, ...]] = {
            term: tuple(found) for term, found in term_systems.items()
        }
        # Longest phrase first, so "coughing up blood" wins over "cough" and the recorded span is
        # the most specific term that matched.
        self._compiled: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
            (term, re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE))
            for term in sorted(self.terms, key=len, reverse=True)
        )

    @classmethod
    def from_yaml(cls, path: Path) -> SymptomSystemMap:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SymptomMapError(f"cannot read the symptom table at {path}: {exc}") from exc
        if not isinstance(raw, dict) or "systems" not in raw:
            raise SymptomMapError(f"{path}: expected a mapping with a 'systems' key")

        systems: dict[BodySystem, Sequence[str]] = {}
        labels: dict[str, str] = {}
        for name, block in raw["systems"].items():
            if name not in SYSTEMS:
                raise SymptomMapError(f"{path}: {name!r} is not one of the ten body systems")
            if not isinstance(block, dict) or "terms" not in block:
                raise SymptomMapError(f"{path}: system {name!r} needs a 'terms' list")
            terms = block["terms"]
            if not terms:
                raise SymptomMapError(f"{path}: system {name!r} has no terms")
            systems[name] = list(terms)
            labels[name] = str(block.get("label", name))
        return cls(systems, labels)

    def match(self, text: str) -> tuple[SymptomMatch, ...]:
        """Every table term found in ``text``, longest-first, without overlaps."""
        found: list[SymptomMatch] = []
        claimed: list[tuple[int, int]] = []
        for term, regex in self._compiled:
            for hit in regex.finditer(text):
                span = (hit.start(), hit.end())
                if any(start <= span[0] and span[1] <= end for start, end in claimed):
                    continue
                claimed.append(span)
                found.append(
                    SymptomMatch(term=term, systems=self.terms[term], start=span[0], end=span[1])
                )
        found.sort(key=lambda item: (item.start, item.term))
        return tuple(found)

    def group(self, text: str) -> dict[BodySystem, list[str]]:
        """Matched terms grouped by system, systems in the canonical order of :data:`SYSTEMS`."""
        grouped: dict[BodySystem, list[str]] = {}
        for match in self.match(text):
            for system in match.systems:
                terms = grouped.setdefault(system, [])
                if match.term not in terms:
                    terms.append(match.term)
        return {system: grouped[system] for system in SYSTEMS if system in grouped}

    def label(self, system: str) -> str:
        return self.labels.get(system, system)

    def __len__(self) -> int:
        return len(self.terms)

    def __iter__(self) -> Iterator[str]:
        return iter(self.terms)
