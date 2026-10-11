"""Ollama backend, using its native /api/chat endpoint with a JSON schema as format."""

from __future__ import annotations

import json

import httpx

from .base import LLMError, LLMOutputError, LLMProvider


class OllamaProvider(LLMProvider):
    def __init__(self, name: str, model: str, base_url: str = "http://localhost:11434", timeout: float = 600.0,
                 max_context: int = 16384):
        super().__init__(name, model)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_context = max_context

    def context_size(self, system: str, prompt: str, max_tokens: int) -> int:
        """Room for the prompt and the answer. Ollama's default window is small and silently cuts answers off."""
        prompt_tokens = (len(system) + len(prompt)) // 3 + 256  # about 3 characters per token in Swedish
        needed = prompt_tokens + max_tokens
        return min(self.max_context, -(-needed // 1024) * 1024)

    async def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens,
                        "num_ctx": self.context_size(system, prompt, max_tokens)},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=body)
                if response.status_code == 404:
                    raise LLMError(f"{self.name}: {self.missing_model_hint()}")
                response.raise_for_status()
            data = response.json()
            content = data["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMError(f"{self.name}: {exc}") from exc
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            cut = " (the answer was cut off at the length limit)" if data.get("done_reason") == "length" else ""
            raise LLMOutputError(f"{self.name}: invalid JSON{cut}: {exc}") from exc

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
