"""Bridge between Python harness and WordPress runtime."""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import GraderConfig

#: Short timeout for cheap local Docker queries (e.g. ``docker ps``).
CONTAINER_QUERY_TIMEOUT_SECONDS = 30

#: Where the clean-install baseline dump lives inside the runtime container.
#: Written once by :meth:`WordPressEnvironment.setup`, read by every
#: :meth:`WordPressEnvironment.reset`.
BASELINE_DUMP_PATH = "/tmp/wp-bench-baseline.sql"


class EnvironmentSetupTimeout(RuntimeError):
    """Raised when environment setup (wp-env/Docker) exceeds its timeout.

    Setup timeouts are harness/environment failures, not per-test results,
    so they fail fast with a clear message instead of being recorded as a
    test outcome.
    """


@dataclass
class ExecutionResult:
    success: bool
    raw: dict[str, Any]
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class ProcessResult:
    """Outcome of a subprocess run, including timeout state."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


class WordPressEnvironment:
    """Shells out to wp-env/docker runtime to execute verification."""

    def __init__(self, config: GraderConfig):
        self.config = config

    def setup(self) -> None:
        if self.config.wp_env_dir:
            self._run_wp_env(["npx", "wp-env", "start"])
        elif self.config.kind == "docker":
            if not self._container_exists():
                self._start_container()
        else:
            # A cli grader drives a runtime the harness does not own, so there
            # is nothing to install and nothing to capture. reset() refuses
            # rather than pretending it isolated anything.
            return
        self._capture_baseline()

    def _install_command(self) -> list[str]:
        """The canonical clean install every reset restores the site to."""
        return [
            "wp",
            "core",
            "install",
            f"--url={self.config.base_url}",
            "--title=WP-Bench",
            "--admin_user=admin",
            "--admin_password=password",
            "--admin_email=admin@wp-bench.test",
            "--skip-email",
        ]

    def _capture_baseline(self) -> None:
        """Record the clean baseline that every later reset restores.

        Reinstalling WordPress before every test rebuilds a state the harness
        already knows, because it is the same state every time. So the run
        pays for one install here and replays its dump per test instead.

        The dump is taken from exactly what ``wp db reset`` + ``wp core
        install`` produce, so restoring it is state-equivalent to reinstalling.
        The one difference is that install-time timestamps (cron schedules,
        ``user_registered``, ``post_date``) freeze at run start rather than
        advancing per test, which removes a source of run-to-run drift.
        """
        script = " && ".join(
            [
                "wp db reset --yes",
                shlex.join(self._install_command()),
                f"wp db export {shlex.quote(BASELINE_DUMP_PATH)}",
            ]
        )
        _, stderr, returncode, timed_out = self._exec(
            ["sh", "-c", script],
            timeout=self.config.setup_timeout_seconds,
        )
        if timed_out:
            raise EnvironmentSetupTimeout(
                "Timed out capturing the clean WordPress baseline after "
                f"{self.config.setup_timeout_seconds}s (grader.setup_timeout_seconds)."
            )
        if returncode != 0:
            raise RuntimeError(
                f"Failed to capture the clean WordPress baseline (exit code {returncode})"
                f"{': ' + stderr.strip() if stderr.strip() else ''}"
            )

    def reset(self) -> None:
        """Restore the WordPress runtime to the captured clean baseline.

        ``wp db reset`` drops every table, then the baseline dump recorded by
        :meth:`setup` is imported to return to a deterministic just-installed
        state. Called before every execution test when
        ``run.execution_isolation`` is ``reset_per_test`` so no test can
        observe database state (options, posts, roles, transients, cron
        events, etc.) left behind by an earlier test or model run.

        Both steps travel in one invocation: at roughly a quarter-second of
        round-trip latency each, a second trip into the runtime costs more
        than the restore itself. ``&&`` chains them so a failed drop can never
        import the baseline over surviving state.
        """
        if not self.config.wp_env_dir and self.config.kind != "docker":
            # Config validation rejects this pairing, so reaching here means a
            # new grader kind was added without a reset. Refuse rather than
            # let the run stamp an isolation guarantee it never delivered.
            raise RuntimeError(
                f"grader.kind {self.config.kind!r} has no reset implementation, so "
                "run.execution_isolation 'reset_per_test' cannot be honored."
            )
        script = " && ".join(
            [
                "wp db reset --yes",
                f"wp db import {shlex.quote(BASELINE_DUMP_PATH)}",
            ]
        )
        self._reset_step(["sh", "-c", script])

    def _reset_step(self, command: list[str]) -> None:
        """Run one reset command, failing loudly.

        A reset that times out or exits nonzero leaves the next test running
        against dirty or uninstalled WordPress, and its assertion failures
        get attributed to the model instead of the harness.
        """
        _, stderr, returncode, timed_out = self._exec(command)
        if timed_out:
            raise EnvironmentSetupTimeout(
                f"Timed out resetting WordPress with {' '.join(command)} after "
                f"{self.config.timeout_seconds}s (grader.timeout_seconds)."
            )
        if returncode != 0:
            raise RuntimeError(
                f"Reset command failed with exit code {returncode}: "
                f"{' '.join(command)}{': ' + stderr.strip() if stderr.strip() else ''}"
            )

    def execute_code(self, code: str, verification_spec: dict[str, Any]) -> ExecutionResult:
        """Run a candidate PHP snippet through the runtime verifier.

        Compatibility wrapper over execute_artifact() for snippet payloads.
        """
        payload = {
            "payload_version": "1.0",
            "code": code,
            **verification_spec,
        }
        return self._run_verifier(payload)

    def execute_artifact(self, artifact: Any, verification_spec: dict[str, Any]) -> ExecutionResult:
        """Run a candidate artifact (snippet or plugin files) through the verifier.

        Args:
            artifact: An Artifact with kind, code, and optional files map.
            verification_spec: static_checks/runtime_checks for the test.
        """
        payload = {
            "payload_version": "1.0",
            **artifact.payload_fields(),
            **verification_spec,
        }
        return self._run_verifier(payload)

    def _run_verifier(self, payload: dict[str, Any]) -> ExecutionResult:
        """Send a payload to the runtime verifier and parse the result.

        A runtime timeout is a per-test failure, not a harness crash: it
        returns a structured ``ExecutionResult`` with ``timed_out=True`` and
        a synthetic zero-score runtime payload, so the benchmark records the
        timeout and continues with the next test.
        """
        verifier_path = self._runtime_verifier_path()
        cmd = [
            "wp",
            "eval-file",
            verifier_path,
        ]
        stdout, stderr, rc, timed_out = self._exec(cmd, stdin=json.dumps(payload))
        if timed_out:
            return ExecutionResult(
                success=False,
                raw=self._timeout_raw_result(),
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
        data: dict[str, Any] = {}
        if stdout.strip():
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = {"success": False, "fatal_error": "Invalid JSON"}
        success = data.get("success", False) and rc == 0
        return ExecutionResult(success=success, raw=data, stdout=stdout, stderr=stderr)

    # Internal helpers --------------------------------------------------
    def _timeout_raw_result(self) -> dict[str, Any]:
        """Build the stable raw payload recorded for a runtime timeout."""
        return {
            "success": False,
            "timeout": True,
            "runtime": {
                "score": 0.0,
                "details": {
                    "assertions": [
                        {
                            "type": "timeout",
                            "description": "Runtime execution timed out",
                            "passed": False,
                            "timeout_seconds": self.config.timeout_seconds,
                        }
                    ],
                    "total_weight": 0,
                    "passed_weight": 0,
                },
            },
            "static": {"score": 0.0, "details": {}},
        }

    def _runtime_verifier_path(self) -> str:
        if self.config.wp_env_dir:
            return "/var/www/html/wp-content/plugins/runtime/verify-runtime.php"
        return "/var/www/html/wp-content/plugins/wp-bench-runtime/verify-runtime.php"

    def _run_process(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> ProcessResult:
        """Run a subprocess with a hard timeout.

        Central chokepoint for every external command the environment runs:
        no call path that shells out to Docker, wp-env, or WP-CLI may hang
        indefinitely. On ``subprocess.TimeoutExpired`` any partial output
        captured by the exception is preserved.
        """
        try:
            proc = subprocess.run(
                command,
                capture_output=capture_output,
                text=True,
                check=False,
                cwd=cwd,
                timeout=timeout,
                input=stdin,
            )
            return ProcessResult(
                stdout=proc.stdout if capture_output else "",
                stderr=proc.stderr if capture_output else "",
                returncode=proc.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            def _decode(stream: Any) -> str:
                if stream is None:
                    return ""
                if isinstance(stream, bytes):
                    return stream.decode("utf-8", errors="replace")
                return str(stream)

            return ProcessResult(
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                returncode=-1,
                timed_out=True,
            )

    def _container_exists(self) -> bool:
        result = self._run_process(
            [
                "docker",
                "ps",
                "-a",
                "--format",
                "{{.Names}}",
                "--filter",
                f"name={self.config.container_name}",
            ],
            timeout=CONTAINER_QUERY_TIMEOUT_SECONDS,
        )
        if result.timed_out:
            raise EnvironmentSetupTimeout(
                "Timed out querying Docker for existing containers after "
                f"{CONTAINER_QUERY_TIMEOUT_SECONDS}s. Is the Docker daemon responsive?"
            )
        return self.config.container_name in result.stdout.split()

    def _start_container(self) -> None:
        result = self._run_process(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.config.container_name,
                self.config.image,
            ],
            timeout=self.config.timeout_seconds,
        )
        if result.timed_out:
            raise EnvironmentSetupTimeout(
                f"Timed out starting container '{self.config.container_name}' after "
                f"{self.config.timeout_seconds}s (grader.timeout_seconds)."
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start container '{self.config.container_name}': {result.stderr.strip()}"
            )

    def _exec(
        self,
        command: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int, bool]:
        """Execute a command in the WordPress runtime.

        Args:
            command: WP-CLI command to run inside the runtime.
            stdin: Optional data piped to the process. Used for verifier
                payloads, which must not travel as command arguments
                (argv size limits, visible in process listings).
            timeout: Override for the per-command bound. Setup work (which
                includes a full install) is allowed the longer
                ``grader.setup_timeout_seconds`` rather than the per-test
                ``grader.timeout_seconds``.

        Returns:
            Tuple of (stdout, stderr, returncode, timed_out). No path is
            unbounded.
        """
        if timeout is None:
            timeout = self.config.timeout_seconds
        if self.config.wp_env_dir:
            result = self._run_process(
                ["npx", "wp-env", "run", "cli", *command],
                cwd=str(self.config.wp_env_dir),
                timeout=timeout,
                stdin=stdin,
            )
        elif self.config.kind == "cli":
            result = self._run_process(command, timeout=timeout, stdin=stdin)
        else:
            docker_cmd = [
                "docker",
                "exec",
                "-i",
                self.config.container_name,
                *command,
            ]
            result = self._run_process(docker_cmd, timeout=timeout, stdin=stdin)
        return result.stdout, result.stderr, result.returncode, result.timed_out

    def _run_wp_env(self, command: list[str]) -> None:
        """Run a wp-env management command (setup/reset path).

        Raises:
            EnvironmentSetupTimeout: If the command exceeds the configured
                timeout. Setup problems must fail fast and loudly rather
                than being recorded as test results.
            RuntimeError: If the command exits nonzero.
        """
        result = self._run_process(
            command,
            cwd=str(self.config.wp_env_dir),
            timeout=self.config.setup_timeout_seconds,
            capture_output=False,
        )
        if result.timed_out:
            raise EnvironmentSetupTimeout(
                f"Timed out running {' '.join(command)} after "
                f"{self.config.setup_timeout_seconds}s (grader.setup_timeout_seconds)."
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"Command failed with exit code {result.returncode}: {' '.join(command)}"
            )
