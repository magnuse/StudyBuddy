"""Picks a provider per task and falls back down the configured list."""

from __future__ import annotations

import logging
from typing import Any

from .anthropic_provider import AnthropicProvider
from .base import LLMError, LLMProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatibleProvider

log = logging.getLogger(__name__)

PROVIDER_TYPES = {
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
}


def build_provider(name: str, spec: dict) -> LLMProvider:
    spec = dict(spec)
    kind = spec.pop("type")
    if kind not in PROVIDER_TYPES:
        raise ValueError(f"Unknown provider type {kind!r} for {name!r}")
    return PROVIDER_TYPES[kind](name=name, **spec)


def missing_keys(result: Any, schema: dict) -> list[str]:
    if not isinstance(result, dict):
        return ["<not an object>"]
    return [key for key in schema.get("required", []) if key not in result]


class LLMRouter:
    def __init__(self, providers: dict[str, LLMProvider], tasks: dict[str, list[str]], allow_cloud: bool = True):
        self.providers = providers
        self.tasks = tasks
        self.allow_cloud = allow_cloud
        #: Name of the provider that answered the most recent call, per task.
        self.last_used: dict[str, str] = {}

    @classmethod
    def from_config(cls, llm_config: dict) -> "LLMRouter":
        providers = {name: build_provider(name, spec) for name, spec in llm_config["providers"].items()}
        return cls(providers, llm_config["tasks"], llm_config.get("allow_cloud", True))

    def chain(self, task: str) -> list[LLMProvider]:
        names = self.tasks.get(task) or []
        chain = []
        for name in names:
            provider = self.providers.get(name)
            if provider is None:
                log.warning("Task %s refers to unknown provider %s", task, name)
                continue
            if provider.is_cloud and not self.allow_cloud:
                continue
            chain.append(provider)
        return chain

    async def complete_json(self, task: str, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict:
        chain = self.chain(task)
        if not chain:
            raise LLMError(f"No provider available for task {task!r}")
        errors = []
        for provider in chain:
            for attempt in (1, 2):  # one retry per provider for malformed output
                try:
                    result = await provider.complete_json(system, prompt, schema, max_tokens)
                except LLMError as exc:
                    errors.append(str(exc))
                    log.warning("Task %s: %s failed (%s)", task, provider.name, exc)
                    break  # network or API error: go to the next provider
                missing = missing_keys(result, schema)
                if not missing:
                    self.last_used[task] = provider.name
                    return result
                errors.append(f"{provider.name}: missing {missing}")
                log.warning("Task %s: %s returned incomplete JSON (attempt %d)", task, provider.name, attempt)
        raise LLMError(f"All providers failed for {task!r}: " + "; ".join(errors))
