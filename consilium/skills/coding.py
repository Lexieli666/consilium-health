"""Skill: ``lookup_disease_code`` -- the ICD-10 code and chapter for a condition.

This is the skill the hybrid-retrieval argument in docs/DESIGN.md rests on.  A dense retriever
embeds ``E11.9`` as a near-meaningless token; BM25 finds it exactly, because the tokenizer is
specified to keep code-like tokens intact.  Filtering to the ``coding`` category on top of that
keeps condition prose -- which also says "type 2 diabetes" repeatedly -- out of the pool.

The skill does not stop at returning passages.  It extracts the code strings it can see and reports
them separately, because "which code" is the question and making the caller re-read the passage to
find it is where a code gets misread.  **Extraction is a regex over retrieved text, not a lookup
table**: a second table mapping conditions to codes would be a copy of the corpus that could drift
from it, and the corpus is the thing under measurement.  The consequence is that the codes reported
are exactly the codes the retrieved notes contain, which is the property a reviewer can check.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from consilium.skills.base import (
    Passage,
    SkillContext,
    SkillResult,
    doc_ids,
    passages,
    require_retriever,
    skill,
)

NAME = "lookup_disease_code"

#: ICD-10 codes as they are written in the corpus: a letter, two digits, optionally a decimal point
#: and up to four further characters.  ``U`` is excluded because it is reserved for provisional
#: WHO assignments and is not part of the clinical modification these notes describe.
#:
#: Bounded by ``\b`` on both sides so that "A1c" and "COVID19" cannot match.  The pattern is
#: deliberately loose about what follows the decimal point -- ICD-10-CM extensions mix digits and
#: letters -- and deliberately strict about what precedes it, because the false positive to avoid is
#: an ordinary word being reported as a billable code.
_CODE_RE = re.compile(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?)\b")

#: How much text either side of a code is quoted as its context.
_CONTEXT_CHARS = 120


class LookupDiseaseCodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(
        min_length=1,
        description="The condition, code or code root to look up, e.g. 'type 2 diabetes' or 'E11'.",
    )


class CodeMention(BaseModel):
    """One code string as it appears in one retrieved passage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    doc_id: str
    title: str
    context: str


class LookupDiseaseCodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    condition: str
    codes: list[CodeMention]
    passages: list[Passage]


@skill(
    name=NAME,
    description=(
        "Look up the ICD-10 code, code root or chapter for a condition, and the coding conventions "
        "that govern choosing it. Also accepts a code and returns what it is used for."
    ),
    category="coding",
)
def lookup_disease_code(args: LookupDiseaseCodeArgs, ctx: SkillContext) -> SkillResult:
    retriever = require_retriever(ctx, NAME)
    hits = passages(
        retriever.search(args.condition, skill=NAME, category="coding", tracer=ctx.tracer)
    )
    return SkillResult.success(
        NAME,
        LookupDiseaseCodeResult(condition=args.condition, codes=extract_codes(hits), passages=hits),
        sources=doc_ids(hits),
    )


def extract_codes(hits: list[Passage]) -> list[CodeMention]:
    """Every distinct code in the retrieved passages, in the order the passages rank.

    Deduplicated on ``(code, doc_id)`` rather than on ``code`` alone: the same code appearing in the
    chapter map and in the per-condition note is two different pieces of evidence, and collapsing
    them would hide which note the caller should cite.
    """
    seen: set[tuple[str, str]] = set()
    mentions: list[CodeMention] = []
    for hit in hits:
        for match in _CODE_RE.finditer(hit.text):
            key = (match.group(1), hit.doc_id)
            if key in seen:
                continue
            seen.add(key)
            start = max(0, match.start() - _CONTEXT_CHARS)
            end = min(len(hit.text), match.end() + _CONTEXT_CHARS)
            mentions.append(
                CodeMention(
                    code=match.group(1),
                    doc_id=hit.doc_id,
                    title=hit.title,
                    context=" ".join(hit.text[start:end].split()),
                )
            )
    return mentions
