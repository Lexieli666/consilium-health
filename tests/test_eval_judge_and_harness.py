"""The judge, its validation loop, and running one item through the production turn path."""

from __future__ import annotations

import csv
import dataclasses
import json
from collections import Counter
from pathlib import Path

import pytest

from consilium.config import RunConfig, Settings
from consilium.llm import MockProvider, ScriptedResponse
from consilium.retrieval.corpus import Document
from consilium.runtime import build_runtime
from eval.harness import ItemRun, run_conversation, run_golden_item, session_id_for
from eval.items import (
    ExpectedRoute,
    GoldenCategory,
    GoldenItem,
    MultiturnConversation,
    MultiturnTurn,
)
from eval.judge import (
    FAITHFULNESS_PROMPT,
    MULTITURN_PROMPT,
    SAMPLE_COLUMNS,
    Judge,
    JudgeError,
    SampleRow,
    agreement,
    load_prompt,
    numbered_sources,
    score_sample,
    write_sample,
)
from eval.metrics import turn_event
from eval.run import (
    SAMPLE_SEED,
    draw_human_sample,
    git_commit,
    load_pricing,
    sample_block,
    stratified,
)
from tests.stubs import FailingProvider, RecordingProvider

ROOT = Path(__file__).resolve().parents[1]


def _settings() -> Settings:
    return Settings(root_dir=ROOT, data_dir=ROOT / "data", corpus_dir=ROOT / "data" / "corpus")


def _plan(*agents: str) -> ScriptedResponse:
    return ScriptedResponse(
        content=json.dumps(
            {"subtasks": [{"agent": a, "objective": "o", "why": "w"} for a in agents]}
        )
    )


def _item(item_id: str = "g-001", category: GoldenCategory = "general_health") -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question="what is hypertension",
        category=category,
        expected_route=ExpectedRoute(mode="single", agents=("consultation",)),
        relevant_doc_ids=("condition-hypertension",),
        reference_answer="Persistently raised blood pressure.",
        red_flag=False,
        labeled=True,
    )


# --- judge prompts and parsing -------------------------------------------------------------------


@pytest.mark.parametrize("name", [FAITHFULNESS_PROMPT, MULTITURN_PROMPT])
def test_the_judge_prompts_are_versioned_files_on_disk(name: str) -> None:
    """A prompt change is a change to the measurement, and it belongs in a diff."""
    text = load_prompt(name)
    assert text.strip()
    assert name.endswith("_v1")
    assert "unvalidated" in text or "not measured" in text


def test_a_missing_prompt_is_an_error_naming_the_path() -> None:
    with pytest.raises(JudgeError, match="cannot read the judge prompt"):
        load_prompt("no_such_prompt")


async def test_the_faithfulness_judge_returns_a_claim_level_score() -> None:
    judge = Judge(
        MockProvider(
            [
                ScriptedResponse(
                    content='{"claims": [{"claim": "a", "verdict": "supported", "source": 1}],'
                    ' "supported": 3, "total": 4}'
                )
            ]
        )
    )

    verdict = await judge.faithfulness(
        question="q", answer="a", sources=[("condition-hypertension", "text")]
    )

    assert verdict is not None
    assert verdict.score == 0.75


async def test_an_answer_with_no_claims_is_excluded_rather_than_scored_zero() -> None:
    """An answer that is only a banner and a disclaimer has nothing to ground."""
    judge = Judge(
        MockProvider([ScriptedResponse(content='{"claims": [], "supported": 0, "total": 0}')])
    )

    verdict = await judge.faithfulness(question="q", answer="a", sources=[])

    assert verdict is not None
    assert verdict.score is None


async def test_an_unparseable_judge_reply_is_none_not_zero() -> None:
    """A flaky judge must not look like an ungrounded system."""
    judge = Judge(MockProvider([ScriptedResponse(content="I would rather not.")]))
    assert await judge.faithfulness(question="q", answer="a", sources=[]) is None


async def test_a_judge_outage_does_not_end_a_paid_sweep() -> None:
    judge = Judge(FailingProvider())
    assert await judge.faithfulness(question="q", answer="a", sources=[]) is None


async def test_the_multiturn_judge_rejects_a_verdict_outside_its_three() -> None:
    judge = Judge(MockProvider([ScriptedResponse(content='{"verdict": "maybe", "why": "x"}')]))
    assert (
        await judge.multiturn(
            conversation=["a"], question="q", referent="r", referent_turns=(0,), answer="x"
        )
        is None
    )


async def test_the_multiturn_judge_returns_its_verdict_and_reason() -> None:
    judge = Judge(
        MockProvider([ScriptedResponse(content='{"verdict": "resolved", "why": "about diet"}')])
    )

    verdict = await judge.multiturn(
        conversation=["I have high blood pressure"],
        question="what about diet?",
        referent="hypertension",
        referent_turns=(0,),
        answer="For high blood pressure, sodium reduction is described.",
    )

    assert verdict is not None
    assert verdict.verdict == "resolved"


async def test_the_judge_is_shown_every_referent_turn_and_a_numbered_transcript() -> None:
    """A turn may resolve against several earlier turns, and the judge has to see which.

    The labeller's referent text is one string even for four turns -- "father's acute confusion,
    urinary retention, possible fever, and living alone" -- so the indices are what tells the judge
    how many referents there are, and the numbered transcript is what lets it look them up. Without
    both, "resolve all of them" is an instruction the judge has no way to follow.
    """
    provider = RecordingProvider(content='{"verdict": "resolved", "why": "ok"}')
    judge = Judge(provider)

    await judge.multiturn(
        conversation=["I am 34 weeks pregnant.", "My hands have been swelling.", "And a headache."],
        question="Is that combination worth calling someone about?",
        referent="swollen hands; persistent headache",
        referent_turns=(1, 2),
        answer="Those two together during pregnancy warrant urgent assessment.",
    )

    sent = provider.messages[-1][-1].content or ""
    assert "REFERENT TURNS:\n1, 2" in sent
    assert "[0] I am 34 weeks pregnant." in sent
    assert "[2] And a headache." in sent


# --- judge validation ----------------------------------------------------------------------------


def test_the_sample_csv_leaves_the_two_human_columns_empty(tmp_path: Path) -> None:
    path = tmp_path / "judge_sample.csv"
    write_sample(
        path,
        [
            SampleRow(
                item_id="g-001",
                question="q",
                answer="a",
                retrieved_doc_ids="doc-a doc-b",
                sources_text="[1] (doc-a)\nbody a\n\n[2] (doc-b)\nbody b",
                judge_label="supported",
                judge_rationale="because",
            )
        ],
    )

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == SAMPLE_COLUMNS
        (row,) = list(reader)
    assert row["human_notes"] == ""
    assert row["human_label"] == ""
    assert row["retrieved_doc_ids"] == "doc-a doc-b"
    assert row["sources_text"].startswith("[1] (doc-a)\nbody a")


def test_the_sample_columns_are_the_sample_row_fields() -> None:
    """Two hand-kept lists would drift, and the drift would ship as an empty column.

    ``write_sample`` reads the columns off ``SAMPLE_COLUMNS`` by name, so a column with no field
    behind it fails at write time. This pins the other direction: a field nobody wrote into the
    header would be data collected and then silently dropped.
    """
    assert tuple(field.name for field in dataclasses.fields(SampleRow)) == SAMPLE_COLUMNS
    assert SAMPLE_COLUMNS[-1] == "human_label"
    assert SAMPLE_COLUMNS.index("sources_text") == SAMPLE_COLUMNS.index("retrieved_doc_ids") + 1


def test_the_sample_carries_the_evidence_block_the_judge_is_shown() -> None:
    """One formatter, so the column cannot drift from what produced the verdict beside it."""
    assert numbered_sources([("doc-a", "body a"), ("doc-b", "body b")]) == (
        "[1] (doc-a)\nbody a\n\n[2] (doc-b)\nbody b"
    )
    assert numbered_sources([]) == ""


def test_score_sample_reads_by_column_name_not_by_position(tmp_path: Path) -> None:
    """The layout gained two columns; the scorer reads `judge_label` and `human_label` by name."""
    path = tmp_path / "reordered.csv"
    columns = list(reversed(SAMPLE_COLUMNS))
    values = [("supported", "supported"), ("supported", "unsupported")]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for judge_label, human_label in values:
            writer.writerow(
                dict.fromkeys(columns, "x")
                | {"judge_label": judge_label, "human_label": human_label}
            )

    result = score_sample(path)

    assert result.n == 2
    assert result.raw_agreement == pytest.approx(0.5)


def test_agreement_reports_kappa_not_only_raw_agreement() -> None:
    """With a skewed label distribution two raters agree 90% by chance."""
    pairs = [("supported", "supported")] * 9 + [("supported", "unsupported")]

    result = agreement(pairs)

    assert result.raw_agreement == pytest.approx(0.9)
    assert result.cohens_kappa == pytest.approx(0.0)


def test_perfect_agreement_on_a_varied_sample_gives_kappa_one() -> None:
    pairs = [("supported", "supported")] * 5 + [("unsupported", "unsupported")] * 5
    result = agreement(pairs)
    assert result.raw_agreement == 1.0
    assert result.cohens_kappa == pytest.approx(1.0)


def test_unlabelled_rows_are_skipped_rather_than_counted_as_disagreements(tmp_path: Path) -> None:
    """Otherwise the agreement number depends on how far the labeller got."""
    path = tmp_path / "sample.csv"
    write_sample(
        path,
        [
            SampleRow("a", "q", "x", "", "s", "supported", "r", human_label="supported"),
            SampleRow("b", "q", "x", "", "s", "supported", "r"),
        ],
    )

    result = score_sample(path)

    assert result.n == 1
    assert result.raw_agreement == 1.0


def test_scoring_a_sample_nobody_labelled_says_so(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"
    write_sample(path, [SampleRow("a", "q", "x", "", "s", "supported", "r")])

    with pytest.raises(JudgeError, match="Fill in the last column"):
        score_sample(path)


def test_scoring_a_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(JudgeError, match="cannot read the judge sample"):
        score_sample(tmp_path / "absent.csv")


# --- drawing the human sample ---------------------------------------------------------------------

#: One item-id block per golden category, which is what `--human-sample` stratifies over.
_BLOCKS: dict[str, GoldenCategory] = {
    "g-gh": "general_health",
    "g-su": "symptom_urgency",
    "g-cc": "condition_coding",
    "g-ge": "guideline_evidence",
    "g-md": "multi_dimensional",
}

_SUPPORTED = '{"claims": [{"claim": "a", "verdict": "supported", "source": 1}], \
"supported": 2, "total": 2}'
_PARTLY_SUPPORTED = '{"claims": [{"claim": "a", "verdict": "unsupported"}], \
"supported": 1, "total": 2}'
_NO_CLAIMS = '{"claims": [], "supported": 0, "total": 0}'


def _judge_saying(payload: str, *, times: int = 60) -> Judge:
    return Judge(MockProvider([ScriptedResponse(content=payload) for _ in range(times)]))


async def _runs_across_every_block(
    tmp_path: Path, *, per_block: int = 6
) -> tuple[list[ItemRun], dict[str, Document]]:
    """`per_block` items in each of the five id blocks, all carrying one real turn outcome.

    The outcome comes from `run_golden_item` rather than being hand-built, so the rows the draw
    judges hold an answer and a source list the production path actually produced. Only the item
    identity varies across the list, because identity is the axis the stratification is about.
    """
    runtime = build_runtime(
        _settings(),
        provider=MockProvider([_plan("consultation"), ScriptedResponse(content="An answer.")]),
        embedder="hash",
        store="numpy",
    )
    template = await run_golden_item(runtime, _item("g-gh-000"), runs_dir=tmp_path, prefix="full")
    assert template.outcome is not None
    outcome = dataclasses.replace(
        template.outcome,
        answer="Blood pressure is persistently raised.",
        sources=("condition-hypertension",),
    )
    runs = [
        ItemRun(item=_item(f"{block}-{index:03d}", category), outcome=outcome)
        for block, category in _BLOCKS.items()
        for index in range(1, per_block + 1)
    ]
    return runs, dict(runtime.documents)


async def test_the_human_sample_is_stratified_over_the_five_id_blocks(tmp_path: Path) -> None:
    """`runs[:N]` over a set written in category blocks is one and a third categories."""
    runs, documents = await _runs_across_every_block(tmp_path)

    draw = await draw_human_sample(
        _judge_saying(_SUPPORTED),
        runs,
        documents,
        count=10,
        config="full",
    )

    assert draw.per_block == 2
    assert len(draw.rows) == 10
    assert Counter(sample_block(row.item_id) for row in draw.rows) == dict.fromkeys(_BLOCKS, 2)
    # The draw this replaces: the first ten runs in file order are two of the five blocks.
    assert len({sample_block(run.item.id) for run in runs[:10]}) == 2


async def test_the_same_seed_draws_the_same_rows(tmp_path: Path) -> None:
    """A sampling method a reviewer cannot re-run is not a stated sampling method."""
    runs, documents = await _runs_across_every_block(tmp_path)

    first = await draw_human_sample(
        _judge_saying(_SUPPORTED), runs, documents, count=10, config="full"
    )
    second = await draw_human_sample(
        _judge_saying(_SUPPORTED), runs, documents, count=10, config="full"
    )

    assert [row.item_id for row in first.rows] == [row.item_id for row in second.rows]
    assert first.seed == SAMPLE_SEED
    assert f"random.Random({SAMPLE_SEED})" in first.method()
    assert "stratified over 5 item-id blocks" in first.method()
    assert "no row needed replacing" in first.method()


async def test_every_shipped_row_carries_the_judge_label_and_the_judge_evidence(
    tmp_path: Path,
) -> None:
    """The defect this replaces: a CSV whose judge columns were empty could not be scored at all."""
    runs, documents = await _runs_across_every_block(tmp_path)

    draw = await draw_human_sample(
        _judge_saying(_SUPPORTED), runs, documents, count=5, config="full"
    )

    assert len(draw.rows) == 5
    assert all(row.judge_label == "supported" for row in draw.rows)
    assert all(row.judge_rationale for row in draw.rows)
    body = documents["condition-hypertension"].body
    expected = numbered_sources([("condition-hypertension", body)])
    assert all(row.sources_text == expected for row in draw.rows)
    assert all(row.retrieved_doc_ids == "condition-hypertension" for row in draw.rows)
    assert all(row.human_label == "" and row.human_notes == "" for row in draw.rows)


async def test_an_answer_the_judge_only_partly_supported_is_labelled_unsupported(
    tmp_path: Path,
) -> None:
    """The person is asked the same all-or-nothing question, so the judge column has two values."""
    runs, documents = await _runs_across_every_block(tmp_path)

    draw = await draw_human_sample(
        _judge_saying(_PARTLY_SUPPORTED),
        runs,
        documents,
        count=5,
        config="full",
    )

    assert {row.judge_label for row in draw.rows} == {"unsupported"}


async def test_a_row_the_judge_cannot_score_is_replaced_from_its_own_block(
    tmp_path: Path,
) -> None:
    """Both ways a row can come back unscorable, and neither may ship as a blank label.

    An unparseable judge reply and an answer with no factual claim in it are different failures,
    but the consequence is the same: nothing for the human's label to agree or disagree with. The
    replacement is drawn from the same block so the strata stay equal rather than being levelled
    by whichever block happened to survive.
    """
    runs, documents = await _runs_across_every_block(tmp_path)
    judge = Judge(
        MockProvider(
            [
                ScriptedResponse(content="I would rather not."),
                ScriptedResponse(content=_NO_CLAIMS),
                *[ScriptedResponse(content=_SUPPORTED) for _ in range(20)],
            ]
        )
    )

    draw = await draw_human_sample(judge, runs, documents, count=5, config="full")

    assert len(draw.rows) == 5
    assert Counter(sample_block(row.item_id) for row in draw.rows) == dict.fromkeys(_BLOCKS, 1)
    # Blocks are drawn in sorted order, so the two refusals both landed in the first one.
    assert len(draw.excluded) == 2
    assert {sample_block(item_id) for item_id in draw.excluded} == {"g-cc"}
    assert not set(draw.excluded) & {row.item_id for row in draw.rows}
    assert all(row.judge_label for row in draw.rows)
    assert "were excluded and replaced from within their own block" in draw.method()


def test_the_block_of_an_item_id_is_its_prefix() -> None:
    """The CSV carries the id and nothing else, so the id is what a reviewer can check."""
    assert sample_block("g-gh-001") == "g-gh"
    assert sample_block("m-021") == "m"
    assert sample_block("solo") == "solo"


# --- harness -------------------------------------------------------------------------------------


def test_a_session_id_is_derived_safely_from_an_item_id() -> None:
    """It becomes a directory name, so it goes through the same shape the tracer requires."""
    from consilium.memory import validate_session_id

    assert validate_session_id(session_id_for("g-001", prefix="full")) == "full-g-001"
    assert validate_session_id(session_id_for("../escape", prefix="full"))
    assert validate_session_id(session_id_for("x" * 200, prefix="full"))


async def test_running_one_item_goes_through_the_production_turn_path(tmp_path: Path) -> None:
    """A harness that drove the system differently would measure a system nobody ships."""
    runtime = build_runtime(
        _settings(),
        provider=MockProvider([_plan("consultation"), ScriptedResponse(content="An answer.")]),
        embedder="hash",
        store="numpy",
    )

    run = await run_golden_item(runtime, _item(), runs_dir=tmp_path, prefix="full")

    assert run.ok
    assert run.outcome is not None
    assert turn_event(run.events) is not None
    assert (tmp_path / "full-g-001" / "0.jsonl").exists()


async def test_a_total_provider_outage_still_produces_a_delivered_answer(tmp_path: Path) -> None:
    """The planner falls back, the worker fails, the router answers from nothing anyway."""
    runtime = build_runtime(
        _settings(),
        provider=FailingProvider(),
        embedder="hash",
        store="numpy",
    )

    run = await run_golden_item(runtime, _item(), runs_dir=tmp_path, prefix="full")

    assert run.ok
    assert run.outcome is not None
    assert run.outcome.missing == ("consultation",)
    assert turn_event(run.events) is not None


async def test_an_item_that_raises_is_captured_rather_than_ending_the_sweep(
    tmp_path: Path,
) -> None:
    """One bad item must not end a 150-item run that costs money."""
    import dataclasses

    class _BrokenMemory:
        def get(self, session_id: str) -> object:
            raise RuntimeError("the session store is down")

        def save(self, memory: object) -> None:
            raise RuntimeError("the session store is down")

        def drop(self, session_id: str) -> None:
            raise RuntimeError("the session store is down")

        def sessions(self) -> list[str]:
            return []

    runtime = build_runtime(
        _settings(),
        provider=MockProvider([_plan("consultation"), ScriptedResponse(content="An answer.")]),
        embedder="hash",
        store="numpy",
    )
    broken = dataclasses.replace(runtime, memory=_BrokenMemory())  # type: ignore[arg-type]

    run = await run_golden_item(broken, _item(), runs_dir=tmp_path, prefix="full")

    assert run.ok is False
    assert run.error is not None and "session store is down" in run.error


async def test_each_item_runs_in_its_own_session(tmp_path: Path) -> None:
    """Otherwise item N could be answered from item N-1's working memory."""
    runtime = build_runtime(
        _settings(),
        provider=MockProvider(
            [
                _plan("consultation"),
                ScriptedResponse(content="First."),
                _plan("consultation"),
                ScriptedResponse(content="Second."),
            ]
        ),
        embedder="hash",
        store="numpy",
    )

    await run_golden_item(runtime, _item("g-001"), runs_dir=tmp_path, prefix="full")
    await run_golden_item(runtime, _item("g-002"), runs_dir=tmp_path, prefix="full")

    assert len(runtime.memory.get("full-g-001")) == 1
    assert len(runtime.memory.get("full-g-002")) == 1
    assert runtime.memory.get("full-g-002").exchanges[0].answer.startswith("Second.")


async def test_a_conversation_runs_every_turn_in_one_session(tmp_path: Path) -> None:
    """The only place a session spans turns, because it is the only place memory is under test."""
    conversation = MultiturnConversation(
        id="m-001",
        turns=(
            MultiturnTurn(question="I have high blood pressure"),
            MultiturnTurn(
                question="what about diet?", depends_on_turn=0, expected_referent="hypertension"
            ),
        ),
        labeled=True,
    )
    runtime = build_runtime(
        _settings(),
        provider=MockProvider(
            [
                _plan("consultation"),
                ScriptedResponse(content="Noted."),
                _plan("consultation"),
                ScriptedResponse(content="Sodium reduction is described."),
            ]
        ),
        embedder="hash",
        store="numpy",
    )

    run = await run_conversation(runtime, conversation, runs_dir=tmp_path, prefix="full-mt")

    assert run.error is None
    assert len(run.answers) == 2
    assert len(runtime.memory.get("full-mt-m-001")) == 2
    assert (tmp_path / "full-mt-m-001" / "1.jsonl").exists()


async def test_memory_off_still_runs_a_conversation(tmp_path: Path) -> None:
    """`full_no_memory` must be expressible against the multi-turn set, not only the golden one."""
    conversation = MultiturnConversation(
        id="m-002",
        turns=(MultiturnTurn(question="one"), MultiturnTurn(question="two")),
    )
    runtime = build_runtime(
        _settings(),
        config=RunConfig(name="full_no_memory", memory=False),
        provider=MockProvider(
            [
                _plan("consultation"),
                ScriptedResponse(content="One."),
                _plan("consultation"),
                ScriptedResponse(content="Two."),
            ]
        ),
        embedder="hash",
        store="numpy",
    )

    run = await run_conversation(runtime, conversation, runs_dir=tmp_path, prefix="nomem")

    assert [answer.split("\n")[0] for answer in run.answers] == ["One.", "Two."]
    assert len(runtime.memory.get("nomem-m-002")) == 0


# --- runner helpers ------------------------------------------------------------------------------


def test_pricing_ships_empty_so_cost_is_not_measured_by_default() -> None:
    """A rate card copied from a vendor page at some past date is not a measurement."""
    assert load_pricing() == {}


def test_the_stratified_subset_keeps_the_category_mix() -> None:
    """The golden set is written in category blocks, so the first N would be two categories."""
    categories: tuple[GoldenCategory, ...] = (
        "general_health",
        "symptom_urgency",
        "condition_coding",
    )
    items = [
        _item(f"{category}-{index}", category) for category in categories for index in range(10)
    ]

    chosen = stratified(items, 6)

    assert len(chosen) == 6
    assert len({item.category for item in chosen}) == 3


def test_the_commit_is_recorded_for_the_run() -> None:
    """Published numbers name the commit that produced them."""
    assert git_commit()


# --- the runner's guardrails ---------------------------------------------------------------------


async def test_the_runner_refuses_to_publish_numbers_from_a_mock_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numbers from a scripted mock are not measurements and must never reach a results file."""
    from eval.run import main

    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    code = await main(["--golden", str(tmp_path / "absent.jsonl")])

    assert code == 2


async def test_the_runner_refuses_an_unlabelled_golden_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The checkpoint, enforced where it cannot be forgotten."""
    from eval.items import write_jsonl
    from eval.run import main

    draft = tmp_path / "golden.jsonl"
    write_jsonl(draft, [GoldenItem(id="g-001", question="q", category="general_health")])

    monkeypatch.setenv("CONSILIUM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    code = await main(["--golden", str(draft), "--multiturn", str(draft)])

    assert code == 2
    assert "labelled by hand" in capsys.readouterr().err


async def test_a_human_sample_without_a_config_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise the sample comes from the ablation set's first preset, which retrieves nothing."""
    from eval.run import main

    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    code = await main(["--human-sample", "40"])

    assert code == 2
    err = capsys.readouterr().err
    assert "baseline_llm" in err
    assert "--config full --human-sample 40" in err


async def test_a_human_sample_with_the_judge_switched_off_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sample exists to compare the judge against a person; there is nothing to compare."""
    from eval.run import main

    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    code = await main(["--config", "full", "--human-sample", "40", "--no-judge"])

    assert code == 2
    assert "cannot be combined with --no-judge" in capsys.readouterr().err


async def test_score_judge_reports_agreement_without_touching_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eval.run import main

    sample = tmp_path / "judge_sample.csv"
    write_sample(
        sample,
        [
            SampleRow("a", "q", "x", "", "s", "supported", "r", human_label="supported"),
            SampleRow("b", "q", "x", "", "s", "supported", "r", human_label="unsupported"),
        ],
    )

    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    code = await main(["--score-judge", str(sample)])

    assert code == 0
    output = capsys.readouterr().out
    assert "raw agreement: 0.500" in output
    assert "docs/EVALUATION.md" in output


async def test_score_judge_reports_an_unlabelled_sample_rather_than_a_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eval.run import main

    sample = tmp_path / "judge_sample.csv"
    write_sample(sample, [SampleRow("a", "q", "x", "", "s", "supported", "r")])

    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    code = await main(["--score-judge", str(sample)])

    assert code == 2
    assert "Fill in the last column" in capsys.readouterr().err
