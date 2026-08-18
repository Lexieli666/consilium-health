"""Safety: the red-flag rule table, its matcher, and the negation guard.

Loads ``data/red_flags.yaml`` and matches user input against it.  Two consumers share this one
implementation: the ``assess_risk`` skill and ``OutputRepair``.  Duplicating the matching logic
between them would let a symptom escalate through one path and not the other, which is the class of
bug that shows up as an unexplained gap between the safety trigger rate and red-flag recall.

Built in Phase 2 alongside the data file rather than in Phase 7 with the rest of the safety layer:
a rule table with no loader is an unverified rule table, and the negation policy below needs its
numbers before Phase 8 can report them.

**The negation guard.** A bare phrase matcher escalates on "I have no chest pain".  The first
instinct is to accept that -- a false negative on an emergency symptom is the worst error this
system can make, and a false positive costs one banner.  That reasoning is right in direction and
wrong in magnitude: an escalation banner on "no chest pain" does not cost one banner, it costs the
credibility of every subsequent banner.  Alarm fatigue is the standard failure mode of clinical
alerting, and "negation ignored for safety" reads as not having considered it.

So the guard is deliberately narrow: an explicit negation cue in the ``NEGATION_WINDOW`` tokens
immediately before a matched phrase suppresses that match.  No parser, no scope analysis, no
dependency tree.  It catches "no chest pain", "denies chest pain", "without chest pain",
"never had chest pain" and the contracted forms people actually type -- "I don't have chest pain",
"haven't had chest pain", "it isn't chest pain" -- and it deliberately does *not* catch "not sure if
this is chest pain", where the cue is too far away to be about the symptom.

Contractions are handled by normalizing ``n't`` to ``not`` in the token stream rather than by
listing every contracted form as its own cue.  One rule covers every verb, including the ones a
hand-written list would miss.

**Both outcomes are always recorded.**  :class:`RedFlagAssessment` carries the raw match and the
guarded match, and the ``turn`` trace event carries both plus their disagreement.  The choice of
default policy is then settled by two measured numbers in docs/EVALUATION.md rather than by the
argument above.  If suppression costs any recall on the labeled set, the default reverts to raw and
docs/DESIGN.md says so with the data.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from consilium.retrieval.types import Category

Urgency = Literal["routine", "non-urgent", "urgent", "emergency"]
BodySystem = Literal[
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
]

#: How many tokens before a matched phrase are searched for a negation cue.  Three is wide enough
#: for "never had chest pain" and "denies any chest pain", and narrow enough that "not sure if this
#: is chest pain" keeps its match.  Widening it trades false positives for false negatives, which is
#: the trade this constant exists to make explicit and testable.
NEGATION_WINDOW = 3

#: Explicit negation cues only.  Hedges ("maybe", "possibly") are not cues: uncertainty about an
#: emergency symptom is a reason to escalate, not to suppress.
#:
#: Contracted forms are deliberately absent.  "don't", "haven't" and "isn't" are normalized to
#: ``not`` by :func:`_normalize_token` before this set is consulted, so one cue covers every
#: contraction instead of the set growing a row per verb.  See docs/DESIGN.md.
#:
#: Note that this list mixes two vocabularies.  "denies", "denied" and "deny" are clinician
#: register -- they appear in notes written *about* a patient, not in what a patient types.  They
#: are kept because the system may be handed clinician-authored text and they cost nothing, but
#: they are not what makes the guard work on real user input; "no", "not" and the contractions are.
NEGATION_CUES: frozenset[str] = frozenset(
    {"no", "not", "never", "without", "denies", "denied", "deny", "negative", "none"}
)

#: Any token ending in "n't" is a negation, with no exceptions in English.  Covers don't, doesn't,
#: didn't, haven't, hasn't, hadn't, isn't, aren't, wasn't, weren't, can't, couldn't, wouldn't,
#: shouldn't, won't, mustn't and needn't from one rule.  Both the straight and typographic
#: apostrophe are accepted, because text arriving from a browser or a phone keyboard has the latter.
_CONTRACTED_NEGATION_RE = re.compile(r"^[a-z]+n['\u2019]t$")

#: Apostrophe-free contractions, which are common in typed input.  Only the forms that are not also
#: English words are listed: "cant" (a slope, or insincere talk) and "wont" (a habit) are excluded
#: on purpose, because suppressing an emergency match on a legitimate word is a worse error than
#: missing an informally typed negation.
_APOSTROPHE_FREE_NEGATIONS: frozenset[str] = frozenset(
    {
        "dont",
        "doesnt",
        "didnt",
        "havent",
        "hasnt",
        "hadnt",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "couldnt",
        "wouldnt",
        "shouldnt",
        "mustnt",
        "neednt",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+(?:[.'\u2019-][a-z0-9]+)*")

SCHEMA_VERSION = 1


class RedFlagError(RuntimeError):
    """Raised when the rule table cannot be loaded or fails validation."""


class RedFlagRule(BaseModel):
    """One emergency-symptom rule, as written in ``data/red_flags.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    doc_id: str
    body_system: BodySystem
    urgency: Urgency
    patterns: tuple[str, ...] = Field(min_length=1)
    action: str
    source: str


class RedFlagMatch(BaseModel):
    """One phrase of one rule found in the input, with the guard's verdict on it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    doc_id: str
    pattern: str
    urgency: Urgency
    body_system: BodySystem
    action: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    #: The negation cue that suppressed this match, or ``None`` if it survived.
    negated_by: str | None = None

    @property
    def suppressed(self) -> bool:
        return self.negated_by is not None


class RedFlagAssessment(BaseModel):
    """The result of matching one input against the whole table, under both policies.

    ``matched`` is the policy in force and is what drives the banner.  ``matched_raw`` is what a
    matcher with no negation guard would have concluded.  Keeping both is what lets the evaluation
    report recall and false-positive rate under each policy from a single run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    matches: tuple[RedFlagMatch, ...] = ()
    negation_guard: bool = True

    @property
    def surviving(self) -> tuple[RedFlagMatch, ...]:
        return tuple(m for m in self.matches if not m.suppressed)

    @property
    def suppressed(self) -> tuple[RedFlagMatch, ...]:
        return tuple(m for m in self.matches if m.suppressed)

    @property
    def matched_raw(self) -> bool:
        """Any pattern matched, ignoring negation entirely."""
        return bool(self.matches)

    @property
    def matched(self) -> bool:
        """Any pattern matched under the policy in force."""
        return bool(self.matches) if not self.negation_guard else bool(self.surviving)

    @property
    def negation_suppressed(self) -> bool:
        """The guard changed this input's outcome -- exactly the discordant set for the ablation."""
        return self.matched_raw and not self.matched

    @property
    def urgency(self) -> Urgency:
        """Highest urgency among surviving matches; ``routine`` when nothing survives."""
        order: tuple[Urgency, ...] = ("routine", "non-urgent", "urgent", "emergency")
        effective = self.surviving if self.negation_guard else self.matches
        if not effective:
            return "routine"
        return max((m.urgency for m in effective), key=order.index)

    @property
    def doc_ids(self) -> tuple[str, ...]:
        """Corpus notes backing the surviving matches, deduplicated, in first-seen order."""
        seen: dict[str, None] = {}
        for match in self.surviving:
            seen.setdefault(match.doc_id, None)
        return tuple(seen)

    def action_text(self) -> str | None:
        """The action for the most urgent surviving match, or ``None`` if nothing survived."""
        effective = self.surviving
        if not effective:
            return None
        order: tuple[Urgency, ...] = ("routine", "non-urgent", "urgent", "emergency")
        return max(effective, key=lambda m: order.index(m.urgency)).action


class RedFlagTable:
    """The loaded rule table and the matcher over it."""

    #: Corpus category the backing notes live in, asserted at load so a renamed category cannot
    #: silently detach the rules from their explanations.
    DOC_CATEGORY: Category = "red_flag"

    def __init__(self, rules: Sequence[RedFlagRule], *, negation_guard: bool = True) -> None:
        if not rules:
            raise RedFlagError("the red-flag table is empty; refusing to run with no rules")
        self.rules = tuple(rules)
        self.negation_guard = negation_guard
        # Longest pattern first, so "chest pain" is preferred over a shorter overlapping phrase and
        # the recorded span is the most specific one that matched.
        #
        # No morphology in the matcher.  An earlier version appended `s?` to catch "chest pains";
        # that handles regular plurals and silently fails on "vomited blood", "threw up blood",
        # "face drooped" and every other inflection -- a point fix for one instance of a general
        # problem.  Inflected forms are enumerated in the YAML instead, audited pattern by pattern,
        # so what matches is exactly what a reviewer reads in the data file.  A stemmer would have
        # hidden the behaviour inside a library and made that audit unverifiable.
        self._compiled: tuple[tuple[RedFlagRule, str, re.Pattern[str]], ...] = tuple(
            (rule, pattern, re.compile(rf"\b{re.escape(pattern)}\b", re.IGNORECASE))
            for rule in self.rules
            for pattern in sorted(rule.patterns, key=len, reverse=True)
        )

    @classmethod
    def from_yaml(cls, path: Path, *, negation_guard: bool = True) -> RedFlagTable:
        """Load and validate ``data/red_flags.yaml``."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RedFlagError(f"cannot read the red-flag table at {path}: {exc}") from exc
        if not isinstance(raw, dict) or "entries" not in raw:
            raise RedFlagError(f"{path}: expected a mapping with an 'entries' key")

        rules = [RedFlagRule.model_validate(entry) for entry in raw["entries"]]
        seen: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                raise RedFlagError(f"{path}: duplicate rule id {rule.id!r}")
            seen.add(rule.id)
        return cls(rules, negation_guard=negation_guard)

    def assess(self, text: str) -> RedFlagAssessment:
        """Match ``text`` against every rule, applying the negation guard to each hit."""
        starts = _token_starts(text)
        matches: list[RedFlagMatch] = []
        claimed: list[tuple[int, int]] = []

        for rule, pattern, regex in self._compiled:
            for found in regex.finditer(text):
                span = (found.start(), found.end())
                if any(start <= span[0] and span[1] <= end for start, end in claimed):
                    continue  # a longer phrase of some rule already covers this span
                claimed.append(span)
                matches.append(
                    RedFlagMatch(
                        rule_id=rule.id,
                        doc_id=rule.doc_id,
                        pattern=pattern,
                        urgency=rule.urgency,
                        body_system=rule.body_system,
                        action=rule.action,
                        start=span[0],
                        end=span[1],
                        negated_by=_negation_cue_before(text, span[0], starts),
                    )
                )

        matches.sort(key=lambda m: (m.start, m.rule_id))
        return RedFlagAssessment(matches=tuple(matches), negation_guard=self.negation_guard)

    def rule(self, rule_id: str) -> RedFlagRule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(f"unknown red-flag rule {rule_id!r}")

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self) -> Iterator[RedFlagRule]:
        return iter(self.rules)


def _normalize_token(word: str) -> str:
    """Fold contracted negations onto ``not`` so one cue covers every contraction.

    "I don't have chest pain" is probably the most common negated phrasing in real user input, and
    enumerating a cue per verb would mean a list that is wrong the first time someone writes
    "hadn't". Normalizing here means the existing ``not`` cue and the existing three-token window
    both apply unchanged.
    """
    if _CONTRACTED_NEGATION_RE.match(word) or word in _APOSTROPHE_FREE_NEGATIONS:
        return "not"
    return word


def _token_starts(text: str) -> list[tuple[int, str]]:
    """Word tokens as ``(start_offset, normalized_lowercased_word)``, in order."""
    return [(m.start(), _normalize_token(m.group())) for m in _WORD_RE.finditer(text.lower())]


def _negation_cue_before(
    text: str, match_start: int, starts: Sequence[tuple[int, str]]
) -> str | None:
    """Return the negation cue within ``NEGATION_WINDOW`` tokens before ``match_start``.

    Sentence boundaries end the window.  "I have chest pain. No fever." must not have its chest
    pain suppressed by the "No" that opens the following sentence.
    """
    preceding = [(offset, word) for offset, word in starts if offset < match_start]
    for offset, word in reversed(preceding[-NEGATION_WINDOW:]):
        if any(stop in text[offset:match_start] for stop in (".", "!", "?", ";")):
            break
        if word in NEGATION_CUES:
            return word
    return None
