"""Model interface leveraging LiteLLM providers."""
from __future__ import annotations

from typing import Any

from litellm import completion, completion_cost
from litellm.exceptions import BadRequestError
from litellm.utils import ModelResponse

from .config import ModelConfig


class ModelInterface:
    """Thin wrapper over LiteLLM to keep prompts consistent."""

    def __init__(self, config: ModelConfig):
        self.config = config

    def generate(self, prompt: str) -> str:
        """Generate a completion for the given prompt.

        Args:
            prompt: The user prompt to send to the model.

        Returns:
            The model's response text.
        """
        kwargs = self._completion_kwargs(prompt)
        try:
            response: ModelResponse = completion(**kwargs)
        except BadRequestError as error:
            if not _is_deprecated_temperature_error(error) or "temperature" not in kwargs:
                raise
            kwargs.pop("temperature")
            response = completion(**kwargs)
        choice = response.choices[0]
        return choice.message["content"]  # type: ignore[index]

    @staticmethod
    def estimate_cost(response: ModelResponse) -> float:
        return completion_cost(response)

    def _completion_kwargs(self, prompt: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config.max_tokens,
            "top_p": self.config.top_p,
            "timeout": self.config.request_timeout,
        }
        kwargs["temperature"] = self.config.temperature
        return kwargs


def _is_deprecated_temperature_error(error: BadRequestError) -> bool:
    return "`temperature` is deprecated" in str(error)
