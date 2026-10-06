"""Bridge between Python harness and WordPress runtime."""
from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .config import GraderConfig

#: Short timeout for cheap local Docker queries (e.g. ``docker ps``).
CONTAINER_QUERY_TIMEOUT_SECONDS = 30


class EnvironmentSetupTimeout(RuntimeError):
    """Raised when Docker environment setup exceeds its timeout.

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
    """Shells out to Docker or WP-CLI to execute verification on SQLite."""

    def __init__(self, config: GraderConfig):
        self.config = config
        #: Clean baseline (base64 SQLite snapshot), held on
        #: the host between setup and reset.
        #: Deliberately not written into the runtime: candidate code is
        #: eval'd there with root, so an on-disk dump would be a file the
        #: graded code could truncate or seed to defeat isolation.
        self._baseline: str | None = None
        self._site_config: str | None = None
        self._image: str = config.image
        self._isolated = False
        self._local = threading.local()
        self._lock = threading.Lock()
        self._owned: dict[str, threading.Lock] = {}
        self._stopping = False
        self._run_id = uuid4().hex

    @property
    def image_id(self) -> str | None:
        """Immutable image used by the isolated run, for result provenance."""
        return self._image if self._isolated else None

    def setup(self, *, capture_baseline: bool = True) -> None:
        """Prepare a pristine private baseline, or start the diagnostic runtime.

        capture_baseline creates a fresh trusted install from the image and
        retains its database/configuration on the host. Existing containers
        are never used for this baseline. False selects the existing shared
        runtime for diagnostic isolation ``none``.
        """
        if capture_baseline:
            if self.config.kind != "docker":
                raise RuntimeError(
                    "grader.kind='cli' has no reset implementation. Use the Docker grader "
                    "for reset_per_test, or execution_isolation='none' for diagnostic runs."
                )
            self._isolated = True
            self._resolve_image()
            try:
                self._start_isolated_container(install=True)
                self._wait_for_container()
                self._require_sqlite()
                self._capture_baseline()
                stdout, stderr, rc, timed_out = self._exec(["cat", "wp-config.php"])
                if timed_out or rc != 0 or not stdout.startswith("<?php"):
                    raise RuntimeError(f"Could not capture clean WordPress configuration: {stderr}")
                self._site_config = stdout
            finally:
                self.release()
            return
        if self.config.kind == "docker":
            if not self._container_exists():
                self._start_container()
            else:
                # docker start also succeeds for an already-running container.
                result = self._run_process(
                    ["docker", "start", self.config.container_name],
                    timeout=self.config.setup_timeout_seconds,
                )
                if result.timed_out:
                    raise EnvironmentSetupTimeout("Timed out starting the existing grader container.")
                if result.returncode != 0:
                    raise RuntimeError(f"Failed to start grader container: {result.stderr.strip()}")
            self._wait_for_container()
            self._require_sqlite()
        else:
            # A cli grader drives a runtime the harness does not own, so there
            # is nothing to install and nothing to capture. Verify the backend
            # before recording it in result metadata.
            self._require_sqlite()
            return

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

        Install once and replay a snapshot per test. The snapshot travels on
        stdout/stdin and stays on the host, outside candidate code's reach.
        Install-time timestamps freeze at run start. SQLite snapshots include
        the adapter's schema metadata and committed WAL contents.
        """
        clear = shlex.join(self._sqlite_snapshot_command("clear"))
        export = shlex.join(self._sqlite_snapshot_command("export"))
        script = " && ".join(
            [
                f"{clear} >/dev/null",
                f"{shlex.join(self._install_command())} >/dev/null",
                export,
            ]
        )
        stdout, stderr, returncode, timed_out = self._exec(
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
        # Every restore decodes, header-checks and integrity-checks the
        # snapshot before replacing the database, so a bad capture fails the
        # first reset instead of grading against it.
        self._baseline = stdout

    def reset(self) -> None:
        """Create a private runtime from the image and host-held SQLite baseline.

        The old container, its private filesystems, and all its processes are
        removed. Each thread owns a distinct container; even serial executions
        use a fresh filesystem, process namespace, and network namespace.
        """
        if self.config.kind != "docker":
            # Nothing rejects this pairing at config load, so it reaches here
            # on a real run. Refuse rather than let the run stamp an isolation
            # guarantee it never delivered.
            raise RuntimeError(
                f"grader.kind {self.config.kind!r} has no reset implementation, so "
                "run.execution_isolation 'reset_per_test' cannot be honored."
            )
        if self._baseline is None:
            raise RuntimeError(
                "No clean baseline was captured, so reset() cannot restore one. "
                "Call setup(capture_baseline=True) before grading."
            )
        if not self._isolated or self._site_config is None:
            raise RuntimeError("No isolated runtime baseline was prepared. Call setup() first.")
        self.release()
        try:
            self._start_isolated_container(install=False)
            restore = shlex.join(self._sqlite_snapshot_command("prepare"))
            script = (
                "cp -R /opt/wp-bench-wordpress/. /var/www/html/ && "
                f"{restore} && wp core is-installed"
            )
            self._reset_step(
                ["sh", "-c", script],
                stdin=json.dumps({"config": self._site_config, "database": self._baseline}),
            )
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        """Discard this thread's candidate runtime, including timeout survivors."""
        name = getattr(self._local, "container", None)
        if name is not None:
            self._remove_owned_container(name)
            del self._local.container

    def close(self) -> None:
        """Stop accepting work and remove every container owned by this run."""
        with self._lock:
            self._stopping = True
            names = list(self._owned)
        failures = []
        for name in names:
            try:
                self._remove_owned_container(name)
            except (OSError, RuntimeError) as error:
                failures.append(error)
        if failures:
            raise RuntimeError(f"Could not clean up isolated runtimes: {failures}") from failures[0]

    def _remove_owned_container(self, name: str) -> None:
        with self._lock:
            lifecycle = self._owned.get(name)
        if lifecycle is None:
            return
        # A concurrent abort must wait for docker run to finish before removing
        # its container; otherwise creation could finish after cleanup.
        with lifecycle:
            with self._lock:
                if name not in self._owned:
                    return
            result = self._run_process(
                ["docker", "rm", "-f", "-v", name], timeout=CONTAINER_QUERY_TIMEOUT_SECONDS,
            )
            if result.timed_out or (
                result.returncode != 0 and "No such container" not in result.stderr
            ):
                raise RuntimeError(f"Failed to remove isolated container {name}: {result.stderr}")
            with self._lock:
                self._owned.pop(name, None)

    def _resolve_image(self) -> None:
        result = self._run_process(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.config.image],
            timeout=CONTAINER_QUERY_TIMEOUT_SECONDS,
        )
        if result.timed_out:
            raise EnvironmentSetupTimeout("Timed out resolving the grader image.")
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(f"Could not resolve grader image {self.config.image}: {result.stderr}")
        # A tag may be rebuilt during a long run. All candidates use one image.
        self._image = result.stdout.strip()

    def _runtime_container(self) -> str:
        if not self._isolated:
            return self.config.container_name
        name = getattr(self._local, "container", None)
        if name is None:
            raise RuntimeError("No private candidate runtime exists. Call reset() before execution.")
        return str(name)

    def _start_isolated_container(self, *, install: bool) -> None:
        name = f"wp-bench-{self._run_id}-{uuid4().hex[:12]}"
        lifecycle = threading.Lock()
        with lifecycle:
            with self._lock:
                if self._stopping:
                    raise RuntimeError("The isolated run has been stopped.")
                self._owned[name] = lifecycle
            self._local.container = name
            command = [
                "docker", "run", "-d", "--init", "--rm", "--name", name,
                "--label", f"org.wordpress.wp-bench.run={self._run_id}",
                "--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "www-data",
                "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                "--tmpfs", (
                    "/var/www/html:rw,nosuid,nodev,mode=1777,"
                    f"size={self.config.container_filesystem_mb}m"
                ),
                "--cpus", str(self.config.container_cpus),
                "--memory", f"{self.config.container_memory_mb}m",
                "--memory-swap", f"{self.config.container_memory_mb}m",
                "--pids-limit", str(self.config.container_pids_limit),
                "--env", "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache", "--no-healthcheck",
            ]
            if not install:
                command.extend(["--entrypoint", "sleep"])
            command.extend([self._image, "infinity"] if not install else [self._image])
            result = self._run_process(command, timeout=self.config.setup_timeout_seconds)
            if result.timed_out:
                raise EnvironmentSetupTimeout(f"Timed out starting isolated container {name}.")
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start isolated container {name}: {result.stderr}")

    def _require_sqlite(self) -> None:
        """Fail setup if an external CLI or old image loads another backend."""
        _, stderr, returncode, timed_out = self._exec(
            [
                "wp",
                "eval",
                (
                    "global $wpdb; if ( ! $wpdb instanceof WP_SQLite_DB ) { "
                    "WP_CLI::error( 'WP-Bench requires the SQLite database drop-in.' ); }"
                ),
            ],
            timeout=self.config.setup_timeout_seconds,
        )
        if timed_out:
            raise EnvironmentSetupTimeout("Timed out verifying the SQLite database backend.")
        if returncode != 0:
            raise RuntimeError(f"Could not verify the SQLite database backend: {stderr.strip()}")

    def _sqlite_snapshot_command(self, action: str) -> list[str]:
        return [
            "php",
            "/var/www/html/wp-content/plugins/wp-bench-runtime/sqlite-snapshot.php",
            action,
        ]

    def _reset_step(self, command: list[str], *, stdin: str | None = None) -> None:
        """Run one reset command, failing loudly.

        A reset that times out or exits nonzero leaves the next test running
        against dirty or uninstalled WordPress, and its assertion failures
        get attributed to the model instead of the harness.
        """
        _, stderr, returncode, timed_out = self._exec(command, stdin=stdin)
        # shlex.join, not ' '.join: the command is an ``sh -c`` invocation, and
        # naive joining renders a string that runs the reset against the
        # operator's own WordPress if they paste it into a host shell.
        rendered = shlex.join(command)
        if timed_out:
            raise EnvironmentSetupTimeout(
                f"Timed out resetting WordPress with {rendered} after "
                f"{self.config.timeout_seconds}s (grader.timeout_seconds)."
            )
        if returncode != 0:
            raise RuntimeError(
                f"Reset command failed with exit code {returncode}: "
                f"{rendered}{': ' + stderr.strip() if stderr.strip() else ''}"
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
        try:
            stdout, stderr, rc, timed_out = self._exec(cmd, stdin=json.dumps(payload))
        finally:
            if self._isolated:
                self.release()
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
        no call path that shells out to Docker or WP-CLI may hang
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
                "--init",
                "--name",
                self.config.container_name,
                self.config.image,
            ],
            timeout=self.config.setup_timeout_seconds,
        )
        if result.timed_out:
            raise EnvironmentSetupTimeout(
                f"Timed out starting container '{self.config.container_name}' after "
                f"{self.config.setup_timeout_seconds}s (grader.setup_timeout_seconds)."
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start container '{self.config.container_name}': {result.stderr.strip()}"
            )

    def _wait_for_container(self) -> None:
        """Wait for the entrypoint's install before attempting isolation.

        The healthcheck marks completion of the entrypoint's install.
        """
        deadline = time.monotonic() + self.config.setup_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EnvironmentSetupTimeout("Timed out waiting for the SQLite grader to be ready.")
            result = self._run_process(
                ["docker", "inspect", "--format", "{{json .State}}", self._runtime_container()],
                timeout=min(remaining, CONTAINER_QUERY_TIMEOUT_SECONDS),
            )
            if result.timed_out:
                raise EnvironmentSetupTimeout("Timed out querying the SQLite grader's readiness.")
            if result.returncode != 0:
                raise RuntimeError(f"Could not inspect grader container: {result.stderr.strip()}")
            state = json.loads(result.stdout)
            if not state.get("Running"):
                logs = self._run_process(
                    ["docker", "logs", "--tail", "30", self._runtime_container()],
                    timeout=CONTAINER_QUERY_TIMEOUT_SECONDS,
                )
                raise RuntimeError(f"SQLite grader exited during setup: {logs.stdout}{logs.stderr}")
            health = state.get("Health", {}).get("Status")
            if self._isolated:
                ready = self._run_process(
                    ["docker", "exec", self._runtime_container(), "test", "-f", "/tmp/wp-bench-ready"],
                    timeout=min(remaining, CONTAINER_QUERY_TIMEOUT_SECONDS),
                )
                if ready.timed_out:
                    raise EnvironmentSetupTimeout("Timed out querying baseline readiness.")
                if ready.returncode == 0:
                    return
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                continue
            if health == "healthy":
                return
            if health is None:
                raise RuntimeError(
                    "Grader image has no SQLite readiness healthcheck. Rebuild ./runtime "
                    "and recreate the container."
                )
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

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
        if self.config.kind == "cli":
            result = self._run_process(command, timeout=timeout, stdin=stdin)
        else:
            docker_cmd = [
                "docker",
                "exec",
                "-i",
                self._runtime_container(),
                *command,
            ]
            result = self._run_process(docker_cmd, timeout=timeout, stdin=stdin)
        return result.stdout, result.stderr, result.returncode, result.timed_out
