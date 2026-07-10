"""Bridge between Python harness and WordPress runtime."""
from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import GraderConfig

#: Short timeout for cheap local Docker queries (e.g. ``docker ps``).
CONTAINER_QUERY_TIMEOUT_SECONDS = 30


class EnvironmentSetupTimeout(RuntimeError):
    """Raised when environment setup (wp-env/Docker) exceeds its timeout.

    Setup timeouts are harness/environment failures, not per-test results,
    so they fail fast with a clear message instead of being recorded as a
    test outcome.
    """


@dataclass
class ExecutionResult:
    success: bool
    raw: Dict[str, Any]
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
            return
        if self.config.kind != "docker":
            return
        if not self._container_exists():
            self._start_container()

    def reset(self) -> None:
        """Restore the WordPress runtime to a known clean baseline.

        ``wp db reset`` drops every table, which leaves WordPress uninstalled,
        so a fresh ``wp core install`` follows to return to a deterministic
        just-installed state. Called before every execution test when
        ``run.execution_isolation`` is ``reset_per_test`` so no test can
        observe database state (options, posts, roles, transients, cron
        events, etc.) left behind by an earlier test or model run.
        """
        install_cmd = [
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
        if self.config.wp_env_dir:
            self._run_wp_env(["npx", "wp-env", "run", "cli", "wp", "db", "reset", "--yes"])
            self._run_wp_env(["npx", "wp-env", "run", "cli", *install_cmd])
        elif self.config.kind == "docker":
            self._exec(["wp", "db", "reset", "--yes"])
            self._exec(install_cmd)

    def execute_code(self, code: str, verification_spec: Dict[str, Any]) -> ExecutionResult:
        """Run candidate code through the runtime verifier.

        A runtime timeout is a per-test failure, not a harness crash: it
        returns a structured ``ExecutionResult`` with ``timed_out=True`` and
        a synthetic zero-score runtime payload, so the benchmark records the
        timeout and continues with the next test.
        """
        payload = {
            "code": code,
            **verification_spec,
        }
        encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
        verifier_path = self._runtime_verifier_path()
        cmd = [
            "wp",
            "eval-file",
            verifier_path,
            encoded,
        ]
        stdout, stderr, rc, timed_out = self._exec(cmd)
        if timed_out:
            return ExecutionResult(
                success=False,
                raw=self._timeout_raw_result(),
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
        data: Dict[str, Any] = {}
        if stdout.strip():
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = {"success": False, "fatal_error": "Invalid JSON"}
        success = data.get("success", False) and rc == 0
        return ExecutionResult(success=success, raw=data, stdout=stdout, stderr=stderr)

    # Internal helpers --------------------------------------------------
    def _timeout_raw_result(self) -> Dict[str, Any]:
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
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        capture_output: bool = True,
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

    def _exec(self, command: list[str]) -> tuple[str, str, int, bool]:
        """Execute a command in the WordPress runtime.

        Returns:
            Tuple of (stdout, stderr, returncode, timed_out). All paths are
            bounded by ``grader.timeout_seconds``.
        """
        timeout = self.config.timeout_seconds
        if self.config.wp_env_dir:
            result = self._run_process(
                ["npx", "wp-env", "run", "cli", *command],
                cwd=str(self.config.wp_env_dir),
                timeout=timeout,
            )
        elif self.config.kind == "cli":
            result = self._run_process(command, timeout=timeout)
        else:
            docker_cmd = [
                "docker",
                "exec",
                "-i",
                self.config.container_name,
                *command,
            ]
            result = self._run_process(docker_cmd, timeout=timeout)
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
