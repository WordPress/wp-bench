from __future__ import annotations

from pathlib import Path

from wp_bench.config import DatasetConfig
from wp_bench.datasets import KnowledgeTest, _parse_knowledge_suite
from wp_bench.knowledge import render_knowledge_prompt, score_knowledge_answer
import wp_bench.datasets as datasets_module


def test_render_knowledge_prompt_uses_choice_instructions_for_multiple_choice() -> None:
    test = KnowledgeTest(
        id="k-rest-002",
        suite="wp-core-v1",
        prompt="Which function is used to register a custom REST API route in WordPress?",
        test_type="multiple_choice",
        category="rest-api",
        difficulty="intermediate",
        choices=[
            {"key": "A", "text": "add_rest_route()"},
            {"key": "B", "text": "register_rest_route()"},
        ],
        correct_answer="B",
    )

    prompt = render_knowledge_prompt(test)

    assert "Choices:" in prompt
    assert "B. register_rest_route()" in prompt
    assert "Answer with only the letter of the correct choice." in prompt


def test_render_knowledge_prompt_uses_short_answer_instructions_without_choices() -> None:
    test = KnowledgeTest(
        id="k-rest-001",
        suite="wp-core-v1",
        prompt="What is the default namespace prefix for WordPress core REST API endpoints?",
        test_type="short_answer",
        category="rest-api",
        difficulty="intermediate",
        correct_answer="wp/v2",
        answer_type="exact",
    )

    prompt = render_knowledge_prompt(test)

    assert "Choices:" not in prompt
    assert "Answer with only the letter" not in prompt
    assert "Answer briefly with the correct WordPress function, API, hook, or value." in prompt


def test_score_multiple_choice_accepts_letter_or_choice_text() -> None:
    test = KnowledgeTest(
        id="k-rest-002",
        suite="wp-core-v1",
        prompt="Which function is used to register a custom REST API route in WordPress?",
        test_type="multiple_choice",
        category="rest-api",
        difficulty="intermediate",
        choices=[
            {"key": "A", "text": "add_rest_route()"},
            {"key": "B", "text": "register_rest_route()"},
        ],
        correct_answer="B",
    )

    assert score_knowledge_answer(test, "B") == 1.0
    assert score_knowledge_answer(test, "The answer is B.") == 1.0
    assert score_knowledge_answer(test, "register_rest_route()") == 1.0
    assert score_knowledge_answer(test, "A. register_rest_route()") == 0.0


def test_score_short_answer_exact_accepts_expected_value_in_prose() -> None:
    test = KnowledgeTest(
        id="k-rest-001",
        suite="wp-core-v1",
        prompt="What is the default namespace prefix for WordPress core REST API endpoints?",
        test_type="short_answer",
        category="rest-api",
        difficulty="intermediate",
        correct_answer="wp/v2",
        answer_type="exact",
    )

    assert score_knowledge_answer(test, "wp/v2") == 1.0
    assert score_knowledge_answer(test, "The answer is wp/v2.") == 1.0


def test_score_short_answer_contains_accepts_function_name_with_parens() -> None:
    test = KnowledgeTest(
        id="k-security-001",
        suite="wp-core-v1",
        prompt="Which function should be used to escape HTML output in WordPress?",
        test_type="short_answer",
        category="security",
        difficulty="basic",
        correct_answer="esc_html",
        answer_type="contains",
    )

    assert score_knowledge_answer(test, "Use esc_html() for this.") == 1.0


def test_local_parser_preserves_short_answer_metadata() -> None:
    suite_path = (
        Path(__file__).resolve().parents[2]
        / "datasets"
        / "suites"
        / "wp-core-v1"
        / "knowledge"
        / "rest-api.json"
    )

    test = _parse_knowledge_suite(suite_path)[0]

    assert test.test_type == "short_answer"
    assert test.answer_type == "exact"
    assert test.choices is None


def test_huggingface_loader_preserves_short_answer_metadata(monkeypatch) -> None:
    rows = [
        {
            "id": "k-rest-001",
            "suite": "wp-core-v1",
            "test_kind": "knowledge",
            "type": "short_answer",
            "prompt": "What is the default namespace prefix for WordPress core REST API endpoints?",
            "category": "rest-api",
            "difficulty": "intermediate",
            "choices": "[]",
            "correct_answer": "wp/v2",
            "answer_type": "exact",
        }
    ]

    monkeypatch.setattr(datasets_module, "hf_load_dataset", lambda *args, **kwargs: rows)

    loaded = datasets_module.load_tests(
        DatasetConfig(source="huggingface", name="WordPress/wp-bench-v1")
    )
    test = loaded["knowledge"][0]

    assert test.test_type == "short_answer"
    assert test.answer_type == "exact"
    assert test.choices is None
