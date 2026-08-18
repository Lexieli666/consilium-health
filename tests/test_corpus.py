"""The corpus conventions frozen in CLAUDE.md section 7 are asserted here rather than upheld by
hand.

Two of them are written down as tests rather than as conventions on purpose.  The disclaimer
blockquote is *required* by the ingest loader, so a note missing it is an ingest error rather than
a style lapse; and the US-spelling rule is a retrieval decision, because BM25 is lexical and a note
that says only "oesophageal" cannot be retrieved by a query saying "esophageal".  A convention that
78 files obey because someone checked each one is a convention that breaks on file 79.

Nothing here touches the network, an API key, or a model download: the corpus is a directory of
Markdown files, and every assertion below is a string operation over it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, NamedTuple

import pytest

from consilium.retrieval.corpus import DISCLAIMER, FRONT_MATTER_KEYS, load_corpus
from consilium.retrieval.types import CATEGORIES

CORPUS_DIR: Final = Path(__file__).resolve().parents[1] / "data" / "corpus"

# FRONT_MATTER_KEYS and DISCLAIMER are imported from the loader rather than restated here.  Both
# are enforced at ingest time -- a malformed note is a CorpusError, not a lint failure -- and a
# second copy in the test file would be free to drift from the copy that actually rejects notes,
# which would leave the lint passing while ingestion failed.

#: 2,700-3,500 characters of body yields 3-4 chunks at the 800-1,000 character chunk size.  A
#: one-chunk corpus would never exercise the per-doc_id dedup step in RRF fusion.
MIN_BODY_CHARS: Final = 2_700
MAX_BODY_CHARS: Final = 3_500

#: Front matter, exactly one blank line, disclaimer, then the body.
_NOTE_RE: Final = re.compile(r"\A---\n(?P<front>.*?)\n---\n\n(?P<rest>.*)\Z", re.DOTALL)
_FRONT_MATTER_LINE_RE: Final = re.compile(r"\A(?P<key>[a-z_]+): (?P<value>.+)\Z")

# --------------------------------------------------------------------------------------------
# doc_id patterns, frozen per category in docs/CORPUS.md.
# --------------------------------------------------------------------------------------------

#: Lowercase, hyphen-separated, no underscores, numerals as digits.  Applies to every category.
_SLUG: Final = r"[a-z0-9]+(?:-[a-z0-9]+)*"

#: An ICD-10-CM category root is a letter followed by two digits, and it is undecimalized in a
#: doc_id (`e11`, never `e11.9`) because a dot in a filename stem reads as an extension.
_CODE_ROOT: Final = r"[a-z]\d{2}"

LIFESTYLE_DOMAINS: Final = ("diet", "activity", "sleep", "adherence")

DOC_ID_PATTERNS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "condition": (re.compile(rf"\Acondition-{_SLUG}\Z"),),
    "guideline": (re.compile(rf"\Aguideline-{_SLUG}-{_SLUG}\Z"),),
    "lifestyle": (re.compile(rf"\Alifestyle-{_SLUG}-(?:{'|'.join(LIFESTYLE_DOMAINS)})\Z"),),
    "red_flag": (re.compile(rf"\Ared-flag-{_SLUG}\Z"),),
    "coding": (
        # Chapter map: two digits, always, so `chapter-9` fails rather than passing as a
        # classification-level note.
        re.compile(rf"\Acoding-icd10-chapter-\d{{2}}-{_SLUG}\Z"),
        # Per-condition code selection, ending in the undecimalized code root.
        re.compile(rf"\Acoding-{_SLUG}-{_CODE_ROOT}\Z"),
        # Notes about the classification itself rather than a chapter or a condition.
        re.compile(rf"\Acoding-icd10-(?!chapter-){_SLUG}\Z"),
    ),
}

# --------------------------------------------------------------------------------------------
# Spelling.  US forms throughout, with a documented set of British alternate-spelling anchors.
# --------------------------------------------------------------------------------------------

#: The allowlist from docs/CORPUS.md.  Each of these may appear once, in parentheses, at first use
#: in a note where the term is central, so that either spelling retrieves the document.
SPELLING_ALLOWLIST: Final = frozenset(
    {
        "oesophageal",
        "oesophagus",
        "oesophagitis",
        "haemoglobin",
        "anaemia",
        "apnoea",
        "generalised",
        "gord",
        "paediatric",
        "oedema",
        "diarrhoea",
        "haemorrhage",
    }
)

#: Substrings that occur in British spellings and in no US spelling, so a word containing one is
#: British without further analysis.  Each entry was checked against the reverse direction: none of
#: them is a substring of a US form (`fulfil`, for instance, is deliberately absent, because it is a
#: substring of the US `fulfillment`).
_BRITISH_SUBSTRINGS: Final = (
    # -ae-/-oe- digraphs, the class that dominates clinical prose
    "aemi",
    "haem",
    "anaesth",
    "paed",
    "gynaec",
    "rhoea",
    "pnoea",
    "coeliac",
    "foet",
    "caesar",
    "manoeuvr",
    # -our
    "colour",
    "behaviour",
    "tumour",
    "humour",
    "favour",
    "labour",
    "odour",
    "vapour",
    "honour",
    "rumour",
    "neighbour",
    "flavour",
    "vigour",
    "endeavour",
    "harbour",
    "armour",
    "saviour",
    # -re
    "centre",
    "fibre",
    "litre",
    "metre",
    "calibre",
    "theatre",
    "spectre",
    "sombre",
    "lustre",
    # miscellaneous
    "sulph",
    "aluminium",
    "practise",
    "licence",
    "defence",
    "offence",
    "programme",
    "ageing",
    "judgement",
    "enrolment",
    "skilful",
    "storey",
    "mould",
    "grey",
)

#: British forms that are only British at the *start* of a word.  A US compound built from a root
#: ending in "o" and one beginning with "e" reproduces the "oe" digraph by accident:
#: `gastro` + `esophageal` is `gastroesophageal`, and `angio` + `edema` is `angioedema`, both of
#: which are the US spellings and both of which contain "oesophag" and "oedem" as substrings.
#: British compounds hyphenate ("gastro-oesophageal"), and the word splitter breaks on the hyphen,
#: so the British form still reaches this check as a word starting with "oe".
_BRITISH_WORD_PREFIXES: Final = (
    "oesophag",
    "oedem",
    "oestro",
    "oestra",
)

#: Words ending -ise/-ised/-ises/-ising/-isation or -yse/-ysed/-yses/-ysing are presumed British.
#: The rule is inverted deliberately: listing the British forms would miss the one nobody thought
#: of, whereas the set of words spelled -ise in US English is closed and short.
_ISE_RE: Final = re.compile(r"\A[a-z]+(?:is|ys)(?:e|es|ed|ing|ation|ations)\Z")

_US_ISE_BASES: Final = (
    "advertise",
    "advise",
    "appraise",
    "apprise",
    "arise",
    "bruise",
    "chastise",
    "circumcise",
    "comprise",
    "compromise",
    "concise",
    "cruise",
    "demise",
    "despise",
    "devise",
    "disguise",
    "enterprise",
    "excise",
    "exercise",
    "expertise",
    "franchise",
    "guise",
    "improvise",
    "incise",
    "liaise",
    "malaise",
    "merchandise",
    "noise",
    "paradise",
    "poise",
    "praise",
    "precise",
    "premise",
    "promise",
    "raise",
    "revise",
    "rise",
    "supervise",
    "surmise",
    "surprise",
    "televise",
    "treatise",
    "wise",
)

#: Matched as *endings* rather than whole words, so that `immunocompromised` and `stepwise` are
#: exempted by `compromise` and `wise` without either being listed separately.
_US_ISE_ENDINGS: Final = tuple(
    form for base in _US_ISE_BASES for form in (base, f"{base}s", f"{base}d", f"{base[:-1]}ing")
)

_WORD_RE: Final = re.compile(r"[A-Za-z]+")


class Note(NamedTuple):
    """One parsed corpus file."""

    path: Path
    stem: str
    front_matter: tuple[tuple[str, str], ...]
    body: str

    @property
    def values(self) -> dict[str, str]:
        return dict(self.front_matter)


def _parse(path: Path) -> Note:
    """Split a note into front matter and body, asserting the structural contract as it goes."""
    text = path.read_text(encoding="utf-8")
    match = _NOTE_RE.match(text)
    assert match is not None, (
        f"{path.name}: expected '---' front matter, one blank line, then the disclaimer"
    )

    front_matter: list[tuple[str, str]] = []
    for line in match.group("front").split("\n"):
        line_match = _FRONT_MATTER_LINE_RE.match(line)
        assert line_match is not None, f"{path.name}: unparsable front-matter line {line!r}"
        front_matter.append((line_match.group("key"), line_match.group("value").strip().strip('"')))

    rest = match.group("rest")
    assert rest.startswith(DISCLAIMER), f"{path.name}: disclaimer missing or not byte-identical"

    return Note(
        path=path,
        stem=path.stem,
        front_matter=tuple(front_matter),
        body=rest[len(DISCLAIMER) :].strip(),
    )


CORPUS_FILES: Final = sorted(CORPUS_DIR.glob("*.md"))


def _normalize(text: str) -> str:
    """Lowercase, and collapse hyphens and line breaks to single spaces.

    Corpus notes wrap at 100 characters, so a two-word phrase can straddle a newline; and the same
    term is written hyphenated in one note and open in another.  Comparing raw substrings would
    make both an accidental failure.
    """
    return re.sub(r"[\s-]+", " ", text.lower())


def _is_british(word: str) -> bool:
    """Three independent rules; a lowercase word matching any one of them is a British form."""
    has_british_substring = any(fragment in word for fragment in _BRITISH_SUBSTRINGS)
    has_british_prefix = word.startswith(_BRITISH_WORD_PREFIXES)
    is_british_ise = bool(_ISE_RE.match(word)) and not word.endswith(_US_ISE_ENDINGS)
    return has_british_substring or has_british_prefix or is_british_ise


def _british_words(text: str) -> list[str]:
    """Return the British-spelled words in ``text`` that are not documented anchors."""
    return [
        word
        for word in (raw.lower() for raw in _WORD_RE.findall(text))
        if word not in SPELLING_ALLOWLIST and _is_british(word)
    ]


# --------------------------------------------------------------------------------------------
# The spelling detector's own tests.  A lint that cannot fail is decoration, and the anchoring
# rule below is subtle enough that someone will eventually try to simplify it away.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ischaemia of the myocardium",
        "haematuria",
        "leukaemia",
        "hypoglycaemia",
        "the tumour was small",
        "measured in millilitres",
        "randomised to treatment",
        "normalised over time",
        "behavioural therapy",
        "oestrogen",
        "the centre of the chest",
        "colour vision",
        "host defence",
        "anaesthetic",
        "coeliac disease",
        "clinical judgement",
    ],
)
def test_spelling_detector_flags_british_forms(text: str) -> None:
    """These are British forms with no entry in the allowlist, so the lint must reject them."""
    assert _british_words(text) != [], f"detector missed a British form in {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "gastroesophageal reflux disease",
        "angioedema of the lips",
        "the causative organism",
        "raised blood pressure",
        "exercise raises the heart rate",
        "advised to seek care",
        "otherwise well",
        "a stepwise approach",
        "immunocompromised adults",
        "bruises easily",
        "malaise and fatigue",
        "supervised exercise",
        "this surprises people",
        "realism about outcomes",
        "hemoglobin and edema",
        "a concise summary",
    ],
)
def test_spelling_detector_does_not_flag_us_forms(text: str) -> None:
    assert _british_words(text) == [], f"detector false-positived on {text!r}"


def test_documented_anchors_are_exempt() -> None:
    """The allowlist is what makes either spelling retrieve the document."""
    assert _british_words("gastro-oesophageal reflux (GORD)") == []
    assert _british_words("iron-deficiency anaemia") == []
    assert _british_words("obstructive sleep apnoea") == []


# --------------------------------------------------------------------------------------------
# Directory-level contract.
# --------------------------------------------------------------------------------------------


def test_corpus_directory_holds_only_ingestable_notes() -> None:
    """Every file is a document and its doc_id is its stem -- with no exceptions to carve out.

    No READMEs, no index files, no subdirectories.  Provenance lives in docs/CORPUS.md instead.
    """
    entries = sorted(CORPUS_DIR.iterdir())
    assert entries, f"{CORPUS_DIR} is empty"
    assert [entry.name for entry in entries if not entry.is_file()] == []
    assert [entry.name for entry in entries if entry.suffix != ".md"] == []


@pytest.mark.parametrize("category", CATEGORIES)
def test_every_category_has_documents(category: str) -> None:
    """An empty category leaves its skill with nothing to retrieve.

    ``lookup_disease_code`` filters retrieval to ``coding``; if no coding note exists, the skill
    returns nothing for every question and the condition-and-coding block of the golden set is
    unanswerable by construction.
    """
    notes = [note for note in map(_parse, CORPUS_FILES) if note.values["category"] == category]
    assert notes, f"category {category!r} has no corpus documents"


def test_corpus_size_is_within_the_specified_band() -> None:
    assert 60 <= len(CORPUS_FILES) <= 80, (
        f"corpus has {len(CORPUS_FILES)} notes; the brief specifies 60-80"
    )


@pytest.mark.parametrize(
    "path", [p for p in CORPUS_FILES if p.stem.startswith("condition-")], ids=lambda path: path.stem
)
def test_every_condition_is_named_in_a_coding_note(path: Path) -> None:
    """Coding and condition notes must be mutually retrievable, and BM25 is lexical.

    A coding note that says "ischemic heart disease" does not retrieve for "coronary artery
    disease", so naming the condition is not a stylistic nicety -- it is the difference between a
    condition-and-coding question being answerable and not. Hyphens and line breaks are normalized
    to spaces on both sides so that "iron-deficiency\nanemia" still matches the topic phrase.
    """
    topic = _normalize(path.stem.removeprefix("condition-").replace("-", " "))
    coding = _normalize(
        "\n".join(
            other.read_text(encoding="utf-8")
            for other in CORPUS_FILES
            if other.stem.startswith("coding-")
        )
    )
    assert topic in coding, f"{path.stem}: no coding note names {topic!r}"


# --------------------------------------------------------------------------------------------
# Per-note contract.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_front_matter_is_the_five_frozen_keys_in_order(path: Path) -> None:
    note = _parse(path)
    keys = tuple(key for key, _ in note.front_matter)
    assert keys == FRONT_MATTER_KEYS


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_doc_id_equals_the_filename_stem(path: Path) -> None:
    """The golden set labels doc_ids, so renaming a file is a re-labeling job, not a sed."""
    note = _parse(path)
    assert note.values["doc_id"] == note.stem


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_category_is_one_of_the_literal_values(path: Path) -> None:
    note = _parse(path)
    assert note.values["category"] in CATEGORIES


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_disclaimer_is_present_and_byte_identical(path: Path) -> None:
    """_parse asserts this; the test exists so the requirement is visible as a named check."""
    assert _parse(path).path == path


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_body_length_is_within_the_frozen_band(path: Path) -> None:
    note = _parse(path)
    assert MIN_BODY_CHARS <= len(note.body) <= MAX_BODY_CHARS, (
        f"{note.stem}: body is {len(note.body)} characters, band is "
        f"{MIN_BODY_CHARS}-{MAX_BODY_CHARS}"
    )


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_doc_id_matches_the_frozen_pattern_for_its_category(path: Path) -> None:
    note = _parse(path)
    category = note.values["category"]
    patterns = DOC_ID_PATTERNS[category]
    assert any(pattern.match(note.stem) for pattern in patterns), (
        f"{note.stem}: does not match any frozen doc_id pattern for category {category!r}"
    )


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_slug_rules_hold(path: Path) -> None:
    """Lowercase, hyphen-separated, no underscores, no dots."""
    note = _parse(path)
    assert re.fullmatch(_SLUG, note.stem), f"{note.stem}: violates the slug rules"


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_coding_code_roots_are_undecimalized(path: Path) -> None:
    """`coding-type-2-diabetes-e11`, never `...-e11.9`: a dot in a stem reads as an extension.

    Chapter maps and classification-level notes carry no code root and are exempt; a per-condition
    note is any coding note that is not one of those, and its final segment must be the root.
    """
    note = _parse(path)
    if note.values["category"] != "coding" or note.stem.startswith("coding-icd10-"):
        pytest.skip("not a per-condition coding note")
    assert re.fullmatch(_CODE_ROOT, note.stem.rsplit("-", 1)[-1]), (
        f"{note.stem}: a per-condition coding note ends in an undecimalized root such as 'e11'"
    )


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda path: path.stem)
def test_no_british_spelling_outside_the_allowlist(path: Path) -> None:
    """BM25 is lexical: a note that says only "oesophageal" is unreachable from "esophageal"."""
    offenders = _british_words(path.read_text(encoding="utf-8"))
    assert offenders == [], f"{path.stem}: British spelling outside the allowlist: {offenders}"


def test_the_loader_accepts_every_note_in_the_corpus() -> None:
    """The conventions above are asserted by this file and *enforced* by the loader.

    Both matter and they are not the same check.  This test is what ties them together: if a
    convention is tightened here but not in `consilium/retrieval/corpus.py`, the corpus keeps
    passing the lint and starts failing at ingest, which is the failure this project can least
    afford to discover in Phase 8.
    """
    documents = load_corpus(CORPUS_DIR)
    assert len(documents) == len(CORPUS_FILES)
    assert [document.doc_id for document in documents] == [path.stem for path in CORPUS_FILES]
    assert all(DISCLAIMER not in document.body for document in documents)
