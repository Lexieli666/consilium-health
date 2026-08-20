"""`consilium ask` and `consilium trace`, end to end against MockProvider.

The offline rule applies to the CLI as much as to the library: these run with no key, no network
and no model download, on the `--embedder hash --store numpy` pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from consilium.cli import app

runner = CliRunner()

SCRIPT = """\
responses:
  - content: '{"subtasks": [{"agent": "consultation", "objective": "Explain it.", "why": "t"}]}'
  - tool_calls:
      - name: search_knowledge
        arguments: {query: "hypertension"}
  - content: "Hypertension is persistently raised blood pressure. See condition-hypertension."
"""


PINNED_SCRIPT = """\
responses:
  - content: "Call emergency services now. Chest discomfort can indicate a heart attack."
"""


@pytest.fixture
def script(tmp_path: Path) -> Path:
    """A routed turn: a planner reply, then the specialist's tool call and answer."""
    path = tmp_path / "script.yaml"
    path.write_text(SCRIPT, encoding="utf-8")
    return path


@pytest.fixture
def pinned_script(tmp_path: Path) -> Path:
    """A pinned turn makes no planner call, so its script has no plan in it."""
    path = tmp_path / "pinned.yaml"
    path.write_text(PINNED_SCRIPT, encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temporary runs directory and the repository's data."""
    root = Path(__file__).resolve().parents[1]
    runs = tmp_path / "runs"
    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    monkeypatch.setenv("CONSILIUM_RUNS_DIR", str(runs))
    monkeypatch.setenv("CONSILIUM_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("CONSILIUM_CORPUS_DIR", str(root / "data" / "corpus"))
    return runs


def _ask(*args: str) -> Result:
    return runner.invoke(
        app, ["ask", *args, "--embedder", "hash", "--store", "numpy"], catch_exceptions=False
    )


def test_ask_answers_and_reports_its_sources_and_trace(env: Path, script: Path) -> None:
    result = _ask("what is hypertension", "--script", str(script), "--session", "cli-one")

    assert result.exit_code == 0
    output = result.stdout
    assert "persistently raised blood pressure" in output
    assert "risk level : routine" in output
    assert "condition-hypertension" in output
    assert (env / "cli-one" / "0.jsonl").exists()


def test_ask_can_pin_a_specialist_and_skip_routing(env: Path, pinned_script: Path) -> None:
    result = _ask(
        "I have crushing chest pain",
        "--agent",
        "diagnostic",
        "--script",
        str(pinned_script),
        "--session",
        "cli-two",
    )

    assert result.exit_code == 0
    assert "route      : single [diagnostic]" in result.stdout
    assert "risk level : emergency" in result.stdout


def test_ask_reports_the_route_the_planner_chose(env: Path, script: Path) -> None:
    result = _ask("what is hypertension", "--script", str(script), "--session", "cli-routed")

    assert result.exit_code == 0
    assert "route      : single [consultation]" in result.stdout
    assert "planner fallback" not in result.stdout


def test_ask_says_so_when_the_planner_fell_back(env: Path, tmp_path: Path) -> None:
    """A fallback means the plan was unusable; a reader should not need the trace to learn that."""
    path = tmp_path / "unparseable.yaml"
    path.write_text(
        'responses:\n  - content: "I am not going to produce JSON today."\n'
        '  - content: "An answer anyway."\n',
        encoding="utf-8",
    )

    result = _ask("what is hypertension", "--script", str(path), "--session", "cli-fallback")

    assert result.exit_code == 0
    assert "planner fallback" in result.stdout


def test_ask_rejects_an_unknown_agent(env: Path) -> None:
    result = _ask("q", "--agent", "triage")
    assert result.exit_code != 0
    assert "--agent must be one of" in result.output


def test_ask_rejects_an_unknown_preset(env: Path) -> None:
    result = _ask("q", "--config", "not_a_preset")
    assert result.exit_code == 1


def test_ask_runs_without_a_script_and_says_the_answer_is_a_placeholder(env: Path) -> None:
    """`consilium ask` has to work in a fresh checkout with no key; it must not look real."""
    result = _ask("anything", "--session", "cli-three")

    assert result.exit_code == 0
    assert "mock provider" in result.stdout


def test_trace_pretty_prints_the_turn_that_ask_wrote(env: Path, script: Path) -> None:
    _ask("what is hypertension", "--script", str(script), "--session", "cli-four")

    result = runner.invoke(app, ["trace", "cli-four"], catch_exceptions=False)

    assert result.exit_code == 0
    for expected in ("llm_call", "retrieval", "tool_call", "turn", "caller=agent:consultation"):
        assert expected in result.stdout, expected


def test_trace_reports_a_missing_run_rather_than_an_empty_one(env: Path) -> None:
    result = runner.invoke(app, ["trace", "never-ran"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "no trace at" in result.output


def test_trace_reports_the_line_number_of_a_corrupt_record(env: Path, tmp_path: Path) -> None:
    """The command is a check on the artifact every number is derived from, not a log viewer."""
    path = env / "corrupt"
    path.mkdir(parents=True)
    (path / "0.jsonl").write_text('{"type": "turn"}\n', encoding="utf-8")

    result = runner.invoke(app, ["trace", "corrupt"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "0.jsonl:1" in result.output
