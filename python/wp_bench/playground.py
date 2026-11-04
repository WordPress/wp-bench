"""Disposable WordPress Playground processes for PHP execution tests."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading

from .config import GraderConfig
from .environment import EnvironmentSetupTimeout, WordPressEnvironment


class PlaygroundEnvironment(WordPressEnvironment):
    """Use a fresh WASM filesystem and SQLite install for every candidate."""

    def __init__(self, config: GraderConfig):
        super().__init__(config)
        self._isolated = True
        self._processes: set[subprocess.Popen[str]] = set()
        self._workdirs: dict[subprocess.Popen[str], tempfile.TemporaryDirectory[str]] = {}

    @property
    def image_id(self) -> None:
        return None

    def setup(self, *, capture_baseline: bool = True) -> None:
        if not capture_baseline:
            raise ValueError("Playground requires run.execution_isolation='reset_per_test'.")
        bridge = self.config.playground_directory / "verify.mjs"
        if not bridge.is_file():
            raise RuntimeError(f"Playground bridge not found: {bridge}")
        # Fail before model calls if dependencies, downloads or boot are broken.
        self.reset()
        self.release()

    def reset(self) -> None:
        self.release()
        diagnostics = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 - lives until release()
        with self._lock:
            if self._stopping:
                diagnostics.close()
                raise RuntimeError("The Playground run has been stopped.")
            workdir = tempfile.TemporaryDirectory(prefix="wp-bench-playground-")
            try:
                process = subprocess.Popen(
                    [self.config.playground_node,
                     str(self.config.playground_directory / "verify.mjs"),
                     self.config.playground_wordpress_version, self.config.base_url],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics,
                    text=True,
                    env={**os.environ, "TMPDIR": workdir.name,
                         "TMP": workdir.name, "TEMP": workdir.name},
                )
            except BaseException:
                workdir.cleanup()
                diagnostics.close()
                raise
            self._processes.add(process)
            self._workdirs[process] = workdir
        self._local.process = process
        self._local.diagnostics = diagnostics
        ready: queue.Queue[str] = queue.Queue()
        stdout = process.stdout
        assert stdout is not None
        threading.Thread(target=lambda: ready.put(stdout.readline()), daemon=True).start()
        try:
            line = ready.get(timeout=self.config.setup_timeout_seconds)
            if line != "WP_BENCH_READY\n":
                process.wait(timeout=5)
                diagnostics.seek(0)
                raise RuntimeError(f"Could not boot Playground: {diagnostics.read()}")
        except queue.Empty as error:
            self.release()
            raise EnvironmentSetupTimeout("Timed out booting WordPress Playground.") from error
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        process = getattr(self._local, "process", None)
        if process is None:
            return
        self._stop(process)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()
        self._local.diagnostics.close()
        del self._local.process
        del self._local.diagnostics

    def _stop(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if process.poll() is None:
                process.kill()
            process.wait()
            self._processes.discard(process)
            workdir = self._workdirs.pop(process, None)
            if workdir is not None:
                workdir.cleanup()

    def close(self) -> None:
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        for process in processes:
            self._stop(process)
        self.release()

    def _exec(
        self, command: list[str], *, stdin: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int, bool]:
        process = getattr(self._local, "process", None)
        if process is None:
            raise RuntimeError("Call reset() before Playground execution.")
        payload = json.loads(stdin or "{}")
        if payload.get("artifact_kind") == "wp_cli_shell":
            raise ValueError("Playground does not support wp_cli_shell; use the Docker grader.")
        try:
            stdout, _ = process.communicate(
                input=json.dumps(payload) + "\n",
                timeout=timeout or self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            self._stop(process)
            return "", "Playground execution timed out", -1, True
        diagnostics = self._local.diagnostics
        diagnostics.seek(0)
        stderr = diagnostics.read()
        if process.returncode != 0:
            return stdout, stderr, process.returncode, False
        try:
            response = json.loads(stdout)
            return response["stdout"], stderr + response["stderr"], response["returncode"], False
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise RuntimeError(f"Invalid Playground bridge response: {stdout}; {stderr}") from error
