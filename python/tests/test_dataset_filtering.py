from __future__ import annotations

import pytest

from wp_bench.cli import _normalize_test_ids
from wp_bench.config import HarnessConfig
from wp_bench.core import select_run_tests
from wp_bench.datasets import ExecutionTest, filter_tests_by_ids


def _execution_test(test_id: str) -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category="general",
        requirements=[],
        test_function=None,
        static_checks={},
        runtime_checks={},
        reference_solution=None,
        metadata={},
    )


def test_normalize_test_ids_accepts_repeated_and_comma_separated_values() -> None:
    assert _normalize_test_ids(["e-one,e-two", "e-two", " e-three "]) == [
        "e-one",
        "e-two",
        "e-three",
    ]


def test_filter_tests_by_ids_returns_requested_tests() -> None:
    tests = [_execution_test("e-one"), _execution_test("e-two")]

    filtered = filter_tests_by_ids(tests, ["e-two"])

    assert [test.id for test in filtered] == ["e-two"]


def test_filter_tests_by_ids_without_ids_returns_all() -> None:
    tests = [_execution_test("e-one"), _execution_test("e-two")]

    assert filter_tests_by_ids(tests, []) is tests


def test_filter_tests_by_ids_rejects_unknown_ids() -> None:
    tests = [_execution_test("e-one")]

    with pytest.raises(ValueError, match="Unknown test id"):
        filter_tests_by_ids(tests, ["missing"])


def test_zero_selected_tests_fails_loudly() -> None:
    """An empty selection (missing suite, execution-less dataset) must not
    produce a vacuous successful run."""
    config = HarnessConfig.model_validate({"dataset": {"source": "local", "name": "wp-core-v1"}})
    with pytest.raises(ValueError, match="No execution tests selected"):
        select_run_tests([], config)


def test_dry_run_zero_selection_fails_loudly(tmp_path) -> None:
    """dry-run on a suite with no execution tests exits with the CLI's
    clear validation error, not a traceback or a successful zero count."""
    from typer.testing import CliRunner

    from wp_bench.cli import app

    config_path = tmp_path / "wp-bench.yaml"
    config_path.write_text(
        "dataset:\n  source: local\n  name: wpbp-no-such-suite\n"
        "run:\n  suite: wpbp-no-such-suite\n  dry_run: true\n"
    )
    result = CliRunner().invoke(app, ["run", "--config", str(config_path), "--dry-run"])
    assert result.exit_code == 1
    assert "No execution tests selected" in result.output


@pytest.mark.parametrize(
    "flags",
    [
        ["--check-reference-solution", "--check-exploits"],
        ["--dry-run", "--check-exploits"],
        ["--dry-run", "--check-reference-solution"],
    ],
)
def test_audit_mode_flags_are_mutually_exclusive(flags: list[str]) -> None:
    from typer.testing import CliRunner

    from wp_bench.cli import app

    result = CliRunner().invoke(app, ["run", *flags])
    assert result.exit_code == 1
    # The console wraps long lines, so compare on collapsed whitespace.
    assert "cannot be combined" in " ".join(result.output.split())


def test_limit_zero_fails_with_the_config_message() -> None:
    from typer.testing import CliRunner

    from wp_bench.cli import app

    result = CliRunner().invoke(app, ["run", "--dry-run", "--limit", "0"])
    assert result.exit_code == 1
    assert "greater than 0" in result.output
