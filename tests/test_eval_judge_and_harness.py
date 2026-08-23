"""The judge, its validation loop, and running one item through the production turn path."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from consilium.config import RunConfig, Settings
from consilium.llm import MockProvider, ScriptedResponse
from consilium.runtime import build_runtime
from eval.harness import run_conversation, run_golden_item, session_id_for
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
    Judge,
    JudgeError,
    SampleRow,
    agreement,
    load_prompt,
    score_sample,
    write_sample,
)
from eval.metrics import turn_event
from eval.run import git_commit, load_pricing, stratified
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


def test_the_sample_csv_leaves_the_human_column_empty(tmp_path: Path) -> None:
    path = tmp_path / "judge_sample.csv"
    write_sample(
        path,
        [
            SampleRow(
                item_id="g-001",
                question="q",
                answer="a",
                retrieved_doc_ids="doc-a doc-b",
                judge_label="supported",
                judge_rationale="because",
            )
        ],
    )

    with path.open(encoding="utf-8", newline="") as handle:
        (row,) = list(csv.DictReader(handle))
    assert row["human_label"] == ""
    assert row["retrieved_doc_ids"] == "doc-a doc-b"


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
            SampleRow("a", "q", "x", "", "supported", "r", human_label="supported"),
            SampleRow("b", "q", "x", "", "supported", "r"),
        ],
    )

    result = score_sample(path)

    assert result.n == 1
    assert result.raw_agreement == 1.0


def test_scoring_a_sample_nobody_labelled_says_so(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"
    write_sample(path, [SampleRow("a", "q", "x", "", "supported", "r")])

    with pytest.raises(JudgeError, match="Fill in the last column"):
        score_sample(path)


def test_scoring_a_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(JudgeError, match="cannot read the judge sample"):
        score_sample(tmp_path / "absent.csv")


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


async def test_score_judge_reports_agreement_without_touching_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eval.run import main

    sample = tmp_path / "judge_sample.csv"
    write_sample(
        sample,
        [
            SampleRow("a", "q", "x", "", "supported", "r", human_label="supported"),
            SampleRow("b", "q", "x", "", "supported", "r", human_label="unsupported"),
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
    write_sample(sample, [SampleRow("a", "q", "x", "", "supported", "r")])

    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    code = await main(["--score-judge", str(sample)])

    assert code == 2
    assert "Fill in the last column" in capsys.readouterr().err
