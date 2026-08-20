"""Substrate: constructing a provider from settings.

One function, so that every entry point -- the CLI, the API, the eval harness -- picks a provider
the same way and reports a missing credential with the same sentence.  The factory never reads a
credential from anywhere but :class:`~consilium.config.Settings`, which holds them as ``SecretStr``,
and it never writes one anywhere.
"""

from __future__ import annotations

from pathlib import Path

from consilium.config import Settings
from consilium.llm.anthropic_provider import DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL
from consilium.llm.anthropic_provider import AnthropicProvider
from consilium.llm.base import LLMProvider
from consilium.llm.mock import MockProvider, ScriptedResponse
from consilium.llm.openai_provider import DEFAULT_MODEL as OPENAI_DEFAULT_MODEL
from consilium.llm.openai_provider import OpenAIProvider

#: What an unscripted ``MockProvider`` answers.  It exists so that ``consilium ask`` runs offline
#: with no key, and it is deliberately unmistakable for a real answer: a mock reply that reads like
#: content is how a screenshot of a mock ends up in a README.
UNSCRIPTED_MOCK_ANSWER = (
    "(mock provider: no script loaded, so this is a placeholder rather than an answer. "
    "Set CONSILIUM_PROVIDER and a key, or pass --script, to get a real response.)"
)


class ProviderError(RuntimeError):
    """Raised when the configured provider cannot be constructed."""


def make_provider(settings: Settings, *, script: Path | None = None) -> LLMProvider:
    """Build the provider named by ``settings``.

    ``script`` applies only to the mock provider and is how an offline demo or a test supplies
    scripted responses without touching the environment.
    """
    if settings.provider == "mock":
        if script is not None:
            return MockProvider.from_file(script, model=settings.model or "mock-model")
        return MockProvider(
            [ScriptedResponse(content=UNSCRIPTED_MOCK_ANSWER)],
            model=settings.model or "mock-model",
        )

    if settings.provider == "openai":
        if settings.openai_api_key is None:
            raise ProviderError(
                "CONSILIUM_PROVIDER=openai but OPENAI_API_KEY is not set. Set it in the "
                "environment or in a .env file; never write a key into tracked source."
            )
        return OpenAIProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.model or OPENAI_DEFAULT_MODEL,
        )

    if settings.anthropic_api_key is None:
        raise ProviderError(
            "CONSILIUM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. Set it in the "
            "environment or in a .env file; never write a key into tracked source."
        )
    return AnthropicProvider(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.model or ANTHROPIC_DEFAULT_MODEL,
    )
