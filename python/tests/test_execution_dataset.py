from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_DIR = PROJECT_ROOT / "datasets" / "suites" / "wp-core-v1" / "execution"

SUPPORTED_ASSERTIONS = {
    "class_exists",
    "custom_assertion",
    "function_exists",
    "hook_registered",
    "option_value",
    "output_contains",
    "output_equals",
    "output_matches",
    "output_not_contains",
    "post_meta_value",
    "query_result",
    "rest_response",
    "returns_value",
    "shortcode_exists",
}


def _execution_suites() -> list[dict[str, Any]]:
    return [orjson.loads(path.read_bytes()) for path in sorted(EXECUTION_DIR.glob("*.json"))]


def _execution_tests() -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    for suite in _execution_suites():
        tests.extend(suite.get("tests", []))
    return tests


def test_execution_suite_has_exactly_150_tests() -> None:
    assert len(_execution_tests()) == 150


def test_execution_test_ids_are_unique() -> None:
    ids = [test["id"] for test in _execution_tests()]
    assert len(ids) == len(set(ids))


def test_execution_tests_have_required_fields() -> None:
    required = {
        "category",
        "difficulty",
        "expected_behavior",
        "id",
        "metadata",
        "prompt",
        "reference_solution",
        "requirements",
        "runtime_checks",
        "static_checks",
    }
    for test in _execution_tests():
        assert required <= test.keys(), test["id"]
        assert "judge_config" not in test, test["id"]
        assert test["reference_solution"].strip(), test["id"]
        assert test["expected_behavior"].strip(), test["id"]
        assert test["expected_behavior"] != test["prompt"], test["id"]
        assert test["requirements"], test["id"]
        assert test["metadata"].get("source_refs"), test["id"]


def test_execution_assertion_types_are_supported() -> None:
    for test in _execution_tests():
        assertions = test.get("runtime_checks", {}).get("assertions", [])
        assert assertions, test["id"]
        for assertion in assertions:
            assert assertion.get("type") in SUPPORTED_ASSERTIONS, test["id"]


def test_execution_suite_includes_modern_wordpress_coverage() -> None:
    modern_tests = [
        test
        for test in _execution_tests()
        if test.get("metadata", {}).get("release_focus") in {"6.9", "7.0"}
    ]
    assert len(modern_tests) >= 35
