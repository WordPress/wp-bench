"""Model interface leveraging LiteLLM providers."""
from __future__ import annotations

from litellm import completion, completion_cost
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
        response: ModelResponse = completion(
            model=self.config.name,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=self.config.top_p,
            timeout=self.config.request_timeout,
        )
        choice = response.choices[0]
        return choice.message["content"]  # type: ignore[index]

    @staticmethod
    def estimate_cost(response: ModelResponse) -> float:
        return completion_cost(response)
