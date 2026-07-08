"""Main orchestration loop for WP-Bench."""
from __future__ import annotations

import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import orjson

from .config import HarnessConfig, ModelConfig
from .datasets import (
    ExecutionTest,
    KnowledgeTest,
    ensure_test_ids_match_type,
    filter_tests_by_ids,
    load_tests,
)
from .environment import WordPressEnvironment
from .knowledge import render_knowledge_prompt, score_knowledge_answer
from .models import ModelInterface
from .output import (
    create_progress,
    print_abort_message,
    print_comparison_table,
    print_model_header,
    print_reference_solution_failures,
    print_results_path,
    print_test_error,
)
from .records import (
    RESULT_SCHEMA_VERSION,
    build_execution_record,
    build_knowledge_record,
    execution_record_passed,
    sort_records,
)
from .scoring import SCORING_VERSION, ScoreAggregator, UsageAggregator
from .selection import select_tests
from .utils import ensure_dir, sha256, strip_code_fences


class TestError(Exception):
    """Wrapper to preserve test context when an error occurs."""

    def __init__(self, test_id: str, test_type: str, original_error: Exception):
        self.test_id = test_id
        self.test_type = test_type
        self.original_error = original_error
        self.traceback_str = traceback.format_exc()
        super().__init__(str(original_error))


def _timestamped_path(path: Path) -> Path:
    """Add timestamp to filename: results.json -> results_20231216_143052.json"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.parent / f"{path.stem}_{timestamp}{path.suffix}"


def _limit_tests(tests: List[Any], config: HarnessConfig) -> List[Any]:
    """Select tests for this run (seeded stratified selection when limited)."""
    return select_tests(
        tests,
        limit=config.run.limit,
        test_ids=config.run.test_ids,
        seed=config.run.seed,
    )


def _build_verification_spec(test: Any, config: HarnessConfig) -> Dict[str, Any]:
    """Build the verifier payload honoring run.skip_runtime/skip_static.

    A skipped dimension is sent as empty checks so the runtime does not
    execute it; scoring treats it as not applicable (see _score_correctness).

    When runtime checks run, a weight-0 ``function_exists`` assertion derived
    from ``test.test_function`` is prepended. It is the harness's gateway for
    invoking the submission -- shown to the model and checked at runtime, but
    earning no score. A missing gateway fails the behavioral assertions on its
    own; the derived check only makes that failure diagnosable.
    """
    static_checks = {} if config.run.skip_static else test.static_checks
    if config.run.skip_runtime:
        runtime_checks: Dict[str, Any] = {}
    else:
        runtime_checks = dict(test.runtime_checks)
        if test.test_function:
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", test.test_function.strip())
            if not match:
                raise ValueError(
                    f"Cannot extract function name from test_function "
                    f"signature: {test.test_function!r}"
                )
            name = match.group(0)
            derived = {
                "type": "function_exists",
                "target": name,
                "description": f"Defines test function {name}()",
                "weight": 0,
            }
            existing = runtime_checks.get("assertions", [])
            runtime_checks["assertions"] = [derived, *existing]
    return {
        "static_checks": static_checks,
        "runtime_checks": runtime_checks,
    }


def _model_call_info(generation: Any) -> Dict[str, Any]:
    """Call-reliability metadata recorded alongside each test result."""
    return {
        "retry_count": generation.retry_count,
        "provider_response_id": generation.provider_response_id,
        "temperature_fallback": generation.temperature_fallback,
    }


def _run_isolated_execution_loop(
    *,
    tests_to_run: List[Any],
    config: HarnessConfig,
    environment: WordPressEnvironment,
    process_test: Any,
    on_result: Any,
    progress_label: str,
) -> None:
    """Run execution-style tests honoring the configured isolation strategy.

    ``reset_per_test`` (default): tests run serially and the WordPress
    environment is reset to a known baseline before every test, so no test
    can observe state (options, posts, roles, hooks persisted to DB, etc.)
    left behind by a previous test or a previous model run.

    ``none``: legacy concurrent behavior against a shared environment,
    bounded by ``run.execution_concurrency``. Not valid for official runs.

    Args:
        tests_to_run: Tests to execute, already limited/filtered.
        config: Harness configuration (isolation strategy, concurrency).
        environment: WordPress environment shared by this run.
        process_test: Callable taking a test and returning a record dict.
        on_result: Callable invoked with each record (aggregation/appending).
        progress_label: Label for the progress bar.
    """
    isolation = config.run.execution_isolation
    with create_progress() as progress:
        task = progress.add_task(progress_label, total=len(tests_to_run))
        if isolation == "reset_per_test":
            for test in tests_to_run:
                environment.reset()
                result = process_test(test)
                on_result(result)
                progress.update(task, advance=1)
            return
        with ThreadPoolExecutor(max_workers=config.run.execution_concurrency) as executor:
            futures = {executor.submit(process_test, test): test for test in tests_to_run}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    on_result(result)
                    progress.update(task, advance=1)
                except TestError:
                    for f in futures:
                        f.cancel()
                    raise


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
        self.model = ModelInterface(config.model or config.get_models()[0])
        self.environment = WordPressEnvironment(config.grader)
        self.aggregator = ScoreAggregator()
        self.usage_aggregator = UsageAggregator()
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
        tests = filter_tests_by_ids(load_tests(self.config.dataset), self.config.run.test_ids)
        test_type = self.config.run.test_type
        reference_mode = self.config.run.check_reference_solution
        if reference_mode:
            if test_type == "knowledge":
                raise ValueError("--check-reference-solution only supports execution tests")
            self._ensure_reference_solution_ids_are_execution_tests(tests)
            run_knowledge = False
            run_execution = True
        else:
            ensure_test_ids_match_type(tests, test_type, self.config.run.test_ids)
            run_knowledge = test_type in (None, "knowledge")
            run_execution = test_type in (None, "execution")
        if run_execution:
            self.environment.setup()
        try:
            if run_knowledge:
                self._run_knowledge_tests(tests["knowledge"])
            if reference_mode:
                self._run_reference_solution_tests(tests["execution"])
            elif run_execution:
                self._run_execution_tests(tests["execution"])
        except TestError as e:
            print_test_error(e)
            raise SystemExit(1) from e
        except KeyboardInterrupt:
            print_abort_message()
            raise SystemExit(130) from None
        summary = self.aggregator.finalize()
        model_config = self.config.model.model_dump(mode="json") if self.config.model else None
        payload = {
            "metadata": {
                "suite": self.config.run.suite,
                "mode": "reference_solution" if reference_mode else "model",
                "result_schema_version": RESULT_SCHEMA_VERSION,
                "model": model_config,
                "grader": self.config.grader.model_dump(mode="json"),
                "dataset": self.config.dataset.model_dump(mode="json"),
                "runtime_isolation": self.config.run.execution_isolation,
                "scoring_version": SCORING_VERSION,
                "seed": self.config.run.seed,
                "limit": self.config.run.limit,
                "selected_test_ids": sorted(
                    {record["test_id"] for record in self.records}
                ),
                "usage": self.usage_aggregator.summary(),
                "scores": {
                    "knowledge": summary.knowledge,
                    "execution_pass_rate": summary.execution_pass_rate,
                    "runtime": summary.runtime,
                    "static_policy_pass_rate": summary.static_policy_pass_rate,
                    "correctness": summary.correctness,
                    "overall": summary.overall(),
                },
            },
            "results": sort_records(self.records),
        }
        self._write_outputs(payload)
        if reference_mode:
            failures = [
                record for record in self.records if not execution_record_passed(record)
            ]
            if failures:
                print_reference_solution_failures(failures)
                raise SystemExit(1)
        return payload

    def _ensure_reference_solution_ids_are_execution_tests(
        self,
        tests: Dict[str, List[Any]],
    ) -> None:
        """Ensure reference-solution mode is scoped to execution tests."""
        selected_ids = self.config.run.test_ids
        if not selected_ids:
            return

        execution_ids = {test.id for test in tests["execution"]}
        non_execution_ids = [test_id for test_id in selected_ids if test_id not in execution_ids]
        if non_execution_ids:
            raise ValueError(
                "--check-reference-solution only supports execution test id(s): "
                + ", ".join(non_execution_ids)
            )

    def _run_knowledge_tests(self, tests: List[KnowledgeTest]) -> None:
        """Run knowledge tests in parallel.

        Prompts the model with WordPress knowledge questions and scores responses
        against either multiple-choice or short-answer expectations.

        Args:
            tests: List of knowledge test definitions.

        Raises:
            TestError: If any test fails, stops execution and raises with details.
        """
        tests_to_run = _limit_tests(tests, self.config)
        concurrency = self.config.run.concurrency

        def process_test(test: KnowledgeTest) -> Dict[str, Any]:
            try:
                prompt = self._render_knowledge_prompt(test)
                generation = self.model.generate_with_metadata(prompt)
                answer = strip_code_fences(generation.text).strip()
                correct = score_knowledge_answer(test, answer)
                return build_knowledge_record(
                    test=test,
                    mode="model",
                    model_config=self.config.model,
                    prompt_hash=sha256(prompt),
                    raw_completion=generation.text,
                    answer=answer,
                    knowledge_score=correct,
                    usage=generation.usage_dict(),
                    model_call=_model_call_info(generation),
                )
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
                            self.aggregator.add_knowledge(result["scores"]["knowledge"])
                            self.usage_aggregator.add(result.get("usage"))
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
        tests_to_run = _limit_tests(tests, self.config)

        def process_test(test: ExecutionTest) -> Dict[str, Any]:
            """Process a single execution test."""
            try:
                prompt = self._render_execution_prompt(test)
                generation = self.model.generate_with_metadata(prompt)
                code = strip_code_fences(generation.text)
                verification_spec = _build_verification_spec(test, self.config)
                env_result = self.environment.execute_code(code, verification_spec)
                scores = self._score_execution(
                    env_result.raw,
                    test,
                    skip_runtime=self.config.run.skip_runtime,
                    skip_static=self.config.run.skip_static,
                )
                return build_execution_record(
                    test=test,
                    mode="model",
                    model_config=self.config.model,
                    prompt_hash=sha256(prompt),
                    raw_completion=generation.text,
                    code=code,
                    env_result=env_result,
                    scores=scores,
                    usage=generation.usage_dict(),
                    model_call=_model_call_info(generation),
                )
            except Exception as e:
                raise TestError(test.id, "execution", e) from e

        def on_result(result: Dict[str, Any]) -> None:
            with self._lock:
                self.aggregator.add_execution(result["scores"])
                self.usage_aggregator.add(result.get("usage"))
                self.records.append(result)

        _run_isolated_execution_loop(
            tests_to_run=tests_to_run,
            config=self.config,
            environment=self.environment,
            process_test=process_test,
            on_result=on_result,
            progress_label="Execution",
        )

    def _run_reference_solution_tests(self, tests: List[ExecutionTest]) -> None:
        """Run execution tests using their reference_solution as candidate code."""
        tests_to_run = _limit_tests(tests, self.config)

        def process_test(test: ExecutionTest) -> Dict[str, Any]:
            try:
                if not test.reference_solution:
                    raise ValueError("Missing reference_solution")
                verification_spec = _build_verification_spec(test, self.config)
                env_result = self.environment.execute_code(test.reference_solution, verification_spec)
                scores = self._score_execution(
                    env_result.raw,
                    test,
                    skip_runtime=self.config.run.skip_runtime,
                    skip_static=self.config.run.skip_static,
                )
                return build_execution_record(
                    test=test,
                    mode="reference_solution",
                    model_config=None,
                    prompt_hash=None,
                    raw_completion=None,
                    code=test.reference_solution,
                    env_result=env_result,
                    scores=scores,
                )
            except Exception as e:
                raise TestError(test.id, "execution", e) from e

        def on_result(result: Dict[str, Any]) -> None:
            with self._lock:
                self.aggregator.add_execution(result["scores"])
                self.usage_aggregator.add(result.get("usage"))
                self.records.append(result)

        _run_isolated_execution_loop(
            tests_to_run=tests_to_run,
            config=self.config,
            environment=self.environment,
            process_test=process_test,
            on_result=on_result,
            progress_label="Reference solutions",
        )

    @staticmethod
    def _render_knowledge_prompt(test: KnowledgeTest) -> str:
        """Format a knowledge test into a prompt string.

        Args:
            test: Knowledge test with question and choices.

        Returns:
            Formatted prompt with instructions that match the answer mode.
        """
        return render_knowledge_prompt(test)

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
        if test.test_function:
            lines.append("")
            lines.append(f"Define this function: {test.test_function}")
        lines.append(
            "Return only valid PHP code without explanations. Wrap the response in ```php fences."
        )
        return "\n".join(lines)

    @staticmethod
    def _score_execution(
        raw: Dict[str, Any],
        test: ExecutionTest,
        *,
        skip_runtime: bool = False,
        skip_static: bool = False,
    ) -> Dict[str, Any]:
        """Score an execution test with runtime behavior as the primary signal.

        Scoring model (SCORING_VERSION 2.0):

        - ``execution_pass`` (bool, primary): the code executed without a
          hard crash/timeout, its runtime assertions effectively all passed
          (runtime score >= 0.999), and no forbidden static pattern with
          severity ``error`` matched. When runtime assertions are skipped or
          absent, the runtime requirement falls back to the static score so
          static-only tests remain gradable.
        - ``runtime_score`` (float): weighted partial runtime assertion
          score, 0.0 unless the code actually ran.
        - ``static_score`` (float): regex diagnostic score. Never grants
          correctness credit on its own; kept for authoring/diagnostics.
        - ``static_policy_pass`` (bool): False when a forbidden pattern with
          severity ``error`` matched (a hard guardrail failure).
        - ``correctness`` (float, legacy): 1.0 on strict pass, otherwise
          partial runtime credit (0.0 on crash/policy failure semantics
          preserved through the runtime score). Kept one release for
          consumers of the old key.

        Rationale: regex checks are gameable and can punish valid alternate
        implementations. Behavior is ground truth; static checks remain as
        diagnostics and hard security/policy guardrails only.
        """
        static_checks = test.static_checks or {}
        runtime_checks = test.runtime_checks or {}
        runtime_applicable = not skip_runtime and bool(runtime_checks.get("assertions"))
        static_applicable = not skip_static and bool(
            static_checks.get("required_patterns") or static_checks.get("forbidden_patterns")
        )

        static_result = raw.get("static") if isinstance(raw, dict) else None
        static_score = None
        if static_applicable and isinstance(static_result, dict):
            value = static_result.get("score")
            static_score = float(value) if isinstance(value, (int, float)) else 0.0

        static_policy_pass = not (
            static_applicable
            and BenchmarkRunner._forbidden_error_found(static_result)
        )

        crashed = runtime_applicable and (not raw or BenchmarkRunner._runtime_crashed(raw))
        runtime_score = 0.0
        if runtime_applicable and not crashed and isinstance(raw, dict):
            runtime_result = raw.get("runtime")
            value = runtime_result.get("score") if isinstance(runtime_result, dict) else None
            runtime_score = float(value) if isinstance(value, (int, float)) else 0.0

        if runtime_applicable:
            behavior_passed = not crashed and runtime_score >= 0.999
        elif static_applicable:
            # Static-only tests: behavior cannot be observed, so the static
            # score is the only gradable dimension.
            behavior_passed = (static_score or 0.0) >= 0.999
        else:
            behavior_passed = False

        execution_pass = behavior_passed and static_policy_pass

        if execution_pass:
            correctness = 1.0
        elif runtime_applicable:
            correctness = 0.0 if crashed else round(runtime_score, 4)
        else:
            correctness = round(static_score or 0.0, 4)

        return {
            "knowledge": None,
            "correctness": correctness,
            "execution_pass": execution_pass,
            "runtime": round(runtime_score, 4) if runtime_applicable else None,
            "static": round(static_score, 4) if static_score is not None else None,
            "static_policy_pass": static_policy_pass if static_applicable else None,
        }

    @staticmethod
    def _forbidden_error_found(static_result: Any) -> bool:
        """Whether the static analysis found a forbidden pattern with severity error."""
        if not isinstance(static_result, dict):
            return False
        details = static_result.get("details") or {}
        if details.get("failure_reason"):
            return True
        forbidden = details.get("forbidden") or []
        return any(
            isinstance(entry, dict)
            and entry.get("found")
            and entry.get("severity") == "error"
            for entry in forbidden
        )

    @staticmethod
    def _runtime_crashed(raw: Dict[str, Any]) -> bool:
        """Detect a hard execution failure in the runtime result.

        Only call this when the test defines runtime assertions. A crash shows
        up two ways: the assertion loop never accumulated weight (execution
        threw before any assertion ran), or the runtime appended a synthetic
        ``execution_error``/``fatal_error`` entry to the assertions.

        Args:
            raw: Raw result dict from the WordPress runtime.

        Returns:
            True if the code failed to run, as opposed to running but failing
            some assertions.
        """
        runtime = raw.get("runtime")
        if not isinstance(runtime, dict):
            return True
        details = runtime.get("details") or {}
        if not details.get("total_weight"):
            return True
        assertions = details.get("assertions") or []
        return any(
            isinstance(assertion, dict)
            and assertion.get("type") in {"execution_error", "fatal_error"}
            for assertion in assertions
        )

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
                for record in payload["results"]:
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
        tests = filter_tests_by_ids(load_tests(self.config.dataset), self.config.run.test_ids)
        ensure_test_ids_match_type(
            tests,
            self.config.run.test_type,
            self.config.run.test_ids,
        )
        if self.config.run.test_type != "knowledge":
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
                "result_schema_version": RESULT_SCHEMA_VERSION,
                "scoring_version": SCORING_VERSION,
                "grader": self.config.grader.model_dump(mode="json"),
                "dataset": self.config.dataset.model_dump(mode="json"),
                "runtime_isolation": self.config.run.execution_isolation,
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
        self.usage_aggregator = UsageAggregator()
        self.records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def run(self) -> Dict[str, Any]:
        """Run all tests and return scores for this model.

        Returns:
            Dict with model config, aggregate scores, and individual results.
        """
        test_type = self.config.run.test_type
        if test_type in (None, "knowledge"):
            self._run_knowledge_tests(self.tests["knowledge"])
        if test_type in (None, "execution"):
            self._run_execution_tests(self.tests["execution"])
        summary = self.aggregator.finalize()
        return {
            "model_config": self.model_config.model_dump(mode="json"),
            "scoring_version": SCORING_VERSION,
            "usage": self.usage_aggregator.summary(),
            "scores": {
                "knowledge": summary.knowledge,
                "execution_pass_rate": summary.execution_pass_rate,
                "runtime": summary.runtime,
                "static_policy_pass_rate": summary.static_policy_pass_rate,
                "correctness": summary.correctness,
                "overall": summary.overall(),
            },
            "results": sort_records(self.records),
        }

    def _run_knowledge_tests(self, tests: List[KnowledgeTest]) -> None:
        """Run knowledge tests in parallel. See BenchmarkRunner._run_knowledge_tests."""
        tests_to_run = _limit_tests(tests, self.config)
        concurrency = self.config.run.concurrency

        def process_test(test: KnowledgeTest) -> Dict[str, Any]:
            try:
                prompt = BenchmarkRunner._render_knowledge_prompt(test)
                generation = self.model.generate_with_metadata(prompt)
                answer = strip_code_fences(generation.text).strip()
                correct = score_knowledge_answer(test, answer)
                return build_knowledge_record(
                    test=test,
                    mode="model",
                    model_config=self.model_config,
                    prompt_hash=sha256(prompt),
                    raw_completion=generation.text,
                    answer=answer,
                    knowledge_score=correct,
                    usage=generation.usage_dict(),
                    model_call=_model_call_info(generation),
                )
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
                            self.aggregator.add_knowledge(result["scores"]["knowledge"])
                            self.usage_aggregator.add(result.get("usage"))
                            self.records.append(result)
                        progress.update(task, advance=1)
                    except TestError:
                        for f in futures:
                            f.cancel()
                        raise

    def _run_execution_tests(self, tests: List[ExecutionTest]) -> None:
        """Run execution tests with isolation. See BenchmarkRunner._run_execution_tests."""
        tests_to_run = _limit_tests(tests, self.config)

        def process_test(test: ExecutionTest) -> Dict[str, Any]:
            try:
                prompt = BenchmarkRunner._render_execution_prompt(test)
                generation = self.model.generate_with_metadata(prompt)
                code = strip_code_fences(generation.text)
                verification_spec = _build_verification_spec(test, self.config)
                env_result = self.environment.execute_code(code, verification_spec)
                scores = BenchmarkRunner._score_execution(
                    env_result.raw,
                    test,
                    skip_runtime=self.config.run.skip_runtime,
                    skip_static=self.config.run.skip_static,
                )
                return build_execution_record(
                    test=test,
                    mode="model",
                    model_config=self.model_config,
                    prompt_hash=sha256(prompt),
                    raw_completion=generation.text,
                    code=code,
                    env_result=env_result,
                    scores=scores,
                    usage=generation.usage_dict(),
                    model_call=_model_call_info(generation),
                )
            except Exception as e:
                raise TestError(test.id, "execution", e) from e

        def on_result(result: Dict[str, Any]) -> None:
            with self._lock:
                self.aggregator.add_execution(result["scores"])
                self.usage_aggregator.add(result.get("usage"))
                self.records.append(result)

        _run_isolated_execution_loop(
            tests_to_run=tests_to_run,
            config=self.config,
            environment=self.environment,
            process_test=process_test,
            on_result=on_result,
            progress_label="Execution",
        )
