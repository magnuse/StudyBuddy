"""Ollama backend, using its native /api/chat endpoint with a JSON schema as format."""

from __future__ import annotations

import json

import httpx

from .base import LLMError, LLMProvider


class OllamaProvider(LLMProvider):
    def __init__(self, name: str, model: str, base_url: str = "http://localhost:11434", timeout: float = 600.0):
        super().__init__(name, model)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=body)
                if response.status_code == 404:
                    raise LLMError(f"{self.name}: {self.missing_model_hint()}")
                response.raise_for_status()
            content = response.json()["message"]["content"]
            return json.loads(content)
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
            raise LLMError(f"{self.name}: {exc}") from exc

    def missing_model_hint(self) -> str:
        return (f"the model {self.model} is not downloaded in Ollama. "
                f"Run: docker exec -it <ollama container> ollama pull {self.model}")

    async def model_available(self) -> bool | None:
        """True if Ollama has the model, False if not, None if Ollama could not be reached."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
            names = {m.get("name") for m in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError):
            return None
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        return wanted in names
