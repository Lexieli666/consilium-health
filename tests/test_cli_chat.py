"""`consilium chat`, the multi-turn REPL.

The property under test is the one that makes the multi-turn golden set meaningful: **the REPL holds
one session id for the whole conversation and threads the same session-keyed working memory through
every turn.**  It opens no memory path of its own -- `run_turn` fetches the session's
`WorkingMemory` from `Runtime.memory` by the tracer's session id, exactly as the API and the
evaluation harness do -- so a conversation held here exercises the code the numbers come from.

Offline, like the rest of the CLI tests: `--embedder hash --store numpy`, a scripted provider, no
key and no network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from consilium.cli import app
from consilium.llm.base import LLMProvider
from consilium.runtime import Runtime
from consilium.trace import TurnEvent, read_trace, trace_path
from tests.stubs import TurnProvider

runner = CliRunner()

PLAN = '{"subtasks": [{"agent": "consultation", "objective": "Answer it.", "why": "test"}]}'
FIRST = "Hypertension is persistently raised blood pressure."
SECOND = "Lower sodium intake is the usual first dietary step."

SCRIPT = f"""\
responses:
  - content: '{PLAN}'
  - content: "{FIRST}"
  - content: '{PLAN}'
  - content: "{SECOND}"
"""


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "script.yaml"
    path.write_text(SCRIPT, encoding="utf-8")
    return path


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temporary runs directory and the repository's data."""
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path / "runs"
    monkeypatch.setenv("CONSILIUM_PROVIDER", "mock")
    monkeypatch.setenv("CONSILIUM_RUNS_DIR", str(directory))
    monkeypatch.setenv("CONSILIUM_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("CONSILIUM_CORPUS_DIR", str(root / "data" / "corpus"))
    return directory


@pytest.fixture
def provider() -> TurnProvider:
    """Two distinguishable answers, so the replayed transcript can be attributed to a turn."""
    return TurnProvider(
        answer=lambda prompt: SECOND if "diet" in prompt else FIRST,
    )


@pytest.fixture
def injected(
    monkeypatch: pytest.MonkeyPatch,
    offline_runtime: Callable[[LLMProvider], Runtime],
    provider: TurnProvider,
    runs: Path,
) -> Iterator[TurnProvider]:
    """Hand `chat` a runtime whose provider the test can read afterwards.

    Patched at the composition root rather than mocked deeper: what is being asserted is which
    messages reached the model on the second turn, and that is only visible from the provider.
    """
    runtime = offline_runtime(provider)

    def build(*_args: object, **_kwargs: object) -> Runtime:
        return runtime

    monkeypatch.setattr("consilium.cli.build_runtime", build)
    yield provider


def chat(*args: str, keys: str) -> Result:
    return runner.invoke(
        app,
        ["chat", *args, "--embedder", "hash", "--store", "numpy"],
        input=keys,
        catch_exceptions=False,
    )


def replayed(provider: TurnProvider, index: int) -> str:
    """Everything the specialist was given on its `index`-th call."""
    calls = [messages for caller, messages in provider.calls if caller == "agent:consultation"]
    return " ".join(message.content or "" for message in calls[index])


def turn_events(runs_dir: Path, session_id: str, turn_index: int) -> list[TurnEvent]:
    path = trace_path(runs_dir, session_id, turn_index)
    return [event for event in read_trace(path) if isinstance(event, TurnEvent)]


def test_chat_answers_a_turn_and_reports_what_produced_it(runs: Path, script: Path) -> None:
    result = chat(
        "--script", str(script), "--session", "c-one", keys="what is hypertension\n/exit\n"
    )

    assert result.exit_code == 0
    assert FIRST in result.stdout
    assert "route      : single [consultation]" in result.stdout
    assert "risk level : routine" in result.stdout
    assert "trace      :" in result.stdout


def test_one_session_id_covers_the_whole_repl(runs: Path, script: Path) -> None:
    result = chat(
        "--script",
        str(script),
        "--session",
        "c-session",
        keys="what is hypertension\nand what about diet\n/exit\n",
    )

    assert "session    : c-session" in result.stdout
    assert "session c-session ended after 2 turn(s)." in result.stdout
    assert len(turn_events(runs, "c-session", 0)) == 1
    assert len(turn_events(runs, "c-session", 1)) == 1


def test_the_second_turn_sees_the_first(injected: TurnProvider, runs: Path) -> None:
    """The session-keyed working memory, threaded by `run_turn` -- not a buffer the REPL keeps."""
    chat("--session", "c-memory", keys="what is hypertension\nand what about diet\n/exit\n")

    second = replayed(injected, 1)
    assert "what is hypertension" in second
    assert FIRST in second


def test_the_first_turn_sees_nothing(injected: TurnProvider) -> None:
    chat("--session", "c-fresh", keys="what is hypertension\nand what about diet\n/exit\n")

    assert "and what about diet" not in replayed(injected, 0)


def test_a_session_id_is_minted_when_none_is_given(runs: Path, script: Path) -> None:
    result = chat("--script", str(script), keys="what is hypertension\n/exit\n")

    assert "session    : chat-" in result.stdout


@pytest.mark.parametrize("command", ["/exit", "/quit"])
def test_either_exit_command_ends_the_session(runs: Path, script: Path, command: str) -> None:
    result = chat("--script", str(script), keys=f"what is hypertension\n{command}\n")

    assert result.exit_code == 0
    assert "ended after 1 turn(s)." in result.stdout


def test_end_of_input_ends_the_session(runs: Path, script: Path) -> None:
    """A piped stdin that runs out is the same event as ctrl-D, and neither is a crash."""
    result = chat("--script", str(script), keys="what is hypertension\n")

    assert result.exit_code == 0
    assert "ended after 1 turn(s)." in result.stdout


def test_help_lists_the_commands(runs: Path, script: Path) -> None:
    result = chat("--script", str(script), keys="/help\n/exit\n")

    assert "/session" in result.stdout
    assert "show these commands" in result.stdout


def test_the_session_command_reports_the_session(injected: TurnProvider, runs: Path) -> None:
    result = chat("--session", "c-report", keys="what is hypertension\n/session\n/exit\n")

    assert "turns      : 1 recorded, next trace index 1" in result.stdout
    assert str(runs / "c-report") in result.stdout


def test_an_unknown_command_is_reported_not_answered(runs: Path, script: Path) -> None:
    """A mistyped command must not be sent to the model as a question; the script would then be
    consumed by it and the next real question would answer from the wrong reply."""
    result = chat("--script", str(script), keys="/sesion\nwhat is hypertension\n/exit\n")

    assert "unknown command /sesion" in result.stdout
    assert FIRST in result.stdout


def test_blank_input_is_ignored(runs: Path, script: Path) -> None:
    result = chat("--script", str(script), keys="\n   \nwhat is hypertension\n/exit\n")

    assert "ended after 1 turn(s)." in result.stdout


def test_a_memory_off_preset_is_flagged_not_refused(runs: Path, script: Path) -> None:
    """The preset is valid and the turns still run; what changes is that they no longer see each
    other, and a reader who typed a follow-up would otherwise read that as a memory failure."""
    result = chat(
        "--script", str(script), "--config", "full_no_memory", keys="what is hypertension\n/exit\n"
    )

    assert "memory off" in result.stdout
    assert FIRST in result.stdout


def test_resuming_a_session_id_continues_the_trace_numbering(runs: Path, script: Path) -> None:
    """A trace sink appends, so starting again at turn 0 would interleave two turns' events in one
    file -- and the file is the artifact every published number is computed from."""
    chat("--script", str(script), "--session", "c-resume", keys="what is hypertension\n/exit\n")
    chat("--script", str(script), "--session", "c-resume", keys="and what about diet\n/exit\n")

    assert len(turn_events(runs, "c-resume", 0)) == 1
    assert len(turn_events(runs, "c-resume", 1)) == 1
    assert turn_events(runs, "c-resume", 1)[0].question == "and what about diet"


def test_an_unknown_preset_is_refused(runs: Path, script: Path) -> None:
    result = chat("--script", str(script), "--config", "nope", keys="/exit\n")

    assert result.exit_code == 1
