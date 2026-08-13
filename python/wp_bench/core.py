"""Main orchestration loop for WP-Bench."""
from __future__ import annotations

import threading
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Any

from .artifacts import (
    Artifact,
    ArtifactError,
    parse_artifact,
    render_artifact_instructions,
)
from .config import HarnessConfig, ModelConfig
from .datasets import (
    ExecutionTest,
    filter_tests_by_ids,
    load_tests,
)
from .environment import WordPressEnvironment
from .exploits import exploit_candidates, gateway_name
from .models import ModelInterface
from .output import (
    create_progress,
    print_abort_message,
    print_comparison_table,
    print_exploit_findings,
    print_model_header,
    print_reference_solution_failures,
    print_results_path,
    print_skill_impact,
    print_systemic_abort,
    print_test_error,
    print_test_warning,
)
from .records import (
    RESULT_SCHEMA_VERSION,
    build_error_record,
    build_execution_record,
    build_exploit_audit_record,
    errored_test_ids,
    execution_record_passed,
    sort_records,
)
from .results_io import RecordStream, open_run_artifacts, write_results_json
from .scoring import SCORING_VERSION, ScoreAggregator, UsageAggregator
from .selection import select_tests
from .skills import (
    BASELINE_VARIANT,
    LoadedSkill,
    ReusedBaseline,
    Variant,
    build_variants,
)
from .utils import sha256


@contextmanager
def _graded_run(stream: RecordStream) -> Iterator[None]:
    """Own a run's record stream and report a failed pass, once.

    Every run mode needs the same three things: the stream released
    however the run ends, a TestError rendered and exited on, and Ctrl-C
    reported as an abort. Keeping them in one place is what stops a new
    run mode from silently getting one of the three wrong -- the exploit
    audit already had to re-add its own copy.
    """
    try:
        with stream:
            yield
    except TestError as error:
        print_test_error(error)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        print_abort_message()
        raise SystemExit(130) from None


class _ResultBookkeeping:
    """Aggregation, retention, and streaming for one run's records.

    Single- and multi-model runners keep identical bookkeeping; sharing one
    implementation keeps them from drifting apart the next time a metric or
    an error predicate changes.
    """

    def _init_bookkeeping(self, stream: RecordStream) -> None:
        self.aggregator = ScoreAggregator()
        self.usage_aggregator = UsageAggregator()
        self.records: list[dict[str, Any]] = []
        self._stream = stream
        self._lock = threading.Lock()

    def _on_result(self, result: dict[str, Any]) -> None:
        """Aggregate a finished record, keep it, and stream it to disk."""
        with self._lock:
            if result.get("error") is None:
                self.aggregator.add_execution(result["scores"])
            self.usage_aggregator.add(result.get("usage"))
            self.records.append(result)
            self._stream.write(result)


class TestError(Exception):
    """Wrapper to preserve test context when an error occurs."""

    def __init__(self, test_id: str, original_error: Exception):
        self.test_id = test_id
        self.original_error = original_error
        self.traceback_str = traceback.format_exc()
        super().__init__(str(original_error))


def select_run_tests(tests: list[Any], config: HarnessConfig) -> list[Any]:
    """Select tests for this run (seeded stratified selection when limited).

    Zero selected tests is always a configuration or dataset problem (missing
    suite, an HF export with no execution rows): every run mode would
    otherwise "succeed" vacuously, so fail loudly here instead.
    """
    selected = select_tests(
        tests,
        limit=config.run.limit,
        test_ids=config.run.test_ids,
        seed=config.run.seed,
    )
    if not selected:
        raise ValueError(
            f"No execution tests selected from dataset "
            f"'{config.dataset.name}' (suite {config.run.suite!r}). "
            "Check the dataset source and suite name."
        )
    return selected


def _build_verification_spec(test: Any, config: HarnessConfig) -> dict[str, Any]:
    """Build the verifier payload honoring run.skip_runtime/skip_static.

    A skipped dimension is sent as empty checks so the runtime does not
    execute it; scoring treats it as not applicable (see _score_execution).

    When runtime checks run, a weight-0 ``function_exists`` assertion derived
    from ``test.test_function`` is prepended. It is the harness's gateway for
    invoking the submission -- shown to the model and checked at runtime, but
    earning no score. A missing gateway fails the behavioral assertions on its
    own; the derived check only makes that failure diagnosable.
    """
    static_checks = {} if config.run.skip_static else test.static_checks
    if config.run.skip_runtime:
        runtime_checks: dict[str, Any] = {}
    else:
        runtime_checks = dict(test.runtime_checks)
        if test.test_function:
            name = gateway_name(test)
            if not name:
                raise ValueError(
                    f"Cannot extract function name from test_function "
                    f"signature: {test.test_function!r}"
                )
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


def _artifact_failure_scores() -> dict[str, Any]:
    """Scores for a completion that failed artifact parsing/validation."""
    return {
        "correctness": 0.0,
        "execution_pass": False,
        "runtime": 0.0,
        "static": None,
        "static_policy_pass": None,
    }


class _ArtifactFailureResult:
    """Stand-in env result recording an artifact parse/validation failure."""

    def __init__(self, error: ArtifactError):
        self.success = False
        self.stdout = ""
        self.stderr = str(error)
        self.timed_out = False
        self.raw = {
            "success": False,
            "artifact_error": str(error),
            "runtime": {"score": 0.0, "details": {"assertions": [], "total_weight": 0}},
            "static": {"score": 0.0, "details": {}},
        }


def _model_call_info(generation: Any) -> dict[str, Any]:
    """Call-reliability metadata recorded alongside each test result."""
    return {
        "retry_count": generation.retry_count,
        "provider_response_id": generation.provider_response_id,
        "temperature_fallback": generation.temperature_fallback,
    }


def _error_record(
    test: Any,
    error: TestError,
    *,
    mode: str,
    model_config: ModelConfig | None,
    variant: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical record for a test the runner caught erroring.

    Unwraps the TestError (type/message) so each ``on_error`` call site is
    a one-liner rather than repeating the extraction.
    """
    return build_error_record(
        test=test,
        mode=mode,
        model_config=model_config,
        error_type=type(error.original_error).__name__,
        error_message=str(error.original_error),
        variant=variant,
    )


class _ContinueOnErrorPolicy:
    """Decides whether a per-test error aborts the run.

    With ``run.continue_on_error`` disabled (the default), the first error
    aborts — legacy behavior, required for official runs. When enabled,
    per-test errors are recorded and the run continues, with one guardrail:
    if the first ``SYSTEMIC_THRESHOLD`` processed tests all error with no
    success in between, the failure is systemic (bad credentials, dead
    runtime) rather than per-test, and burning through the remaining suite
    would waste hours and API spend for an unusable result. Fail loudly
    instead.
    """

    SYSTEMIC_THRESHOLD = 5

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.errors = 0
        self.successes = 0
        self.last_error: TestError | None = None

    def record_success(self) -> None:
        self.successes += 1

    def register_error(self, error: TestError) -> bool:
        """Record a per-test error and return whether it aborts the run."""
        if not self.enabled:
            return True
        self.errors += 1
        self.last_error = error
        if self.successes == 0 and self.errors >= self.SYSTEMIC_THRESHOLD:
            print_systemic_abort(self.errors)
            return True
        return False

    def finish(self) -> None:
        """Abort a completed loop that graded nothing.

        Small runs (``--limit`` below the threshold) can finish before the
        systemic check trips. A run whose every test errored has produced no
        score at all — writing a null-score results file would be silently
        useless, so fail loudly with the last error instead.
        """
        if self.enabled and self.errors and not self.successes:
            print_systemic_abort(self.errors)
            assert self.last_error is not None
            raise self.last_error


def _run_isolated_execution_loop(
    *,
    tests_to_run: list[Any],
    config: HarnessConfig,
    environment: WordPressEnvironment,
    process_test: Any,
    on_result: Any,
    on_error: Any,
    progress_label: str,
) -> None:
    """Run execution-style tests honoring the configured isolation strategy.

    ``reset_per_test`` (default): tests run serially and the WordPress
    environment is reset to a known baseline before every test, so no test
    can observe state (options, posts, roles, hooks persisted to DB, etc.)
    left behind by a previous test or a previous model run.

    ``none``: legacy concurrent behavior against a shared environment,
    bounded by ``run.execution_concurrency``. Not valid for official runs.

    Per-test errors abort the run unless ``run.continue_on_error`` is set,
    in which case they are warned about, recorded via ``on_error``, and the
    run continues (see _ContinueOnErrorPolicy for the systemic-failure
    guardrail).

    Args:
        tests_to_run: Tests to execute, already limited/filtered.
        config: Harness configuration (isolation strategy, concurrency).
        environment: WordPress environment shared by this run.
        process_test: Callable taking a test and returning a record dict.
        on_result: Callable invoked with each record (aggregation/appending).
        on_error: Callable (test, TestError) -> error record.
        progress_label: Label for the progress bar.
    """
    policy = _ContinueOnErrorPolicy(config.run.continue_on_error)
    if config.run.execution_isolation != "reset_per_test":
        _run_concurrent_loop(
            tests_to_run=tests_to_run,
            max_workers=config.run.execution_concurrency,
            progress_label=progress_label,
            process_test=process_test,
            on_result=on_result,
            on_error=on_error,
            policy=policy,
        )
        return
    with create_progress() as progress:
        task = progress.add_task(progress_label, total=len(tests_to_run))
        for test in tests_to_run:
            environment.reset()
            try:
                result = process_test(test)
            except TestError as error:
                if policy.register_error(error):
                    raise
                print_test_warning(error)
                result = on_error(test, error)
            else:
                policy.record_success()
            on_result(result)
            progress.update(task, advance=1)
    policy.finish()


def _run_concurrent_loop(
    *,
    tests_to_run: list[Any],
    max_workers: int,
    progress_label: str,
    process_test: Any,
    on_result: Any,
    on_error: Any,
    policy: _ContinueOnErrorPolicy,
) -> None:
    """Run tests concurrently, honoring the continue-on-error policy.

    The concurrent core for the ``none``-isolation execution branch. A
    per-test error aborts (cancelling pending futures) unless the policy
    records it and lets the run continue.
    """
    with create_progress() as progress:
        task = progress.add_task(progress_label, total=len(tests_to_run))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_test, test): test for test in tests_to_run}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except TestError as error:
                    if policy.register_error(error):
                        for f in futures:
                            f.cancel()
                        raise
                    print_test_warning(error)
                    result = on_error(futures[future], error)
                else:
                    policy.record_success()
                on_result(result)
                progress.update(task, advance=1)
    policy.finish()


class BenchmarkRunner(_ResultBookkeeping):
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
        self._results_path, stream = open_run_artifacts(config.output)
        self._init_bookkeeping(stream or RecordStream(None))

    def run(self) -> dict[str, Any]:
        """Execute the full benchmark pipeline.

        Loads tests, sets up the WordPress environment, runs the execution
        tests, computes aggregate scores, and writes results to disk.

        Returns:
            Dict containing metadata (scores, config) and individual test results.

        Raises:
            SystemExit: If a test fails, prints error details and exits with code 1.
        """
        tests = filter_tests_by_ids(load_tests(self.config.dataset), self.config.run.test_ids)
        if not tests:
            raise ValueError(
                f"Dataset '{self.config.dataset.name}' contains no execution "
                "tests. Check the dataset source and suite name."
            )
        if self.config.run.check_exploits:
            return self._run_exploit_audit(tests)
        reference_mode = self.config.run.check_reference_solution
        self.environment.setup()
        with _graded_run(self._stream):
            if reference_mode:
                self._run_reference_solution_tests(tests)
            else:
                self._run_execution_tests(tests)
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
                "continue_on_error": self.config.run.continue_on_error,
                "errored_test_ids": errored_test_ids(self.records),
                "usage": self.usage_aggregator.summary(),
                "scores": summary.as_scores_dict(),
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

    def _run_execution_tests(self, tests: list[ExecutionTest]) -> None:
        """Run code generation execution tests in parallel.

        Prompts the model to generate PHP code, executes it in the WordPress
        environment, and scores based on static/runtime assertions.

        Args:
            tests: List of execution test definitions.

        Raises:
            TestError: If any test fails, stops execution and raises with details.
        """
        tests_to_run = select_run_tests(tests, self.config)

        def process_test(test: ExecutionTest) -> dict[str, Any]:
            """Process a single execution test."""
            try:
                prompt = self._render_execution_prompt(test)
                generation = self.model.generate_with_metadata(prompt)
                artifact_kind = getattr(test, "artifact_kind", "php_snippet")
                try:
                    artifact = parse_artifact(generation.text, artifact_kind)
                except ArtifactError as artifact_error:
                    return build_execution_record(
                        test=test,
                        mode="model",
                        model_config=self.config.model,
                        prompt_hash=sha256(prompt),
                        raw_completion=generation.text,
                        code="",
                        env_result=_ArtifactFailureResult(artifact_error),
                        scores=_artifact_failure_scores(),
                        usage=generation.usage_dict(),
                        model_call=_model_call_info(generation),
                    )
                verification_spec = _build_verification_spec(test, self.config)
                env_result = self.environment.execute_artifact(artifact, verification_spec)
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
                    code=artifact.code,
                    env_result=env_result,
                    scores=scores,
                    usage=generation.usage_dict(),
                    model_call=_model_call_info(generation),
                )
            except Exception as e:
                raise TestError(test.id, e) from e

        def on_error(test: ExecutionTest, error: TestError) -> dict[str, Any]:
            return _error_record(test, error, mode="model", model_config=self.config.model)

        _run_isolated_execution_loop(
            tests_to_run=tests_to_run,
            config=self.config,
            environment=self.environment,
            process_test=process_test,
            on_result=self._on_result,
            on_error=on_error,
            progress_label="Execution",
        )

    def _run_reference_solution_tests(self, tests: list[ExecutionTest]) -> None:
        """Run execution tests using their reference_solution as candidate code."""
        tests_to_run = select_run_tests(tests, self.config)

        def process_test(test: ExecutionTest) -> dict[str, Any]:
            try:
                artifact_kind = getattr(test, "artifact_kind", "php_snippet")
                if artifact_kind == "wp_plugin_files":
                    if not test.reference_files:
                        raise ValueError("Missing reference_files for plugin artifact test")
                    artifact = Artifact(kind="wp_plugin_files", files=test.reference_files)
                else:
                    if not test.reference_solution:
                        raise ValueError("Missing reference_solution")
                    artifact = Artifact(kind="php_snippet", code=test.reference_solution)
                verification_spec = _build_verification_spec(test, self.config)
                env_result = self.environment.execute_artifact(artifact, verification_spec)
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
                    code=artifact.code,
                    env_result=env_result,
                    scores=scores,
                )
            except Exception as e:
                raise TestError(test.id, e) from e

        def on_error(test: ExecutionTest, error: TestError) -> dict[str, Any]:
            return _error_record(test, error, mode="reference_solution", model_config=None)

        _run_isolated_execution_loop(
            tests_to_run=tests_to_run,
            config=self.config,
            environment=self.environment,
            process_test=process_test,
            on_result=self._on_result,
            on_error=on_error,
            progress_label="Reference solutions",
        )

    def _run_exploit_audit(self, tests: list[ExecutionTest]) -> dict[str, Any]:
        """Adversarial assertion audit: prove zero-effort cheats fail.

        For every execution test, run each exploit candidate (generic
        battery + any authored exploit_solutions) through the real verifier
        and check whether it earns ``execution_pass``. A test a cheat can
        pass is under-specified — its assertions check a predictable output
        rather than the WordPress behavior the task describes. Exits non-zero
        when any test is exploitable, mirroring reference-solution mode.
        """
        self.environment.setup()
        with _graded_run(self._stream):
            self._run_exploit_audit_tests(tests)

        exploitable = [record for record in self.records if record["exploitable"]]
        auditable = sum(1 for record in self.records if record["candidates_tried"] > 0)
        audit = {
            "total": len(self.records),
            "auditable": auditable,
            "not_auditable": len(self.records) - auditable,
            "exploitable": len(exploitable),
            "exploitable_test_ids": sorted(record["test_id"] for record in exploitable),
        }
        payload = {
            "metadata": {
                "suite": self.config.run.suite,
                "mode": "exploit_audit",
                "result_schema_version": RESULT_SCHEMA_VERSION,
                "scoring_version": SCORING_VERSION,
                "grader": self.config.grader.model_dump(mode="json"),
                "dataset": self.config.dataset.model_dump(mode="json"),
                "runtime_isolation": self.config.run.execution_isolation,
                "audit": audit,
            },
            "results": sort_records(self.records),
        }
        self._write_outputs(payload)
        print_exploit_findings(audit, exploitable)
        if exploitable:
            raise SystemExit(1)
        return payload

    def _run_exploit_audit_tests(self, tests: list[ExecutionTest]) -> None:
        """Run every exploit candidate for each test; record exploitable ones.

        Serial and reset-before-each-candidate: candidates share a gateway
        function name and rely on per-test fixtures, so state must not leak
        between attempts. Short-circuits a test as soon as one cheat passes.
        """
        tests_to_run = select_run_tests(tests, self.config)
        with create_progress() as progress:
            task = progress.add_task("Exploit audit", total=len(tests_to_run))
            for test in tests_to_run:
                candidates = exploit_candidates(test)
                try:
                    hit = self._first_passing_exploit(test, candidates)
                except Exception as e:
                    raise TestError(test.id, e) from e
                record = build_exploit_audit_record(
                    test=test,
                    candidates_tried=len(candidates),
                    passing_exploit=hit[0] if hit else None,
                    exploit_code=hit[1] if hit else None,
                )
                with self._lock:
                    self.records.append(record)
                    self._stream.write(record)
                progress.update(task, advance=1)

    def _first_passing_exploit(
        self,
        test: ExecutionTest,
        candidates: list[tuple[str, str]],
    ) -> tuple[str, str] | None:
        """Return the first (label, code) cheat that satisfies the assertions.

        The verification spec is identical across a test's candidates, so it
        is built once here rather than per candidate. Returns None when no
        cheat passes — the test's assertions rejected every zero-effort stub.
        """
        verification_spec = _build_verification_spec(test, self.config)
        for label, code in candidates:
            self.environment.reset()
            env_result = self.environment.execute_artifact(
                Artifact(kind="php_snippet", code=code),
                verification_spec,
            )
            scores = self._score_execution(
                env_result.raw,
                test,
                skip_runtime=self.config.run.skip_runtime,
                skip_static=self.config.run.skip_static,
            )
            if scores.get("execution_pass"):
                return (label, code)
        return None

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
            render_artifact_instructions(getattr(test, "artifact_kind", "php_snippet"))
        )
        return "\n".join(lines)

    @staticmethod
    def _score_execution(
        raw: dict[str, Any],
        test: ExecutionTest,
        *,
        skip_runtime: bool = False,
        skip_static: bool = False,
    ) -> dict[str, Any]:
        """Score an execution test with runtime behavior as the primary signal.

        Scoring model (SCORING_VERSION 3.0):

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
    def _runtime_crashed(raw: dict[str, Any]) -> bool:
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

    def _write_outputs(self, payload: dict[str, Any]) -> None:
        """Write benchmark results to JSON and JSONL files.

        Args:
            payload: Complete results dict with metadata and test records.
        """
        write_results_json(self._results_path, payload)
        print_results_path(self._results_path)
        self._stream.finalize(payload["results"])


class MultiModelRunner:
    """Run benchmarks across multiple models and produce a comparison table.

    Iterates over all configured models, runs the full test suite for each using
    SingleModelRunner, and outputs a side-by-side comparison of scores.
    """

    def __init__(
        self,
        config: HarnessConfig,
        skills: list[LoadedSkill] | None = None,
        baseline: ReusedBaseline | None = None,
    ):
        """Initialize the multi-model runner.

        Args:
            config: Harness configuration with multiple models defined.
            skills: Pre-loaded skills for A/B variant runs (loaded in the
                CLI so path errors surface before environment setup). When
                set, every model runs both a baseline and a with-skills pass
                unless ``config.skills.only`` skips the baseline.
            baseline: Baseline passes from a previous run (validated in the
                CLI). Present means the baseline arm is taken from that file
                instead of graded again.
        """
        self.config = config
        self.environment = WordPressEnvironment(config.grader)
        self.skills = skills or []
        self.baseline = baseline
        self.variants = build_variants(self.skills, skills_only=config.skills.only)
        self.results: dict[str, dict[str, Any]] = {}
        self._results_path, self._stream = open_run_artifacts(config.output)

    def run(self) -> dict[str, Any]:
        """Execute benchmarks for all configured (model x variant) passes.

        Sets up the WordPress environment once, then runs each pass
        sequentially. Prints a comparison table and writes combined results.

        Returns:
            Dict mapping display names (model name plus variant suffix,
            e.g. "gpt-4o" / "gpt-4o+skills") to their individual results.

        Raises:
            SystemExit: If a test fails, prints error details and exits with code 1.
        """
        models = self.config.get_models()
        self._ensure_unique_display_names(models)
        tests = filter_tests_by_ids(load_tests(self.config.dataset), self.config.run.test_ids)
        if not tests:
            raise ValueError(
                f"Dataset '{self.config.dataset.name}' contains no execution "
                "tests. Check the dataset source and suite name."
            )
        self.environment.setup()

        with _graded_run(self._stream):
            for model_config in models:
                for variant in self.variants:
                    display_name = f"{model_config.name}{variant.label_suffix}"
                    reused = self._reused_entry(model_config, variant)
                    if reused is not None:
                        self.results[display_name] = reused
                        continue
                    print_model_header(display_name)

                    runner = SingleModelRunner(
                        config=self.config,
                        model_config=model_config,
                        environment=self.environment,
                        tests=tests,
                        variant=variant,
                        stream=self._stream,
                    )
                    result = runner.run()
                    result["base_model"] = model_config.name
                    result["variant"] = variant.payload_info()
                    self.results[display_name] = result

        print_comparison_table(self.results)
        print_skill_impact(self.results)
        self._write_outputs()
        return self.results

    def _reused_entry(
        self, model_config: ModelConfig, variant: Variant
    ) -> dict[str, Any] | None:
        """The stored baseline pass for this model, when reuse is active.

        Returned in the shape a fresh pass produces so the comparison table,
        the impact table, and the payload treat it identically -- except for
        ``reused_from``, which keeps the recycling visible everywhere the
        result travels.
        """
        if self.baseline is None or variant.kind != "none":
            return None
        entry = dict(self.baseline.entries[model_config.name])
        entry["model_config"] = entry.pop("config", None)
        entry["reused_from"] = self.baseline.source_path
        return entry

    def _ensure_unique_display_names(self, models: list[ModelConfig]) -> None:
        """Reject display-name collisions before any model call is spent.

        A duplicated model entry, or a model literally named "x+skills"
        alongside model "x" in a skills run, would silently overwrite a
        results key.
        """
        display_names = [
            f"{model.name}{variant.label_suffix}"
            for model in models
            for variant in self.variants
        ]
        duplicates = {name for name in display_names if display_names.count(name) > 1}
        if duplicates:
            raise ValueError(
                f"Duplicate result keys {sorted(duplicates)}: model names plus "
                "variant suffixes must be unique."
            )

    def _skills_metadata(self) -> dict[str, Any]:
        """The skills provenance block recorded in payload metadata."""
        skills_variant = next(
            (variant for variant in self.variants if variant.kind != "none"), None
        )
        return {
            "enabled": bool(self.skills),
            "include_references": self.config.skills.include_references,
            "system_prompt_sha256": (
                sha256(skills_variant.system_prompt)
                if skills_variant and skills_variant.system_prompt
                else None
            ),
            "skills": [skill.provenance() for skill in self.skills],
        }

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
                "continue_on_error": self.config.run.continue_on_error,
                "skills": self._skills_metadata(),
                "variants": [variant.key for variant in self.variants],
                "reused_baseline": self.baseline.provenance() if self.baseline else None,
            },
            "models": {
                name: {
                    "config": result["model_config"],
                    "base_model": result["base_model"],
                    "variant": result["variant"],
                    "scores": result["scores"],
                    "usage": result.get("usage"),
                    "results": result["results"],
                    **({"reused_from": result["reused_from"]} if "reused_from" in result else {}),
                }
                for name, result in self.results.items()
            },
        }
        write_results_json(self._results_path, payload)
        print_results_path(self._results_path)
        records = [record for result in self.results.values() for record in result["results"]]
        self._stream.finalize(sort_records(records))


class SingleModelRunner(_ResultBookkeeping):
    """Run benchmark for a single model with pre-loaded tests.

    Used by MultiModelRunner to evaluate one model at a time while sharing
    the WordPress environment and test definitions across models.
    """

    def __init__(
        self,
        config: HarnessConfig,
        model_config: ModelConfig,
        environment: WordPressEnvironment,
        tests: list[ExecutionTest],
        variant: Variant = BASELINE_VARIANT,
        stream: RecordStream | None = None,
    ):
        """Initialize runner for a specific model.

        Args:
            config: Harness configuration for run settings.
            model_config: Configuration for the specific model to evaluate.
            environment: Shared WordPress environment instance.
            tests: Pre-loaded execution tests.
            variant: Which pass of the run matrix this is (baseline or
                skills); carries the system prompt to inject, if any.
            stream: Shared record stream; every pass's records land in the
                one JSONL, tagged by the model and variant on each record.
                Omitted (tests, ad-hoc use) means no streaming.
        """
        self.config = config
        self.model_config = model_config
        self.variant = variant
        self.model = ModelInterface(model_config, system_prompt=variant.system_prompt)
        self.environment = environment
        self.tests = tests
        self._init_bookkeeping(stream or RecordStream(None))

    def run(self) -> dict[str, Any]:
        """Run all tests and return scores for this model.

        Returns:
            Dict with model config, aggregate scores, and individual results.
        """
        self._run_execution_tests(self.tests)
        summary = self.aggregator.finalize()
        return {
            "model_config": self.model_config.model_dump(mode="json"),
            "scoring_version": SCORING_VERSION,
            "usage": self.usage_aggregator.summary(),
            "errored_test_ids": errored_test_ids(self.records),
            "scores": summary.as_scores_dict(),
            "results": sort_records(self.records),
        }

    def _run_execution_tests(self, tests: list[ExecutionTest]) -> None:
        """Run execution tests with isolation. See BenchmarkRunner._run_execution_tests."""
        tests_to_run = select_run_tests(tests, self.config)
        variant_info = self.variant.record_info()

        def process_test(test: ExecutionTest) -> dict[str, Any]:
            try:
                prompt = BenchmarkRunner._render_execution_prompt(test)
                generation = self.model.generate_with_metadata(prompt)
                artifact_kind = getattr(test, "artifact_kind", "php_snippet")
                try:
                    artifact = parse_artifact(generation.text, artifact_kind)
                except ArtifactError as artifact_error:
                    return build_execution_record(
                        test=test,
                        mode="model",
                        model_config=self.model_config,
                        prompt_hash=sha256(prompt),
                        raw_completion=generation.text,
                        code="",
                        env_result=_ArtifactFailureResult(artifact_error),
                        scores=_artifact_failure_scores(),
                        usage=generation.usage_dict(),
                        model_call=_model_call_info(generation),
                        variant=variant_info,
                    )
                verification_spec = _build_verification_spec(test, self.config)
                env_result = self.environment.execute_artifact(artifact, verification_spec)
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
                    code=artifact.code,
                    env_result=env_result,
                    scores=scores,
                    usage=generation.usage_dict(),
                    model_call=_model_call_info(generation),
                    variant=variant_info,
                )
            except Exception as e:
                raise TestError(test.id, e) from e

        def on_error(test: ExecutionTest, error: TestError) -> dict[str, Any]:
            return _error_record(
                test, error, mode="model", model_config=self.model_config, variant=variant_info
            )

        _run_isolated_execution_loop(
            tests_to_run=tests_to_run,
            config=self.config,
            environment=self.environment,
            process_test=process_test,
            on_result=self._on_result,
            on_error=on_error,
            progress_label="Execution",
        )
