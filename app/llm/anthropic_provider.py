"""Claude backend, using the official Anthropic SDK with structured JSON output."""

from __future__ import annotations

import json

import anthropic

from .base import LLMError, LLMProvider


class AnthropicProvider(LLMProvider):
    is_cloud = True

    def __init__(self, name: str, model: str = "claude-opus-5", api_key: str | None = None,
                 refusal_fallback: bool = True, timeout: float = 120.0):
        super().__init__(name, model)
        self.api_key = api_key
        self.timeout = timeout
        self.refusal_fallback = refusal_fallback
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        # Created on first use, so the bot starts even when no API key is configured.
        # With api_key=None the SDK reads ANTHROPIC_API_KEY from the environment.
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=self.timeout, max_retries=2)
        return self._client

    async def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict:
        params = dict(
            model=self.model,
            max_tokens=max(max_tokens, 16000),  # room for adaptive thinking; billed only for what is used
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        try:
            client = self.client
            if self.refusal_fallback:
                # Server-side fallback: if the model declines, the API reruns the request on a fallback model.
                response = await client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                response = await client.messages.create(**params)
        except anthropic.AnthropicError as exc:
            raise LLMError(f"{self.name}: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMError(f"{self.name}: request declined")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{self.name}: answer was cut off")
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.name}: invalid JSON") from exc
