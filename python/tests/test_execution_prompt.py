"""The execution prompt must tell the model about the grader's load order."""
from __future__ import annotations

from wp_bench.core import EXECUTION_CONTEXT_NOTE, PLUGIN_EXECUTION_CONTEXT_NOTE, BenchmarkRunner
from wp_bench.datasets import ExecutionTest


def _test(artifact_kind: str = "php_snippet") -> ExecutionTest:
    return ExecutionTest(
        id="e-x-001",
        suite="wp-core-v1",
        prompt="Register a thing.",
        expected_behavior="expected",
        category="hooks",
        requirements=["Use the API"],
        test_function="wpbp_x(): void",
        static_checks={},
        runtime_checks={"assertions": []},
        reference_solution="function wpbp_x() {}",
        metadata={},
        artifact_kind=artifact_kind,
    )


def test_prompt_states_that_init_has_already_fired() -> None:
    prompt = BenchmarkRunner._render_execution_prompt(_test())

    assert "init action has already fired" in prompt
    assert prompt.index(EXECUTION_CONTEXT_NOTE) > prompt.index("Define this function")
    assert prompt.rstrip().endswith("```php fences.")


def test_plugin_artifact_prompt_carries_the_plugin_note() -> None:
    prompt = BenchmarkRunner._render_execution_prompt(_test("wp_plugin_files"))

    assert PLUGIN_EXECUTION_CONTEXT_NOTE in prompt
    assert EXECUTION_CONTEXT_NOTE not in prompt
    assert "add_action and add_filter" in prompt
    assert "Plugin Name:" in prompt
