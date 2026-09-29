"""The interface every language model backend implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """A provider failed to return a usable answer."""


class LLMProvider(ABC):
    #: True for providers that send data outside the home network.
    is_cloud: bool = False

    def __init__(self, name: str, model: str):
        self.name = name
        self.model = model

    @abstractmethod
    async def complete_json(self, system: str, prompt: str, schema: dict[str, Any], max_tokens: int = 4000) -> dict:
        """Return a dict that follows the given JSON schema."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r}, {self.model!r})"
