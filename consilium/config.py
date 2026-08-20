"""Substrate: process settings and evaluation run configuration.

Two unrelated kinds of configuration live here on purpose, because they are both "the knobs":

``Settings``   Deployment facts read from the environment -- which provider, which model, where the
               corpus and the traces live.  Immutable once constructed.
``RunConfig``  Which *capabilities of the architecture are switched on* for a given run.  The
               ablation table in docs/EVALUATION.md needs the system runnable in reduced modes, and
               a reduced mode has to be a flag rather than a code edit or the ablation is not
               reproducible.  ``RunConfig`` is threaded from eval/run.py through the router and the
               ReAct loop; it is defined here in Phase 1 so that the router and loop can accept it
               natively instead of being retrofitted in Phase 8.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr

ProviderName = Literal["openai", "anthropic", "mock"]
RouterMode = Literal["planner", "single", "none"]
LogFormat = Literal["json", "console"]

DEFAULT_RUNS_DIR = "runs"
DEFAULT_DATA_DIR = "data"
DEFAULT_CORPUS_DIR = "data/corpus"
DEFAULT_CHROMA_DIR = "data/chroma"
DEFAULT_EPISODIC_DB = "data/episodic.db"


class Settings(BaseModel):
    """Deployment configuration, read once from the environment.

    API keys are held as :class:`~pydantic.SecretStr` so that logging or repr-ing a ``Settings``
    instance cannot leak a credential into a trace file or a structlog record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ProviderName = "mock"
    model: str | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    root_dir: Path = Path()
    runs_dir: Path = Path(DEFAULT_RUNS_DIR)
    #: Where the three runtime data files live: policy.yaml, red_flags.yaml, symptom_systems.yaml.
    #: One setting rather than three, because they are always edited and deployed together and a
    #: deployment that could point at two of the three from different directories would be a
    #: configuration nobody intends.
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    corpus_dir: Path = Path(DEFAULT_CORPUS_DIR)
    chroma_dir: Path = Path(DEFAULT_CHROMA_DIR)
    episodic_db_path: Path = Path(DEFAULT_EPISODIC_DB)

    log_level: str = "INFO"
    log_format: LogFormat = "json"

    @property
    def policy_path(self) -> Path:
        return self.data_dir / "policy.yaml"

    @property
    def red_flags_path(self) -> Path:
        return self.data_dir / "red_flags.yaml"

    @property
    def symptom_systems_path(self) -> Path:
        return self.data_dir / "symptom_systems.yaml"

    @classmethod
    def from_env(cls, *, root_dir: Path | None = None, load_env_file: bool = True) -> Settings:
        """Build settings from environment variables, optionally loading a ``.env`` file first.

        The provider defaults to ``mock`` rather than ``openai``: an unconfigured checkout should
        run the test suite and the offline demo without reaching for a credential, and a missing
        key should be an explicit choice to use a real provider rather than a stack trace.
        """
        if load_env_file:
            load_dotenv(override=False)

        root = (root_dir or Path.cwd()).resolve()

        def _path(env_name: str, default: str) -> Path:
            raw = os.environ.get(env_name, default)
            candidate = Path(raw)
            return candidate if candidate.is_absolute() else root / candidate

        def _secret(env_name: str) -> SecretStr | None:
            raw = os.environ.get(env_name, "").strip()
            return SecretStr(raw) if raw else None

        provider = os.environ.get("CONSILIUM_PROVIDER", "mock").strip().lower()
        if provider not in ("openai", "anthropic", "mock"):
            raise ValueError(
                f"CONSILIUM_PROVIDER must be one of openai, anthropic, mock; got {provider!r}"
            )

        model = os.environ.get("CONSILIUM_MODEL", "").strip() or None

        return cls(
            provider=provider,
            model=model,
            openai_api_key=_secret("OPENAI_API_KEY"),
            anthropic_api_key=_secret("ANTHROPIC_API_KEY"),
            root_dir=root,
            runs_dir=_path("CONSILIUM_RUNS_DIR", DEFAULT_RUNS_DIR),
            data_dir=_path("CONSILIUM_DATA_DIR", DEFAULT_DATA_DIR),
            corpus_dir=_path("CONSILIUM_CORPUS_DIR", DEFAULT_CORPUS_DIR),
            chroma_dir=_path("CONSILIUM_CHROMA_DIR", DEFAULT_CHROMA_DIR),
            episodic_db_path=_path("CONSILIUM_EPISODIC_DB", DEFAULT_EPISODIC_DB),
            log_level=os.environ.get("CONSILIUM_LOG_LEVEL", "INFO").strip().upper(),
            log_format=_log_format(),
        )


def _log_format() -> LogFormat:
    raw = os.environ.get("CONSILIUM_LOG_FORMAT", "json").strip().lower()
    if raw not in ("json", "console"):
        raise ValueError(f"CONSILIUM_LOG_FORMAT must be json or console; got {raw!r}")
    return raw  # type: ignore[return-value]  # validated above


class RunConfig(BaseModel):
    """Independent toggles for one evaluation configuration.

    The toggles are orthogonal on purpose.  ``router="none"`` with ``retrieval=False`` is the
    plain-LLM control the ablation needs; anything less separable would make it impossible to say
    which component a difference came from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    retrieval: bool = True
    router: RouterMode = "planner"
    memory: bool = True
    max_tool_calls: int = Field(default=2, ge=0)
    max_iterations: int = Field(default=3, ge=1)


#: The five named presets.  Four of them are rows in the ablation table; ``full_budget_6`` is a
#: diagnostic run used to observe the untruncated tool-call distribution, because a distribution
#: measured at the cap cannot justify the cap.  It is reported separately, with its n stated.
PRESETS: dict[str, RunConfig] = {
    "baseline_llm": RunConfig(
        name="baseline_llm", retrieval=False, router="none", memory=False, max_tool_calls=0
    ),
    "single_agent_rag": RunConfig(
        name="single_agent_rag", retrieval=True, router="single", memory=False
    ),
    "full": RunConfig(name="full"),
    "full_no_memory": RunConfig(name="full_no_memory", memory=False),
    "full_budget_6": RunConfig(name="full_budget_6", max_tool_calls=6),
}

#: Presets that make up the paired ablation table, in report order.
ABLATION_PRESETS: tuple[str, ...] = ("baseline_llm", "single_agent_rag", "full", "full_no_memory")


def get_preset(name: str) -> RunConfig:
    """Look up a named preset, with the available names in the error message."""
    try:
        return PRESETS[name]
    except KeyError:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(f"unknown run configuration {name!r}; known presets: {known}") from None
