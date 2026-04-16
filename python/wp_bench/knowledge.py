"""Knowledge prompt rendering and scoring helpers."""
from __future__ import annotations

import re

from .datasets import KnowledgeTest
from .utils import strip_code_fences

_CHOICE_ANSWER_RE = re.compile(
    r"^\s*(?:(?:the\s+)?(?:correct\s+)?(?:answer|option|choice)\s*(?:is|:)\s*)?"
    r"[\(\[]?([A-Z])(?:[\)\].:\-,\s]|$)",
    re.IGNORECASE,
)


def render_knowledge_prompt(test: KnowledgeTest) -> str:
    """Format a knowledge test prompt based on its answer mode."""
    prompt = [test.prompt]
    if test.choices:
        prompt.append("Choices:")
        for choice in test.choices:
            prompt.append(f"{choice['key']}. {choice['text']}")
        prompt.append("Answer with only the letter of the correct choice.")
    else:
        prompt.append("Answer briefly with the correct WordPress function, API, hook, or value.")
    return "\n".join(prompt)


def score_knowledge_answer(test: KnowledgeTest, answer: str) -> float:
    """Score a model response for a knowledge test."""
    if not test.correct_answer:
        return 0.0

    if test.choices:
        correct_key = test.correct_answer.upper()
        answer_key = _extract_choice_key(answer)
        if answer_key is not None:
            return 1.0 if answer_key == correct_key else 0.0

        correct_choice_text = _lookup_choice_text(test, correct_key)
        if not correct_choice_text:
            return 0.0

        answer_normalized = _normalize_knowledge_text(answer)
        choice_normalized = _normalize_knowledge_text(correct_choice_text)
        return 1.0 if choice_normalized and choice_normalized in answer_normalized else 0.0

    expected = _normalize_knowledge_text(test.correct_answer)
    actual = _normalize_knowledge_text(answer)
    if not expected or not actual:
        return 0.0

    if actual == expected:
        return 1.0

    answer_type = (test.answer_type or "exact").lower()
    if answer_type in {"contains", "exact"} and expected in actual:
        return 1.0

    return 0.0


def _extract_choice_key(answer: str) -> str | None:
    text = strip_code_fences(answer).strip()
    if not text:
        return None
    match = _CHOICE_ANSWER_RE.match(text)
    if not match:
        return None
    return match.group(1).upper()


def _lookup_choice_text(test: KnowledgeTest, key: str) -> str | None:
    if not test.choices:
        return None
    for choice in test.choices:
        choice_key = str(choice.get("key", "")).upper()
        if choice_key == key:
            text = choice.get("text")
            return text if isinstance(text, str) else None
    return None


def _normalize_knowledge_text(value: str) -> str:
    text = strip_code_fences(value).strip().casefold()
    return re.sub(r"\s+", " ", text)
