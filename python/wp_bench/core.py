"""Main orchestration loop for WP-Bench."""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import orjson

from .config import HarnessConfig, ModelConfig
from .datasets import AbilityTest, ExecutionTest, KnowledgeTest, load_tests
from .environment import WordPressEnvironment
from .models import ModelInterface
from .output import (
    create_progress,
    print_abort_message,
    print_comparison_table,
    print_model_header,
    print_results_path,
    print_test_error,
)
from .scoring import ScoreAggregator
from .utils import ensure_dir, sha256, strip_code_fences


class TestError(Exception):
    """Wrapper to preserve test context when an error occurs."""

    def __init__(self, test_id: str, test_type: str, original_error: Exception):
        self.test_id = test_id
        self.test_type = test_type
        self.original_error = original_error
        self.traceback_str = traceback.format_exc()
        super().__init__(str(original_error))


def render_abilities_prompt(test: AbilityTest, history: List[str] | None = None) -> str:
    prompt = [test.prompt]
    prompt.append(
        'Return a tool call as JSON: {"ability": "name", "input": {...}}'
    )
    prompt.append(f"Allowed abilities: {', '.join(test.allowed_abilities)}")
    if history:
        prompt.append("History:")
        prompt.extend(history)
    prompt.append("If no tool is needed, answer normally.")
    return "\n".join(prompt)


def parse_tool_call(completion: str) -> Dict[str, Any] | None:
    text = strip_code_fences(completion).strip()
    if not text:
        return None
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            return None
        text = text[start:]
    try:
        data = orjson.loads(text)
        if not isinstance(data, dict):
            return None
        if "ability" not in data:
            return None
        return data
    except orjson.JSONDecodeError:
        end = text.rfind("}")
        if end == -1:
            return None
        try:
            data = orjson.loads(text[: end + 1])
            if not isinstance(data, dict):
                return None
            if "ability" not in data:
                return None
            return data
        except orjson.JSONDecodeError:
            return None


def _tool_call_schema_valid(call: Dict[str, Any]) -> bool:
    if not isinstance(call, dict):
        return False
    ability = call.get("ability")
    if not isinstance(ability, str) or not ability:
        return False
    if "input" in call and call["input"] is not None and not isinstance(call["input"], dict):
        return False
    if "method" in call and call["method"] is not None and not isinstance(call["method"], str):
        return False
    return True


def _looks_like_permission_error(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).lower()
    return any(token in text for token in ("permission", "forbidden", "rest_forbidden", "rest_cannot"))


def _observation_bool(observation: Dict[str, Any], key: str) -> bool | None:
    if key not in observation:
        return None
    return bool(observation.get(key))


def _observation_ability_found(observation: Dict[str, Any]) -> bool:
    value = _observation_bool(observation, "ability_found")
    if value is not None:
        return value
    if observation.get("error") == "ability_not_found":
        return False
    return bool(observation.get("success"))


def _observation_method_valid(observation: Dict[str, Any]) -> bool:
    value = _observation_bool(observation, "method_valid")
    if value is not None:
        return value
    if observation.get("error") == "invalid_method":
        return False
    return True


def _observation_permission_ok(observation: Dict[str, Any]) -> bool:
    value = _observation_bool(observation, "permission_ok")
    if value is not None:
        return value
    if _looks_like_permission_error(observation.get("error")):
        return False
    if observation.get("success") is True:
        return True
    return True


def _observation_confirmation_ok(observation: Dict[str, Any]) -> bool:
    value = _observation_bool(observation, "confirmation_ok")
    if value is not None:
        return value
    if observation.get("confirmation_required"):
        return False
    return True


def _all_observations(
    observations: List[Dict[str, Any]],
    predicate,
    require: bool = True,
) -> bool:
    if not observations:
        return False if require else True
    return all(predicate(obs) for obs in observations)


def _match_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            if key not in actual:
                return False
            if not _match_subset(value, actual[key]):
                return False
        return True
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        if len(expected) > len(actual):
            return False
        for idx, value in enumerate(expected):
            if not _match_subset(value, actual[idx]):
                return False
        return True
    return expected == actual


def _extract_expected_match(expected_entry: Any, observation: Dict[str, Any]) -> bool:
    if isinstance(expected_entry, dict):
        if "match" in expected_entry:
            expected_value = expected_entry["match"]
            target = observation.get("result", observation)
            return _match_subset(expected_value, target)
        if "result" in expected_entry:
            expected_value = expected_entry["result"]
            target = observation.get("result")
            return _match_subset(expected_value, target)
        if "equals" in expected_entry:
            return _match_subset(expected_entry["equals"], observation)
    return _match_subset(expected_entry, observation.get("result", observation))


def _check_expected_outputs(
    expected_outputs: Any,
    tool_calls: List[Dict[str, Any]],
    observations: List[Dict[str, Any]],
) -> bool:
    if expected_outputs is None:
        return True
    if not isinstance(expected_outputs, list):
        return False
    if not expected_outputs:
        return True
    if not observations:
        return False
    for idx, expected_entry in enumerate(expected_outputs):
        if idx >= len(observations):
            return False
        observation = observations[idx]
        if isinstance(expected_entry, dict) and "ability" in expected_entry:
            if idx >= len(tool_calls):
                return False
            if tool_calls[idx].get("ability") != expected_entry.get("ability"):
                return False
        if not _extract_expected_match(expected_entry, observation):
            return False
    return True


def _normalize_state_checks(expected_state: Any) -> List[Dict[str, Any]]:
    if expected_state is None:
        return []
    if isinstance(expected_state, list):
        return [item for item in expected_state if isinstance(item, dict)]
    if isinstance(expected_state, dict):
        checks = expected_state.get("checks")
        if isinstance(checks, list):
            return [item for item in checks if isinstance(item, dict)]
        if "ability" in expected_state:
            return [expected_state]
    return []


def _run_state_checks(
    expected_state: Any, environment: WordPressEnvironment
) -> List[Dict[str, Any]]:
    checks = _normalize_state_checks(expected_state)
    results: List[Dict[str, Any]] = []
    for check in checks:
        ability = check.get("ability")
        if not isinstance(ability, str) or not ability:
            results.append(
                {
                    "ability": None,
                    "success": False,
                    "match": False,
                    "error": "missing_ability",
                }
            )
            continue
        input_data = check.get("input")
        method = check.get("method")
        env_result = environment.execute_ability(
            ability=ability,
            input_data=input_data,
            method=method if isinstance(method, str) else None,
        )
        observation = env_result.raw if env_result.raw else {"success": False, "error": "no_result"}
        if "match" in check:
            match_ok = _match_subset(check["match"], observation.get("result", observation))
        elif "result" in check:
            match_ok = _match_subset(check["result"], observation.get("result"))
        elif "equals" in check:
            match_ok = _match_subset(check["equals"], observation)
        else:
            match_ok = bool(observation.get("success"))
        results.append(
            {
                "ability": ability,
                "input": input_data,
                "method": method,
                "observation": observation,
                "match": match_ok,
            }
        )
    return results


def score_abilities(
    test: AbilityTest,
    tool_calls: List[Dict[str, Any]],
    observations: List[Dict[str, Any]],
    final_answer: str,
    state_checks: List[Dict[str, Any]] | None = None,
) -> tuple[float, Dict[str, Any]]:
    results: Dict[str, Any] = {}
    has_tool_calls = len(tool_calls) > 0
    results["tool_calls_present"] = has_tool_calls
    results["schema_validity"] = has_tool_calls and all(_tool_call_schema_valid(t) for t in tool_calls)
    results["allowed_abilities_only"] = all(t.get("ability") in test.allowed_abilities for t in tool_calls)
    results["max_steps_respected"] = len(tool_calls) <= test.max_steps
    results["ability_success"] = _all_observations(observations, lambda obs: bool(obs.get("success")))
    results["ability_found"] = _all_observations(observations, _observation_ability_found)
    results["method_valid"] = _all_observations(observations, _observation_method_valid)
    results["permission_ok"] = _all_observations(observations, _observation_permission_ok)
    results["confirmation_ok"] = _all_observations(observations, _observation_confirmation_ok)

    results["expected_outputs_match"] = _check_expected_outputs(
        test.expected_outputs, tool_calls, observations
    )
    if "expected_output_match" in test.verifiers:
        results["expected_output_match"] = results["expected_outputs_match"]

    if test.expected_state is not None:
        if state_checks:
            results["expected_state_match"] = all(check.get("match") for check in state_checks)
        else:
            results["expected_state_match"] = False
    else:
        results["expected_state_match"] = True

    enabled = [k for k in results.keys() if k in test.verifiers]
    if not enabled:
        return (1.0, results)
    score = sum(1.0 if results[k] else 0.0 for k in enabled) / len(enabled)
    return (round(score, 4), results)


def _timestamped_path(path: Path) -> Path:
    """Add timestamp to filename: results.json -> results_20231216_143052.json"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.parent / f"{path.stem}_{timestamp}{path.suffix}"


class BenchmarkRunner:
    """Primary benchmark orchestrator for single-model evaluation.

    Loads tests from the configured dataset, runs them against a single LLM,
    executes generated code in a WordPress environment, and aggregates scores.
    """

    def __init__(self, config: HarnessConfig):
        """Initialize the runner with harness configuration.

        Args:
            config: Full harness configuration including model, grader, and output settings.
        """
        self.config = config
        self.model = ModelInterface(config.model)
        self.environment = WordPressEnvironment(config.grader)
        self.aggregator = ScoreAggregator()
        self.records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def run(self) -> Dict[str, Any]:
        """Execute the full benchmark pipeline.

        Loads tests, sets up the WordPress environment, runs knowledge and execution
        tests in parallel, computes aggregate scores, and writes results to disk.

        Returns:
            Dict containing metadata (scores, config) and individual test results.

        Raises:
            SystemExit: If a test fails, prints error details and exits with code 1.
        """
        tests = load_tests(self.config.dataset)
        self.environment.setup()
        try:
            self._run_knowledge_tests(tests["knowledge"])
            self._run_execution_tests(tests["execution"])
            if "abilities" in tests:
                self._run_abilities_tests(tests["abilities"])
        except TestError as e:
            print_test_error(e)
            raise SystemExit(1) from e
        except KeyboardInterrupt:
            print_abort_message()
            raise SystemExit(130) from None
        summary = self.aggregator.finalize()
        payload = {
            "metadata": {
                "suite": self.config.run.suite,
                "model": self.config.model.model_dump(mode="json"),
                "grader": self.config.grader.model_dump(mode="json"),
                "dataset": self.config.dataset.model_dump(mode="json"),
                "scores": {
                    "knowledge": summary.knowledge,
                    "correctness": summary.correctness,
                    "quality": summary.quality,
                    "abilities": summary.abilities,
                    "overall": summary.overall(),
                },
            },
            "results": self.records,
        }
        self._write_outputs(payload)
        return payload

    def _run_knowledge_tests(self, tests: List[KnowledgeTest]) -> None:
        """Run multiple-choice knowledge tests in parallel.

        Prompts the model with WordPress knowledge questions and scores responses
        based on whether they match the expected answer letter.

        Args:
            tests: List of knowledge test definitions.

        Raises:
            TestError: If any test fails, stops execution and raises with details.
        """
        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: KnowledgeTest) -> Dict[str, Any]:
            try:
                prompt = self._render_knowledge_prompt(test)
                answer = strip_code_fences(self.model.generate(prompt)).strip()
                correct = 1.0 if (test.correct_answer and answer.upper().startswith(test.correct_answer)) else 0.0
                return {
                    "test_id": test.id,
                    "type": "knowledge",
                    "prompt_hash": sha256(prompt),
                    "answer": answer,
                    "correct": bool(correct),
                    "score": correct,
                }
            except Exception as e:
                raise TestError(test.id, "knowledge", e) from e

        with create_progress() as progress:
            task = progress.add_task("Knowledge", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        with self._lock:
                            self.aggregator.add_knowledge(result["score"])
                            self.records.append(result)
                        progress.update(task, advance=1)
                    except TestError:
                        for f in futures:
                            f.cancel()
                        raise

    def _run_execution_tests(self, tests: List[ExecutionTest]) -> None:
        """Run code generation execution tests in parallel.

        Prompts the model to generate PHP code, executes it in the WordPress
        environment, and scores based on static/runtime assertions.

        Args:
            tests: List of execution test definitions.

        Raises:
            TestError: If any test fails, stops execution and raises with details.
        """
        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: ExecutionTest) -> Dict[str, Any]:
            """Process a single execution test (runs in thread pool)."""
            try:
                prompt = self._render_execution_prompt(test)
                completion = self.model.generate(prompt)
                code = strip_code_fences(completion)
                verification_spec = {
                    "static_checks": test.static_checks,
                    "runtime_checks": test.runtime_checks,
                    "judge_config": test.judge_config,
                }
                env_result = self.environment.execute_code(code, verification_spec)
                correctness = self._score_assertions(env_result.raw)
                quality = env_result.raw.get("quality", {}).get("score") if env_result.raw else None
                return {
                    "test_id": test.id,
                    "type": "execution",
                    "prompt_hash": sha256(prompt),
                    "code": code,
                    "result": env_result.raw,
                    "stdout": env_result.stdout,
                    "stderr": env_result.stderr,
                    "correctness": correctness,
                    "quality": quality,
                }
            except Exception as e:
                raise TestError(test.id, "execution", e) from e

        with create_progress() as progress:
            task = progress.add_task("Execution", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        with self._lock:
                            self.aggregator.add_execution(result["correctness"], result["quality"])
                            self.records.append(result)
                        progress.update(task, advance=1)
                    except TestError:
                        for f in futures:
                            f.cancel()
                        raise

    def _run_abilities_tests(self, tests: List[AbilityTest]) -> None:
        """Run tool-use ability tests in parallel."""
        if not tests:
            return

        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: AbilityTest) -> Dict[str, Any]:
            try:
                history: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                observations: List[Dict[str, Any]] = []
                trace: List[Dict[str, Any]] = []
                final_answer = ""

                setup = test.fixtures.get("setup") if isinstance(test.fixtures, dict) else None
                teardown = test.fixtures.get("teardown") if isinstance(test.fixtures, dict) else None

                for step in range(test.max_steps):
                    prompt = render_abilities_prompt(test, history=history)
                    completion = self.model.generate(prompt)
                    trace_step: Dict[str, Any] = {
                        "step": step,
                        "prompt": prompt,
                        "completion": completion,
                    }
                    parsed = parse_tool_call(completion)
                    if parsed is None:
                        final_answer = completion
                        trace_step["tool_call"] = None
                        trace.append(trace_step)
                        break

                    tool_calls.append(parsed)
                    result = self.environment.execute_ability(
                        ability=parsed.get("ability", ""),
                        input_data=parsed.get("input"),
                        method=parsed.get("method"),
                        setup=setup if step == 0 else None,
                        teardown=teardown if step == test.max_steps - 1 else None,
                    )
                    observation = result.raw if result.raw else {"success": False, "error": "no_result"}
                    observations.append(observation)
                    trace_step["tool_call"] = parsed
                    trace_step["observation"] = observation
                    trace.append(trace_step)
                    history.append(f"TOOL_CALL: {parsed}")
                    history.append(f"OBSERVATION: {observation}")
                else:
                    final_answer = ""

                state_checks = _run_state_checks(test.expected_state, self.environment) if test.expected_state else []
                score, verifier_results = score_abilities(
                    test,
                    tool_calls,
                    observations,
                    final_answer,
                    state_checks=state_checks,
                )

                return {
                    "test_id": test.id,
                    "type": "abilities",
                    "prompt_hash": sha256(render_abilities_prompt(test)),
                    "tool_calls": tool_calls,
                    "observations": observations,
                    "trace": trace,
                    "state_checks": state_checks,
                    "final_answer": final_answer,
                    "verifiers": verifier_results,
                    "score": score,
                }
            except Exception as e:
                raise TestError(test.id, "abilities", e) from e

        with create_progress() as progress:
            task = progress.add_task("Abilities", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        with self._lock:
                            self.aggregator.add_abilities(result["score"])
                            self.records.append(result)
                        progress.update(task, advance=1)
                    except TestError:
                        for f in futures:
                            f.cancel()
                        raise

    @staticmethod
    def _render_knowledge_prompt(test: KnowledgeTest) -> str:
        """Format a knowledge test into a multiple-choice prompt string.

        Args:
            test: Knowledge test with question and choices.

        Returns:
            Formatted prompt asking for a single letter answer.
        """
        prompt = [test.prompt]
        if test.choices:
            prompt.append("Choices:")
            for choice in test.choices:
                prompt.append(f"{choice['key']}. {choice['text']}")
        prompt.append("Answer with only the letter of the correct choice.")
        return "\n".join(prompt)

    @staticmethod
    def _render_execution_prompt(test: ExecutionTest) -> str:
        """Format an execution test into a code generation prompt.

        Args:
            test: Execution test with task description and requirements.

        Returns:
            Formatted prompt requesting PHP code in fenced blocks.
        """
        lines = [test.prompt, "", "Requirements:"]
        for req in test.requirements:
            lines.append(f"- {req}")
        lines.append(
            "Return only valid PHP code without explanations. Wrap the response in ```php fences."
        )
        return "\n".join(lines)

    @staticmethod
    def _score_assertions(raw: Dict[str, Any]) -> float:
        """Calculate correctness score from assertion results.

        Args:
            raw: Raw result dict from WordPress environment containing assertions.

        Returns:
            Float between 0.0 and 1.0 representing fraction of passed assertions.
        """
        assertions = raw.get("assertions") or []
        if not assertions:
            return 0.0
        passed = sum(1 for assertion in assertions if assertion.get("passed"))
        return round(passed / len(assertions), 4)

    def _write_outputs(self, payload: Dict[str, Any]) -> None:
        """Write benchmark results to JSON and JSONL files.

        Args:
            payload: Complete results dict with metadata and test records.
        """
        output_path = _timestamped_path(self.config.output.path)
        ensure_dir(output_path.parent)
        output_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        print_results_path(output_path)
        if self.config.output.jsonl_path:
            jsonl_path = _timestamped_path(self.config.output.jsonl_path)
            ensure_dir(jsonl_path.parent)
            with jsonl_path.open("w", encoding="utf-8") as handle:
                for record in self.records:
                    handle.write(orjson.dumps(record).decode("utf-8"))
                    handle.write("\n")


class MultiModelRunner:
    """Run benchmarks across multiple models and produce a comparison table.

    Iterates over all configured models, runs the full test suite for each using
    SingleModelRunner, and outputs a side-by-side comparison of scores.
    """

    def __init__(self, config: HarnessConfig):
        """Initialize the multi-model runner.

        Args:
            config: Harness configuration with multiple models defined.
        """
        self.config = config
        self.environment = WordPressEnvironment(config.grader)
        self.results: Dict[str, Dict[str, Any]] = {}

    def run(self) -> Dict[str, Any]:
        """Execute benchmarks for all configured models.

        Sets up the WordPress environment once, then runs each model sequentially.
        Prints a comparison table and writes combined results.

        Returns:
            Dict mapping model names to their individual results.

        Raises:
            SystemExit: If a test fails, prints error details and exits with code 1.
        """
        models = self.config.get_models()
        tests = load_tests(self.config.dataset)
        self.environment.setup()

        try:
            for model_config in models:
                model_name = model_config.name
                print_model_header(model_name)

                runner = SingleModelRunner(
                    config=self.config,
                    model_config=model_config,
                    environment=self.environment,
                    tests=tests,
                )
                result = runner.run()
                self.results[model_name] = result
        except TestError as e:
            print_test_error(e)
            raise SystemExit(1) from e
        except KeyboardInterrupt:
            print_abort_message()
            raise SystemExit(130) from None

        print_comparison_table(self.results)
        self._write_outputs()
        return self.results

    def _write_outputs(self) -> None:
        """Write combined results to output files."""
        payload = {
            "metadata": {
                "suite": self.config.run.suite,
                "grader": self.config.grader.model_dump(mode="json"),
                "dataset": self.config.dataset.model_dump(mode="json"),
            },
            "models": {
                name: {
                    "config": result["model_config"],
                    "scores": result["scores"],
                    "results": result["results"],
                }
                for name, result in self.results.items()
            },
        }
        output_path = _timestamped_path(self.config.output.path)
        ensure_dir(output_path.parent)
        output_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        print_results_path(output_path)


class SingleModelRunner:
    """Run benchmark for a single model with pre-loaded tests.

    Used by MultiModelRunner to evaluate one model at a time while sharing
    the WordPress environment and test definitions across models.
    """

    def __init__(
        self,
        config: HarnessConfig,
        model_config: ModelConfig,
        environment: WordPressEnvironment,
        tests: Dict[str, List[Any]],
    ):
        """Initialize runner for a specific model.

        Args:
            config: Harness configuration for run settings.
            model_config: Configuration for the specific model to evaluate.
            environment: Shared WordPress environment instance.
            tests: Pre-loaded dict of knowledge and execution tests.
        """
        self.config = config
        self.model_config = model_config
        self.model = ModelInterface(model_config)
        self.environment = environment
        self.tests = tests
        self.aggregator = ScoreAggregator()
        self.records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def run(self) -> Dict[str, Any]:
        """Run all tests and return scores for this model.

        Returns:
            Dict with model config, aggregate scores, and individual results.
        """
        self._run_knowledge_tests(self.tests["knowledge"])
        self._run_execution_tests(self.tests["execution"])
        if "abilities" in self.tests:
            self._run_abilities_tests(self.tests["abilities"])
        summary = self.aggregator.finalize()
        return {
            "model_config": self.model_config.model_dump(mode="json"),
            "scores": {
                "knowledge": summary.knowledge,
                "correctness": summary.correctness,
                "quality": summary.quality,
                "abilities": summary.abilities,
                "overall": summary.overall(),
            },
            "results": self.records,
        }

    def _run_knowledge_tests(self, tests: List[KnowledgeTest]) -> None:
        """Run knowledge tests in parallel. See BenchmarkRunner._run_knowledge_tests."""
        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: KnowledgeTest) -> Dict[str, Any]:
            try:
                prompt = BenchmarkRunner._render_knowledge_prompt(test)
                answer = strip_code_fences(self.model.generate(prompt)).strip()
                correct = 1.0 if (test.correct_answer and answer.upper().startswith(test.correct_answer)) else 0.0
                return {
                    "test_id": test.id,
                    "type": "knowledge",
                    "answer": answer,
                    "correct": bool(correct),
                    "score": correct,
                }
            except Exception as e:
                raise TestError(test.id, "knowledge", e) from e

        with create_progress() as progress:
            task = progress.add_task("Knowledge", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        with self._lock:
                            self.aggregator.add_knowledge(result["score"])
                            self.records.append(result)
                        progress.update(task, advance=1)
                    except TestError:
                        for f in futures:
                            f.cancel()
                        raise

    def _run_execution_tests(self, tests: List[ExecutionTest]) -> None:
        """Run execution tests in parallel. See BenchmarkRunner._run_execution_tests."""
        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: ExecutionTest) -> Dict[str, Any]:
            try:
                prompt = BenchmarkRunner._render_execution_prompt(test)
                completion = self.model.generate(prompt)
                code = strip_code_fences(completion)
                verification_spec = {
                    "static_checks": test.static_checks,
                    "runtime_checks": test.runtime_checks,
                    "judge_config": test.judge_config,
                }
                env_result = self.environment.execute_code(code, verification_spec)
                correctness = BenchmarkRunner._score_assertions(env_result.raw)
                quality = env_result.raw.get("quality", {}).get("score") if env_result.raw else None
                return {
                    "test_id": test.id,
                    "type": "execution",
                    "code": code,
                    "correctness": correctness,
                    "quality": quality,
                }
            except Exception as e:
                raise TestError(test.id, "execution", e) from e

        with create_progress() as progress:
            task = progress.add_task("Execution", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        with self._lock:
                            self.aggregator.add_execution(result["correctness"], result["quality"])
                            self.records.append(result)
                        progress.update(task, advance=1)
                    except TestError:
                        for f in futures:
                            f.cancel()
                        raise

    def _run_abilities_tests(self, tests: List[AbilityTest]) -> None:
        """Run tool-use ability tests in parallel."""
        if not tests:
            return

        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: AbilityTest) -> Dict[str, Any]:
            try:
                history: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                observations: List[Dict[str, Any]] = []
                trace: List[Dict[str, Any]] = []
                final_answer = ""

                setup = test.fixtures.get("setup") if isinstance(test.fixtures, dict) else None
                teardown = test.fixtures.get("teardown") if isinstance(test.fixtures, dict) else None

                for step in range(test.max_steps):
                    prompt = render_abilities_prompt(test, history=history)
                    completion = self.model.generate(prompt)
                    trace_step: Dict[str, Any] = {
                        "step": step,
                        "prompt": prompt,
                        "completion": completion,
                    }
                    parsed = parse_tool_call(completion)
                    if parsed is None:
                        final_answer = completion
                        trace_step["tool_call"] = None
                        trace.append(trace_step)
                        break

                    tool_calls.append(parsed)
                    result = self.environment.execute_ability(
                        ability=parsed.get("ability", ""),
                        input_data=parsed.get("input"),
                        method=parsed.get("method"),
                        setup=setup if step == 0 else None,
                        teardown=teardown if step == test.max_steps - 1 else None,
                    )
                    observation = result.raw if result.raw else {"success": False, "error": "no_result"}
                    observations.append(observation)
                    trace_step["tool_call"] = parsed
                    trace_step["observation"] = observation
                    trace.append(trace_step)
                    history.append(f"TOOL_CALL: {parsed}")
                    history.append(f"OBSERVATION: {observation}")
                else:
                    final_answer = ""

                state_checks = _run_state_checks(test.expected_state, self.environment) if test.expected_state else []
                score, verifier_results = score_abilities(
                    test,
                    tool_calls,
                    observations,
                    final_answer,
                    state_checks=state_checks,
                )

                return {
                    "test_id": test.id,
                    "type": "abilities",
                    "prompt_hash": sha256(render_abilities_prompt(test)),
                    "tool_calls": tool_calls,
                    "observations": observations,
                    "trace": trace,
                    "state_checks": state_checks,
                    "final_answer": final_answer,
                    "verifiers": verifier_results,
                    "score": score,
                }
            except Exception as e:
                raise TestError(test.id, "abilities", e) from e

        with create_progress() as progress:
            task = progress.add_task("Abilities", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        with self._lock:
                            self.aggregator.add_abilities(result["score"])
                            self.records.append(result)
                        progress.update(task, advance=1)
                    except TestError:
                        for f in futures:
                            f.cancel()
                        raise
