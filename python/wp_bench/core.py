"""Main orchestration loop for WP-Bench."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import orjson
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from .config import HarnessConfig, ModelConfig
from .datasets import ExecutionTest, KnowledgeTest, load_tests
from .environment import WordPressEnvironment
from .models import ModelInterface
from .scoring import ScoreAggregator
from .utils import ensure_dir, sha256, strip_code_fences

console = Console()


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
        """
        tests = load_tests(self.config.dataset)
        self.environment.setup()
        self._run_knowledge_tests(tests["knowledge"])
        self._run_execution_tests(tests["execution"])
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
        """
        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: KnowledgeTest) -> Dict[str, Any]:
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

        with Progress() as progress:
            task = progress.add_task("Knowledge", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    result = future.result()
                    with self._lock:
                        self.aggregator.add_knowledge(result["score"])
                        self.records.append(result)
                    progress.update(task, advance=1)

    def _run_execution_tests(self, tests: List[ExecutionTest]) -> None:
        """Run code generation execution tests in parallel.

        Prompts the model to generate PHP code, executes it in the WordPress
        environment, and scores based on static/runtime assertions.

        Args:
            tests: List of execution test definitions.
        """
        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: ExecutionTest) -> Dict[str, Any]:
            """Process a single execution test (runs in thread pool)."""
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

        with Progress() as progress:
            task = progress.add_task("Execution", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    result = future.result()
                    with self._lock:
                        self.aggregator.add_execution(result["correctness"], result["quality"])
                        self.records.append(result)
                    progress.update(task, advance=1)

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
        console.print(f"Results written to: {output_path}")
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
        """
        models = self.config.get_models()
        tests = load_tests(self.config.dataset)
        self.environment.setup()

        for model_config in models:
            model_name = model_config.name
            console.print(f"\n[bold blue]Running: {model_name}[/bold blue]")

            runner = SingleModelRunner(
                config=self.config,
                model_config=model_config,
                environment=self.environment,
                tests=tests,
            )
            result = runner.run()
            self.results[model_name] = result

        self._print_comparison_table()
        self._write_outputs()
        return self.results

    def _print_comparison_table(self) -> None:
        """Print a formatted table comparing scores across all models."""
        table = Table(title="WP-Bench Results")
        table.add_column("Model", style="cyan")
        table.add_column("Knowledge", justify="right")
        table.add_column("Correctness", justify="right")
        table.add_column("Quality", justify="right")
        table.add_column("Overall", justify="right", style="bold")

        for model_name, result in self.results.items():
            scores = result["scores"]
            table.add_row(
                model_name,
                f"{scores['knowledge']*100:.1f}%",
                f"{scores['correctness']*100:.1f}%",
                f"{scores['quality']*100:.1f}%" if scores['quality'] else "N/A",
                f"{scores['overall']*100:.1f}%",
            )

        console.print(table)

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
        console.print(f"Results written to: {output_path}")


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
        summary = self.aggregator.finalize()
        return {
            "model_config": self.model_config.model_dump(mode="json"),
            "scores": {
                "knowledge": summary.knowledge,
                "correctness": summary.correctness,
                "quality": summary.quality,
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

        with Progress() as progress:
            task = progress.add_task("Knowledge", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    result = future.result()
                    with self._lock:
                        self.aggregator.add_knowledge(result["score"])
                        self.records.append(result)
                    progress.update(task, advance=1)

    def _run_execution_tests(self, tests: List[ExecutionTest]) -> None:
        """Run execution tests in parallel. See BenchmarkRunner._run_execution_tests."""
        limit = self.config.run.limit or len(tests)
        tests_to_run = tests[:limit]
        concurrency = self.config.run.concurrency

        def process_test(test: ExecutionTest) -> Dict[str, Any]:
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

        with Progress() as progress:
            task = progress.add_task("Execution", total=len(tests_to_run))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(process_test, test): test for test in tests_to_run}
                for future in as_completed(futures):
                    result = future.result()
                    with self._lock:
                        self.aggregator.add_execution(result["correctness"], result["quality"])
                        self.records.append(result)
                    progress.update(task, advance=1)
