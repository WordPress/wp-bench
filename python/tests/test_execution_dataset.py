from __future__ import annotations

import re
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


def test_execution_suite_has_exactly_350_tests() -> None:
    assert len(_execution_tests()) == 350


def test_execution_test_ids_are_unique() -> None:
    ids = [test["id"] for test in _execution_tests()]
    assert len(ids) == len(set(ids))


def test_execution_tests_have_required_fields() -> None:
    required = {
        "category",
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


def test_execution_exploit_solutions_are_wellformed() -> None:
    """exploit_solutions, where present, is a non-empty list of PHP snippets
    that each define the test's gateway function (so the assertions can
    invoke them the same way they invoke a real submission)."""
    for test in _execution_tests():
        exploits = test.get("exploit_solutions")
        if exploits is None:
            continue
        assert isinstance(exploits, list) and exploits, test["id"]
        signature = test.get("test_function") or ""
        name = signature.split("(", 1)[0].strip()
        for code in exploits:
            assert isinstance(code, str) and code.strip(), test["id"]
            if name:
                assert name in code, (
                    f"{test['id']}: exploit does not define gateway {name}"
                )


def test_execution_assertion_types_are_supported() -> None:
    for test in _execution_tests():
        assertions = test.get("runtime_checks", {}).get("assertions", [])
        assert assertions, test["id"]
        for assertion in assertions:
            assert assertion.get("type") in SUPPORTED_ASSERTIONS, test["id"]


def test_execution_test_function_is_valid_signature() -> None:
    """test_function must start with an extractable function name, and that
    name must not be duplicated as a hand-written static pattern."""
    signature_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(")
    for test in _execution_tests():
        signature = test.get("test_function")
        if signature is None:
            continue
        assert signature_re.match(signature.strip()), test["id"]
        name = signature.strip().split("(", 1)[0].strip()
        patterns = test.get("static_checks", {}).get("required_patterns", [])
        duplicates = [p for p in patterns if name in p.get("pattern", "")]
        assert not duplicates, (
            f"{test['id']}: test function {name} is auto-checked; "
            "remove it from static_checks.required_patterns"
        )


def test_execution_gateway_calls_declare_test_function() -> None:
    """A test whose assertions invoke a model-defined wpbp_* function must
    declare it in test_function, and the prompt must not name the gateway —
    test_function is the only channel for the entry-point contract."""
    call_re = re.compile(r"\b(wpbp_[a-z0-9_]+)\s*\(")
    problems: list[str] = []
    for test in _execution_tests():
        assertions = test.get("runtime_checks", {}).get("assertions", [])
        code = " ".join(a.get("code", "") for a in assertions if isinstance(a, dict))
        called = set(call_re.findall(code))
        declared = (test.get("test_function") or "").split("(", 1)[0].strip()
        if called and not declared:
            problems.append(
                f"{test['id']}: assertions call {sorted(called)} but test_function is missing"
            )
        elif called and declared not in called:
            problems.append(
                f"{test['id']}: test_function {declared} is never called; "
                f"assertions call {sorted(called)}"
            )
        if declared and declared in test.get("prompt", ""):
            problems.append(
                f"{test['id']}: prompt names {declared}; test_function owns the naming"
            )
    assert not problems, "\n".join(problems)


def test_execution_suite_includes_modern_wordpress_coverage() -> None:
    modern_tests = [
        test
        for test in _execution_tests()
        if test.get("metadata", {}).get("release_focus") in {"6.9", "7.0", "7.1"}
    ]
    assert len(modern_tests) >= 35
