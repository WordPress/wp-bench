from __future__ import annotations

import pytest

from wp_bench.cli import _normalize_test_ids
from wp_bench.datasets import KnowledgeTest, ensure_test_ids_match_type, filter_tests_by_ids


def _knowledge_test(test_id: str) -> KnowledgeTest:
    return KnowledgeTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        test_type="short_answer",
        category="general",
        difficulty="basic",
    )


def test_normalize_test_ids_accepts_repeated_and_comma_separated_values() -> None:
    assert _normalize_test_ids(["e-one,e-two", "e-two", " e-three "]) == [
        "e-one",
        "e-two",
        "e-three",
    ]


def test_filter_tests_by_ids_returns_requested_tests() -> None:
    tests = {
        "knowledge": [_knowledge_test("k-one"), _knowledge_test("k-two")],
        "execution": [],
    }

    filtered = filter_tests_by_ids(tests, ["k-two"])

    assert [test.id for test in filtered["knowledge"]] == ["k-two"]
    assert filtered["execution"] == []


def test_filter_tests_by_ids_rejects_unknown_ids() -> None:
    tests = {"knowledge": [_knowledge_test("k-one")], "execution": []}

    with pytest.raises(ValueError, match="Unknown test id"):
        filter_tests_by_ids(tests, ["missing"])


def test_ensure_test_ids_match_type_rejects_mismatched_explicit_type() -> None:
    tests = {"knowledge": [_knowledge_test("k-one")], "execution": []}

    with pytest.raises(ValueError, match="No execution tests matched"):
        ensure_test_ids_match_type(tests, "execution", ["k-one"])
