"""Bridge between Python harness and WordPress runtime."""
from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from typing import Any, Dict

from .config import GraderConfig


@dataclass
class ExecutionResult:
    success: bool
    raw: Dict[str, Any]
    stdout: str
    stderr: str


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
        if self.config.wp_env_dir:
            self._run_wp_env(["npx", "wp-env", "run", "cli", "wp", "db", "reset", "--yes"])
        elif self.config.kind == "docker":
            self._exec(["wp", "db", "reset", "--yes"])

    def execute_code(self, code: str, verification_spec: Dict[str, Any]) -> ExecutionResult:
        payload = {
            "code": code,
            **verification_spec,
        }
        encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
        cmd = [
            "wp",
            "bench",
            "verify",
            f"--payload={encoded}",
            "--format=json",
        ]
        stdout, stderr, rc = self._exec(cmd)
        data: Dict[str, Any] = {}
        if stdout.strip():
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = {"success": False, "fatal_error": "Invalid JSON"}
        success = data.get("success", False) and rc == 0
        return ExecutionResult(success=success, raw=data, stdout=stdout, stderr=stderr)

    # Internal helpers --------------------------------------------------
    def _container_exists(self) -> bool:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--format",
                "{{.Names}}",
                "--filter",
                f"name={self.config.container_name}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return self.config.container_name in result.stdout.split()

    def _start_container(self) -> None:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.config.container_name,
                self.config.image,
            ],
            check=True,
        )

    def _exec(self, command: list[str]) -> tuple[str, str, int]:
        if self.config.wp_env_dir:
            proc = subprocess.run(
                ["npx", "wp-env", "run", "cli", *command],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(self.config.wp_env_dir),
            )
            return proc.stdout, proc.stderr, proc.returncode

        if self.config.kind == "cli":
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            return proc.stdout, proc.stderr, proc.returncode

        docker_cmd = [
            "docker",
            "exec",
            "-i",
            self.config.container_name,
            *command,
        ]
        proc = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout, proc.stderr, proc.returncode

    def _run_wp_env(self, command: list[str]) -> None:
        subprocess.run(
            command,
            check=True,
            cwd=str(self.config.wp_env_dir),
        )
