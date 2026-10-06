"""Run the entrypoint shell with PHP config evaluation and stubbed services."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "runtime/docker-entrypoint.sh"

# Keep MySQL and WordPress installation out of this test, but execute the
# actual entrypoint and evaluate its resulting DB_NAME expression with PHP.
SERVICES = r"""
cd() { [ "$1" = /var/www/html ]; }
mysql() { return 0; }
wp() {
    case "$1 $2" in
        "config get") php -r 'require "wp-config.php"; echo DB_NAME;' ;;
        "config set") printf "<?php define('DB_NAME', %s);\n" "$4" > wp-config.php ;;
        "core is-installed"|"plugin activate") return 0 ;;
        *) return 1 ;;
    esac
}
source "$1" true
"""


@pytest.mark.parametrize(
    ("db_name", "dynamic_config", "exported_db"),
    [
        ("customdb", False, None),
        ("customdb", True, None),
        ("customdb", True, ""),
        ("team's\\site", False, None),
    ],
)
def test_entrypoint_preserves_existing_database(
    tmp_path: Path, db_name: str, dynamic_config: bool, exported_db: str | None,
) -> None:
    expression = json.dumps(db_name)
    if dynamic_config:
        expression = f"getenv('WORDPRESS_DB_NAME') ?: {expression}"
    (tmp_path / "wp-config.php").write_text(f"<?php define('DB_NAME', {expression});\n")
    env = {key: value for key, value in os.environ.items() if key != "WORDPRESS_DB_NAME"}
    if exported_db is not None:
        env["WORDPRESS_DB_NAME"] = exported_db

    # A second startup must preserve the fallback written by the first.
    for _ in range(2):
        subprocess.run(
            ["bash", "-c", SERVICES, "entrypoint-test", str(ENTRYPOINT)],
            cwd=tmp_path, env=env, check=True, capture_output=True, text=True, timeout=10,
        )
        result = subprocess.run(
            ["php", "-r", 'require "wp-config.php"; echo DB_NAME;'],
            cwd=tmp_path, env=env, check=True, capture_output=True, text=True, timeout=10,
        )
        assert result.stdout == db_name

    result = subprocess.run(
        ["php", "-r", 'require "wp-config.php"; echo DB_NAME;'],
        cwd=tmp_path, env={**env, "WORDPRESS_DB_NAME": "wp_bench_worker"},
        check=True, capture_output=True, text=True, timeout=10,
    )
    assert result.stdout == "wp_bench_worker"
