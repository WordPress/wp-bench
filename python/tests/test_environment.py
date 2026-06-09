from __future__ import annotations

import base64
import json
from pathlib import Path

from wp_bench.config import GraderConfig
from wp_bench.environment import WordPressEnvironment


def test_execute_code_uses_internal_runtime_verifier_for_wp_env() -> None:
    calls: list[list[str]] = []
    environment = WordPressEnvironment(GraderConfig(kind="docker", wp_env_dir=Path("runtime")))

    def fake_exec(command: list[str]) -> tuple[str, str, int]:
        calls.append(command)
        payload = json.loads(base64.b64decode(command[3]).decode("utf-8"))
        assert payload["code"] == "function demo() { return true; }"
        assert payload["static_checks"] == {"required_patterns": []}
        assert payload["runtime_checks"] == {"assertions": []}
        return ('{"success": true}', "", 0)

    environment._exec = fake_exec  # type: ignore[method-assign]

    result = environment.execute_code(
        "function demo() { return true; }",
        {
            "static_checks": {"required_patterns": []},
            "runtime_checks": {"assertions": []},
        },
    )

    assert result.success is True
    assert calls == [
        [
            "wp",
            "eval-file",
            "/var/www/html/wp-content/plugins/runtime/verify-runtime.php",
            calls[0][3],
        ]
    ]
