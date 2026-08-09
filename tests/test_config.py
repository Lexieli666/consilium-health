"""Settings and RunConfig.

The preset assertions matter more than they look: the ablation table is only a control if
``baseline_llm`` really has retrieval and routing switched off, and that is a property of this
dictionary rather than of any prose in the docs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.config import ABLATION_PRESETS, PRESETS, RunConfig, Settings, get_preset

ENV_VARS = (
    "CONSILIUM_PROVIDER",
    "CONSILIUM_MODEL",
    "CONSILIUM_LOG_LEVEL",
    "CONSILIUM_LOG_FORMAT",
    "CONSILIUM_RUNS_DIR",
    "CONSILIUM_CORPUS_DIR",
    "CONSILIUM_CHROMA_DIR",
    "CONSILIUM_EPISODIC_DB",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_offline(tmp_path: Path) -> None:
    """An unconfigured checkout runs the demo and the tests without reaching for a credential."""
    settings = Settings.from_env(root_dir=tmp_path, load_env_file=False)

    assert settings.provider == "mock"
    assert settings.model is None
    assert settings.openai_api_key is None
    assert settings.runs_dir == tmp_path / "runs"
    assert settings.corpus_dir == tmp_path / "data" / "corpus"


def test_environment_overrides_are_applied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CONSILIUM_PROVIDER", "openai")
    monkeypatch.setenv("CONSILIUM_MODEL", "some-model")
    monkeypatch.setenv("CONSILIUM_LOG_FORMAT", "console")
    monkeypatch.setenv("CONSILIUM_RUNS_DIR", "/absolute/runs")

    settings = Settings.from_env(root_dir=tmp_path, load_env_file=False)

    assert settings.provider == "openai"
    assert settings.model == "some-model"
    assert settings.log_format == "console"
    assert settings.runs_dir == Path("/absolute/runs")


def test_api_keys_are_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Settings object may end up in a log line or an exception; the key must not follow it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-printed")

    settings = Settings.from_env(root_dir=tmp_path, load_env_file=False)

    assert settings.openai_api_key is not None
    assert "sk-should-never-be-printed" not in repr(settings)
    assert "sk-should-never-be-printed" not in str(settings)
    assert settings.openai_api_key.get_secret_value() == "sk-should-never-be-printed"


def test_blank_api_key_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")

    assert Settings.from_env(root_dir=tmp_path, load_env_file=False).anthropic_api_key is None


@pytest.mark.parametrize(
    ("name", "value"), [("CONSILIUM_PROVIDER", "cohere"), ("CONSILIUM_LOG_FORMAT", "xml")]
)
def test_invalid_enumerated_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_env(root_dir=tmp_path, load_env_file=False)


def test_settings_are_immutable(tmp_path: Path) -> None:
    settings = Settings.from_env(root_dir=tmp_path, load_env_file=False)

    with pytest.raises(ValueError, match="frozen"):
        settings.provider = "openai"  # type: ignore[misc]


def test_baseline_preset_really_is_a_control() -> None:
    baseline = PRESETS["baseline_llm"]

    assert baseline.retrieval is False
    assert baseline.router == "none"
    assert baseline.memory is False
    assert baseline.max_tool_calls == 0


def test_presets_isolate_one_variable_at_a_time() -> None:
    full = PRESETS["full"]
    no_memory = PRESETS["full_no_memory"]
    budget_6 = PRESETS["full_budget_6"]

    assert (full.retrieval, full.router, full.memory, full.max_tool_calls) == (
        True,
        "planner",
        True,
        2,
    )
    assert no_memory.model_dump(exclude={"name", "memory"}) == full.model_dump(
        exclude={"name", "memory"}
    )
    assert no_memory.memory is False
    assert budget_6.model_dump(exclude={"name", "max_tool_calls"}) == full.model_dump(
        exclude={"name", "max_tool_calls"}
    )
    assert budget_6.max_tool_calls == 6


def test_ablation_presets_are_the_four_table_rows() -> None:
    """full_budget_6 is a diagnostic run, not an ablation row."""
    assert ABLATION_PRESETS == (
        "baseline_llm",
        "single_agent_rag",
        "full",
        "full_no_memory",
    )
    assert set(ABLATION_PRESETS) < set(PRESETS)


def test_preset_names_match_their_keys() -> None:
    assert all(name == preset.name for name, preset in PRESETS.items())


def test_unknown_preset_lists_the_known_ones() -> None:
    with pytest.raises(KeyError, match="baseline_llm"):
        get_preset("full_but_faster")


def test_negative_budgets_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_tool_calls"):
        RunConfig(name="bad", max_tool_calls=-1)
