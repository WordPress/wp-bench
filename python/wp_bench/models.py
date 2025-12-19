"""Model interface leveraging LiteLLM providers."""
from __future__ import annotations

from typing import List, Sequence

from litellm import completion, completion_cost
from litellm.utils import ModelResponse

from .config import ModelConfig


class ModelInterface:
    """Thin wrapper over LiteLLM to keep prompts consistent."""

    def __init__(self, config: ModelConfig):
        self.config = config

    def generate(self, messages: Sequence[dict]) -> str:
        response: ModelResponse = completion(
            model=self.config.name,
            messages=list(messages),
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=self.config.top_p,
            timeout=self.config.request_timeout,
        )
        choice = response.choices[0]
        return choice.message["content"]  # type: ignore[index]

    @staticmethod
    def to_messages(system_prompt: str, user_prompt: str) -> List[dict]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def estimate_cost(response: ModelResponse) -> float:
        return completion_cost(response)
