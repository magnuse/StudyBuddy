"""Backend for OpenAI-compatible local servers such as LM Studio, llama.cpp or vLLM."""

from __future__ import annotations

import json

import httpx

from .base import LLMError, LLMProvider


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, name: str, model: str, base_url: str = "http://localhost:1234/v1",
                 api_key: str | None = None, timeout: float = 600.0):
        super().__init__(name, model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise LLMError(f"{self.name}: {exc}") from exc
