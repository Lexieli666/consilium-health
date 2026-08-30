"""Phase 10: turning a measured run into evidence a reviewer can check.

``eval/run.py`` writes ``eval/results/<timestamp>/{summary.json, report.md, runs/}``, and
``.gitignore`` admits exactly one path under ``eval/results/``: ``published/``.  This module is the
transform between the two, and it exists rather than a ``cp -r`` for three reasons.

**``summary.json`` and ``report.md`` are copied byte for byte and never regenerated.**  Re-rendering
a report from a summary at publication time would let a later change to ``eval/report.py`` silently
restate what a past run measured.  :func:`publish_run` copies the bytes and records their digest;
:func:`verify_manifest` recomputes it.  A published number is then pinned to the file that produced
it, not to the code that could produce it again.

**One session becomes one file.**  The runner writes ``runs/<session_id>/<turn_index>.jsonl``, which
is right for appending during a turn and wrong for reading afterwards: a reviewer chasing one golden
item should not have to know that a session is a directory.  The published layout is
``traces/<session_id>.json`` -- the whole session, its turns in order, every event verbatim -- which
is the path ``../human-annotation/phase10-failure-cases/TEMPLATE.md`` cites and the only thing a
failure-case write-up has to link.  Nothing is dropped, summarized or reordered on the way: the
events are the same objects, and ``turn_index`` survives as a field rather than as a filename.

**The judge's calls were never traced, and the A3 close-out needs their volume.**  ``Judge`` talks
to the provider directly, so no ``llm_call`` event exists for a judge call and ``--max-cost`` says
so wherever it publishes a figure.  :func:`judge_volume` reconstructs what those calls contained
from committed artifacts only -- the published traces, the corpus, the golden set, the two judge
prompt files -- by rebuilding the exact prompt strings ``eval/run.py`` would have sent.  That is a
reconstruction, not a measurement, and ``docs/EVALUATION.md`` §5.3 labels it as one.  It ships as
code rather than as arithmetic in a document so that the number in the document can be recomputed
by a reviewer who has only this repository.

The characters-per-token constant the reconstruction needs is measured, not assumed:
:func:`calibrate` reads the 150 ``baseline_llm`` turns, whose prompt is the one prompt in the sweep
that is reconstructible in full (one system prompt, one user message, no tools, no history), and
divides their characters by the ``prompt_tokens`` the provider actually charged for them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from consilium.agents.consultation import ConsultationAgent
from consilium.config import ABLATION_PRESETS
from consilium.retrieval.corpus import Document, load_corpus
from consilium.router.planner import FALLBACK_OBJECTIVE
from eval.items import GoldenItem, MultiturnConversation, load_golden, load_multiturn

# `_system_block` is imported rather than reimplemented: the judge's prompt is the text below the
# `---` separator, and a second copy of that rule here would be free to drift from the one the judge
# actually used, which would make this reconstruction wrong in the direction nobody would check.
from eval.judge import (
    FAITHFULNESS_PROMPT,
    MULTITURN_PROMPT,
    _system_block,
    load_prompt,
    numbered_sources,
)

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_DIR = ROOT / "eval" / "results" / "published"
CORPUS_DIR = ROOT / "data" / "corpus"
GOLDEN_PATH = ROOT / "eval" / "data" / "golden.jsonl"
MULTITURN_PATH = ROOT / "eval" / "data" / "multiturn.jsonl"

#: Copied byte for byte, never re-rendered.  See the module docstring.
VERBATIM_FILES: tuple[str, ...] = ("summary.json", "report.md")

TRACES_DIRNAME = "traces"
MANIFEST_FILENAME = "MANIFEST.json"

#: The configuration whose prompt is reconstructible in full, and therefore the one the
#: characters-per-token calibration is read from.
CALIBRATION_CONFIG = "baseline_llm"

#: The slice size the A3 worksheet prescribed: ``--limit 10`` per configuration.
DEFAULT_SLICE = 10


class PublishError(RuntimeError):
    """Raised when a run cannot be published or a published tree does not verify."""


# --------------------------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------------------------


def read_session(session_dir: Path) -> dict[str, Any]:
    """One session directory of ``<turn_index>.jsonl`` files as one JSON object.

    Turns are ordered by their **integer** index, not by filename: ``10.jsonl`` sorts before
    ``2.jsonl`` as a string, and a conversation whose turns were reordered on publication would be
    unreadable in exactly the multi-turn cases the reordering matters for.
    """
    turns: list[dict[str, Any]] = []
    for path in sorted(session_dir.glob("*.jsonl"), key=lambda p: int(p.stem)):
        events = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        turns.append({"turn_index": int(path.stem), "events": events})
    return {"session_id": session_dir.name, "turns": turns}


def sha256_file(path: Path) -> str:
    """The digest of one published file, read in binary so a line ending cannot change it."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_files(out_dir: Path) -> list[Path]:
    """Every published file the manifest covers, in a stable order.

    The manifest itself is excluded -- a file cannot carry its own digest -- and so is the
    directory's ``README.md``, which describes the layout rather than being evidence.
    """
    skip = {MANIFEST_FILENAME, "README.md"}
    return sorted(
        (path for path in out_dir.rglob("*") if path.is_file() and path.name not in skip),
        key=lambda path: path.relative_to(out_dir).as_posix(),
    )


def build_manifest(out_dir: Path) -> dict[str, Any]:
    """Digests of every published file, plus the counts a reader would otherwise have to take."""
    digests = {
        path.relative_to(out_dir).as_posix(): sha256_file(path) for path in manifest_files(out_dir)
    }
    traces = [name for name in digests if name.startswith(f"{TRACES_DIRNAME}/")]
    return {
        "algorithm": "sha256",
        "n_files": len(digests),
        "n_traces": len(traces),
        "files": digests,
    }


def verify_manifest(out_dir: Path) -> list[str]:
    """Recompute the manifest and report every disagreement.  Empty means the tree is intact."""
    manifest_path = out_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise PublishError(f"no manifest at {manifest_path}")
    recorded: dict[str, str] = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    current = {
        path.relative_to(out_dir).as_posix(): sha256_file(path) for path in manifest_files(out_dir)
    }
    problems = [f"missing: {name}" for name in sorted(set(recorded) - set(current))]
    problems += [f"unrecorded: {name}" for name in sorted(set(current) - set(recorded))]
    problems += [
        f"changed: {name}"
        for name in sorted(set(recorded) & set(current))
        if recorded[name] != current[name]
    ]
    return problems


def publish_run(run_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Copy one timestamped run into the published layout and write its manifest."""
    runs_dir = run_dir / "runs"
    if not runs_dir.is_dir():
        raise PublishError(f"{run_dir} has no runs/ directory; it is not a completed run")
    for name in VERBATIM_FILES:
        if not (run_dir / name).is_file():
            raise PublishError(f"{run_dir} has no {name}")

    traces_dir = out_dir / TRACES_DIRNAME
    if traces_dir.exists():
        shutil.rmtree(traces_dir)
    traces_dir.mkdir(parents=True)
    for name in VERBATIM_FILES:
        shutil.copyfile(run_dir / name, out_dir / name)

    sessions = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    for session_dir in sessions:
        payload = read_session(session_dir)
        (traces_dir / f"{session_dir.name}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    manifest = build_manifest(out_dir)
    (out_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


# --------------------------------------------------------------------------------------------
# The judge's untraced volume
# --------------------------------------------------------------------------------------------


def load_traces(traces_dir: Path) -> dict[str, list[list[dict[str, Any]]]]:
    """``session_id -> [events of turn 0, events of turn 1, ...]`` from the published tree."""
    sessions: dict[str, list[list[dict[str, Any]]]] = {}
    for path in sorted(traces_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        sessions[str(payload["session_id"])] = [list(turn["events"]) for turn in payload["turns"]]
    return sessions


def _of_type(events: Iterable[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == kind]


def cited_doc_ids(events: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """The ``doc_id`` values one turn cited, deduplicated in first-seen order.

    This is ``TurnOutcome.sources``, rebuilt from the trace: the router unions the
    ``source_doc_ids`` of every skill result the turn's workers produced, and ``tool_call`` events
    carry exactly that field.  Precedence ordering is not reproduced because nothing here depends
    on it -- the judge is shown a set of documents and its prompt length is the same either way.
    """
    seen: dict[str, None] = {}
    for event in _of_type(events, "tool_call"):
        for doc_id in event.get("source_doc_ids") or ():
            seen.setdefault(str(doc_id), None)
    return tuple(seen)


@dataclass(frozen=True)
class Calibration:
    """Characters per prompt token, measured on the one reconstructible prompt in the sweep."""

    turns: int
    prompt_characters: int
    prompt_tokens: int

    @property
    def characters_per_token(self) -> float:
        return self.prompt_characters / self.prompt_tokens


def calibrate(
    sessions: dict[str, list[list[dict[str, Any]]]], items: Sequence[GoldenItem]
) -> Calibration:
    """Divide reconstructed prompt characters by the ``prompt_tokens`` the provider charged.

    ``baseline_llm`` runs with ``router="none"`` and ``max_tool_calls=0``, so its single call
    carries exactly two messages: the consultation agent's system prompt, and the fallback
    objective glued to the question the way ``BaseAgent.answer`` glues it.  There is no tool
    schema, no observation and no history to guess at, which is what makes this configuration and
    no other usable as a calibration.
    """
    characters = 0
    tokens = 0
    turns = 0
    for item in items:
        events = sessions.get(f"{CALIBRATION_CONFIG}-{item.id}")
        if not events:
            continue
        calls = _of_type(events[0], "llm_call")
        if len(calls) != 1:
            continue
        user = f"{FALLBACK_OBJECTIVE}\n\nUser's question: {item.question}"
        characters += len(ConsultationAgent.system_prompt) + len(user)
        tokens += int(calls[0]["prompt_tokens"])
        turns += 1
    if not tokens:
        raise PublishError(f"no {CALIBRATION_CONFIG} turns to calibrate against")
    return Calibration(turns=turns, prompt_characters=characters, prompt_tokens=tokens)


@dataclass(frozen=True)
class JudgeVolume:
    """The judge's reconstructed input: how many calls, and how many characters of prompt."""

    calls: int
    prompt_characters: int
    per_config: tuple[tuple[str, int, int], ...]

    def prompt_tokens(self, calibration: Calibration) -> int:
        return round(self.prompt_characters / calibration.characters_per_token)


def _faithfulness_prompt_length(
    system_length: int,
    *,
    question: str,
    answer: str,
    sources: Sequence[tuple[str, str]],
) -> int:
    user = (
        f"QUESTION:\n{question}\n\nSOURCES:\n{numbered_sources(sources) or '(none)'}"
        f"\n\nANSWER:\n{answer}"
    )
    return system_length + len(user)


def judge_volume(
    sessions: dict[str, list[list[dict[str, Any]]]],
    *,
    items: Sequence[GoldenItem],
    conversations: Sequence[MultiturnConversation],
    documents: dict[str, Document],
) -> JudgeVolume:
    """Rebuild every judge call the sweep made, and total the characters it sent.

    The call pattern is ``eval/run.py``'s and is reproduced rather than assumed: per configuration
    per item, one faithfulness call against what the turn cited (skipped when it cited nothing) and
    one against the golden set's ``relevant_doc_ids`` (skipped when the item has none); then one
    multi-turn call per annotated turn, at the ``full`` configuration only.  ``full_budget_6`` is
    scored but **never judged**, so it contributes nothing here -- which is exactly the kind of
    detail a sizing estimate made from call counts alone will miss.
    """
    faithfulness_system = len(_system_block(load_prompt(FAITHFULNESS_PROMPT)))
    multiturn_system = len(_system_block(load_prompt(MULTITURN_PROMPT)))
    per_config: list[tuple[str, int, int]] = []
    total_calls = 0
    total_characters = 0

    for config in ABLATION_PRESETS:
        calls = 0
        characters = 0
        for item in items:
            events = sessions.get(f"{config}-{item.id}")
            if not events:
                continue
            turn = next(iter(_of_type(events[0], "turn")), None)
            if turn is None:
                continue
            question, answer = str(turn["question"]), str(turn["answer"])
            retrieved = [
                (doc_id, documents[doc_id].body)
                for doc_id in cited_doc_ids(events[0])
                if doc_id in documents
            ]
            oracle = [
                (doc_id, documents[doc_id].body)
                for doc_id in item.relevant_doc_ids
                if doc_id in documents
            ]
            for sources in (retrieved, oracle):
                if not sources:
                    continue
                characters += _faithfulness_prompt_length(
                    faithfulness_system, question=question, answer=answer, sources=sources
                )
                calls += 1
        per_config.append((config, calls, characters))
        total_calls += calls
        total_characters += characters

    calls = 0
    characters = 0
    for conversation in conversations:
        events = sessions.get(f"full-mt-{conversation.id}")
        if not events:
            continue
        answers = [
            str(turn["answer"])
            for turn in (next(iter(_of_type(one, "turn")), None) for one in events)
            if turn is not None
        ]
        for index, turn_label in enumerate(conversation.turns):
            if turn_label.depends_on_turn is None or not turn_label.expected_referent:
                continue
            if index >= len(answers):
                continue
            history = "\n".join(
                f"[{position}] {earlier.question}"
                for position, earlier in enumerate(conversation.turns[:index])
            )
            numbers = ", ".join(str(position) for position in turn_label.depends_on_turn)
            user = (
                f"CONVERSATION:\n{history}\n\nQUESTION:\n{turn_label.question}\n\n"
                f"REFERENT TURNS:\n{numbers or '(none)'}\n\n"
                f"REFERENT:\n{turn_label.expected_referent}\n\nANSWER:\n{answers[index]}"
            )
            characters += multiturn_system + len(user)
            calls += 1
    per_config.append(("full (multi-turn)", calls, characters))
    total_calls += calls
    total_characters += characters

    return JudgeVolume(
        calls=total_calls,
        prompt_characters=total_characters,
        per_config=tuple(per_config),
    )


@dataclass(frozen=True)
class Rates:
    """Dollars per token, recovered from the published run rather than read off a vendor page.

    ``eval/pricing.yaml`` ships empty on purpose and the operator's filled-in copy is not committed,
    so ``summary.json`` records only that two models were priced -- not what they were priced at.
    The rates are still recoverable: each configuration publishes prompt tokens per turn, completion
    tokens per turn and cost per turn, which is one linear equation per configuration in two
    unknowns.  Two configurations solve it and the rest check it, so a solution that reproduces
    every published cost to the cent is the rate card the run used.
    """

    input_per_token: float
    output_per_token: float
    max_residual: float

    def cost(self, prompt_tokens: float, completion_tokens: float) -> float:
        return prompt_tokens * self.input_per_token + completion_tokens * self.output_per_token


def recover_rates(summary: dict[str, Any]) -> Rates:
    """Solve for the two token rates from the published per-configuration costs."""
    rows = [
        (
            float(result["usage"]["prompt_tokens_per_turn"]),
            float(result["usage"]["completion_tokens_per_turn"]),
            float(result["usage"]["cost_per_turn_usd"]),
        )
        for result in summary["results"]
        if result["usage"]["cost_per_turn_usd"] is not None
    ]
    if len(rows) < 2:
        raise PublishError("fewer than two priced configurations; the rates cannot be recovered")
    (p1, c1, y1), (p2, c2, y2) = rows[0], rows[-1]
    determinant = p1 * c2 - p2 * c1
    if not determinant:
        raise PublishError("the priced configurations are collinear; the rates cannot be recovered")
    rate_in = (y1 * c2 - y2 * c1) / determinant
    rate_out = (p1 * y2 - p2 * y1) / determinant
    residual = max(abs(p * rate_in + c * rate_out - y) for p, c, y in rows)
    return Rates(input_per_token=rate_in, output_per_token=rate_out, max_residual=residual)


def turn_tokens(events: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """Prompt and completion tokens of one turn, summed over its ``llm_call`` events."""
    calls = _of_type(events, "llm_call")
    return (
        sum(int(call["prompt_tokens"]) for call in calls),
        sum(int(call["completion_tokens"]) for call in calls),
    )


@dataclass(frozen=True)
class SizingRow:
    """One configuration, priced over the first ``n`` items and over the whole golden set."""

    config: str
    slice_turns: int
    slice_cost: float
    full_turns: int
    full_cost: float

    @property
    def slice_projection(self) -> float:
        """What the slice would have predicted for this configuration over the full set."""
        return self.slice_cost / self.slice_turns * self.full_turns


def sizing_replay(
    sessions: dict[str, list[list[dict[str, Any]]]],
    *,
    items: Sequence[GoldenItem],
    rates: Rates,
    slice_size: int,
) -> list[SizingRow]:
    """Re-run the A3 sizing extrapolation against the published traces.

    ``--limit N`` takes the **first** N items of the golden set, and the golden set is written in
    category blocks, so a ten-item sizing slice is ten ``general_health`` questions.  This replays
    that slice out of the published run and prices it beside the full 150, which is the only way
    left to ask what the sizing could have seen: the sizing runs were a separate, cheaper sweep and
    their traces were not retained.
    """
    rows: list[SizingRow] = []
    head = {item.id for item in items[:slice_size]}
    for config in ABLATION_PRESETS:
        slice_cost = full_cost = 0.0
        slice_turns = full_turns = 0
        for item in items:
            events = sessions.get(f"{config}-{item.id}")
            if not events or not _of_type(events[0], "turn"):
                continue
            prompt, completion = turn_tokens(events[0])
            cost = rates.cost(prompt, completion)
            full_cost += cost
            full_turns += 1
            if item.id in head:
                slice_cost += cost
                slice_turns += 1
        # A configuration with no turns in the tree contributed nothing to the run and cannot be
        # extrapolated from; a row for it would divide by zero to say so.
        if slice_turns and full_turns:
            rows.append(
                SizingRow(
                    config=config,
                    slice_turns=slice_turns,
                    slice_cost=slice_cost,
                    full_turns=full_turns,
                    full_cost=full_cost,
                )
            )
    if not rows:
        raise PublishError("no configuration in the published tree has both a slice and a full set")
    return rows


def reconstruct(out_dir: Path = PUBLISHED_DIR) -> tuple[JudgeVolume, Calibration]:
    """The published tree's judge volume and the calibration that turns it into tokens."""
    sessions = load_traces(out_dir / TRACES_DIRNAME)
    items = load_golden(GOLDEN_PATH)
    conversations = load_multiturn(MULTITURN_PATH)
    documents = {document.doc_id: document for document in load_corpus(CORPUS_DIR)}
    return (
        judge_volume(sessions, items=items, conversations=conversations, documents=documents),
        calibrate(sessions, items),
    )


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.publish",
        description="Publish a measured run, verify a published one, or reconstruct the "
        "judge's untraced input volume.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--from", dest="source", type=Path, help="the eval/results/<timestamp> to publish"
    )
    group.add_argument("--verify", action="store_true", help="recompute the published manifest")
    group.add_argument(
        "--judge-volume",
        action="store_true",
        help="reconstruct the judge's prompt volume from the published traces",
    )
    group.add_argument(
        "--sizing-replay",
        type=int,
        metavar="N",
        nargs="?",
        const=DEFAULT_SLICE,
        help="price the first N items against the whole set, as the A3 sizing did",
    )
    parser.add_argument("--out", type=Path, default=PUBLISHED_DIR, help="the published directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.source is not None:
        manifest = publish_run(args.source, args.out)
        print(
            f"published {args.source} to {args.out}: "
            f"{manifest['n_traces']} trace(s), {manifest['n_files']} file(s)"
        )
        return 0
    if args.verify:
        problems = verify_manifest(args.out)
        for problem in problems:
            print(problem, file=sys.stderr)
        print("manifest verified" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    if args.sizing_replay is not None:
        sessions = load_traces(args.out / TRACES_DIRNAME)
        items = load_golden(GOLDEN_PATH)
        summary = json.loads((args.out / "summary.json").read_text(encoding="utf-8"))
        rates = recover_rates(summary)
        print(
            f"rates recovered from summary.json: ${rates.input_per_token * 1e6:.4f}/M input, "
            f"${rates.output_per_token * 1e6:.4f}/M output "
            f"(largest residual ${rates.max_residual:.2e} per turn)"
        )
        rows = sizing_replay(sessions, items=items, rates=rates, slice_size=args.sizing_replay)
        projected = sum(row.slice_projection for row in rows)
        measured = sum(row.full_cost for row in rows)
        for row in rows:
            print(
                f"  {row.config:18} slice n={row.slice_turns:3d} "
                f"${row.slice_cost / row.slice_turns:.6f}/turn -> ${row.slice_projection:.4f}; "
                f"measured n={row.full_turns:3d} ${row.full_cost:.4f}"
            )
        print(
            f"ablation projected from the first {args.sizing_replay} item(s): ${projected:.4f}; "
            f"measured ${measured:.4f} ({measured / projected:.2f}x)"
        )
        return 0

    volume, calibration = reconstruct(args.out)
    print(
        f"calibration: {calibration.prompt_characters} characters over "
        f"{calibration.turns} {CALIBRATION_CONFIG} turns charged "
        f"{calibration.prompt_tokens} prompt tokens "
        f"= {calibration.characters_per_token:.3f} characters per token"
    )
    for name, calls, characters in volume.per_config:
        print(f"  {name:20} {calls:5d} call(s)  {characters:12,d} characters")
    print(
        f"judge total: {volume.calls} call(s), {volume.prompt_characters:,} characters, "
        f"~{volume.prompt_tokens(calibration):,} input tokens (untraced, reconstructed)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
