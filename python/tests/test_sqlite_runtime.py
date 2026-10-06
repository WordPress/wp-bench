"""SQLite snapshots must restore all state, including WAL and custom tables."""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from wp_bench.config import GraderConfig
from wp_bench.environment import (
    EnvironmentSetupTimeout,
    ProcessResult,
    WordPressEnvironment,
)

SNAPSHOT_SCRIPT = Path(__file__).resolve().parents[2] / "runtime/sqlite-snapshot.php"
FAKE_SNAPSHOT = base64.b64encode(b"SQLite format 3\0" + bytes(512)).decode()


@pytest.mark.parametrize("settings", [{"wp_env_dir": "runtime"}, {"database": "mysql"}])
def test_legacy_database_settings_are_rejected(settings: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        GraderConfig(**settings)


def test_database_metadata_is_fixed_and_read_only() -> None:
    config = GraderConfig()
    assert config.model_dump()["database"] == "sqlite"
    with pytest.raises(AttributeError):
        config.database = "mysql"  # type: ignore[misc, assignment]


def test_sqlite_capture_and_restore_use_host_held_snapshot(monkeypatch) -> None:
    environment = WordPressEnvironment(GraderConfig())
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append((command, kwargs))
        return FAKE_SNAPSHOT, "", 0, False

    monkeypatch.setattr(environment, "_exec", fake_exec)
    environment._capture_baseline()
    environment.reset()

    capture = calls[0][0][2]
    restore = calls[1][0][2]
    assert capture.index("clear") < capture.index("wp core install") < capture.index("export")
    assert "wp db" not in capture + restore
    assert restore.endswith("import && wp core is-installed")
    assert calls[1][1]["stdin"] == FAKE_SNAPSHOT
    assert len(calls) == 2


@pytest.mark.parametrize("snapshot", ["", "not base64", "invalid \N{SNOWMAN}", base64.b64encode(b"wrong header").decode()])
def test_invalid_capture_cannot_become_baseline(monkeypatch, snapshot: str) -> None:
    environment = WordPressEnvironment(GraderConfig())
    monkeypatch.setattr(environment, "_exec", lambda *args, **kwargs: (snapshot, "", 0, False))
    with pytest.raises(RuntimeError, match="baseline"):
        environment._capture_baseline()
    assert environment._baseline is None


def test_setup_starts_stopped_container_and_waits_before_capture(monkeypatch) -> None:
    environment = WordPressEnvironment(GraderConfig())
    steps: list[str] = []
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    def fake_process(command: list[str], **kwargs: Any) -> ProcessResult:
        assert command == ["docker", "start", "wp-bench-grader"]
        steps.append("start")
        return ProcessResult("", "", 0, False)

    monkeypatch.setattr(environment, "_run_process", fake_process)
    monkeypatch.setattr(environment, "_wait_for_container", lambda: steps.append("ready"))
    monkeypatch.setattr(environment, "_require_sqlite", lambda: steps.append("sqlite"))
    monkeypatch.setattr(environment, "_capture_baseline", lambda: steps.append("capture"))
    environment.setup()
    assert steps == ["start", "ready", "sqlite", "capture"]


@pytest.mark.parametrize("kind", ["cli", "docker"])
def test_wrong_backend_aborts_setup(monkeypatch, kind) -> None:
    environment = WordPressEnvironment(GraderConfig(kind=kind))
    monkeypatch.setattr(environment, "_container_exists", lambda: False)
    monkeypatch.setattr(environment, "_start_container", lambda: None)
    monkeypatch.setattr(environment, "_wait_for_container", lambda: None)
    monkeypatch.setattr(environment, "_exec", lambda *args, **kwargs: ("", "SQLite drop-in required", 1, False))
    with pytest.raises(RuntimeError, match="SQLite database backend"):
        environment.setup()


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"Running": True}, "no SQLite readiness healthcheck"),
        ({"Running": False}, "exited during setup"),
    ],
)
def test_incompatible_or_failed_image_aborts_setup(monkeypatch, state, message) -> None:
    environment = WordPressEnvironment(GraderConfig())
    monkeypatch.setattr(
        environment, "_run_process",
        lambda *args, **kwargs: ProcessResult(json.dumps(state), "", 0, False),
    )
    with pytest.raises(RuntimeError, match=message):
        environment._wait_for_container()


def test_readiness_timeout_is_a_setup_failure(monkeypatch) -> None:
    environment = WordPressEnvironment(GraderConfig())
    monkeypatch.setattr(
        environment, "_run_process", lambda *args, **kwargs: ProcessResult("", "", -1, True),
    )
    with pytest.raises(EnvironmentSetupTimeout, match="readiness"):
        environment._wait_for_container()


@pytest.fixture(scope="module")
def php() -> str:
    executable = shutil.which("php")
    if not executable:
        pytest.skip("PHP is required for SQLite snapshot integration tests")
    result = subprocess.run(
        [executable, "-r", 'exit(extension_loaded("pdo_sqlite") ? 0 : 1);'],
        capture_output=True, timeout=10, check=False,
    )
    if result.returncode:
        pytest.skip("PHP PDO SQLite is required for snapshot integration tests")
    return executable


def snapshot(php: str, database: Path, action: str, stdin: str | None = None):
    return subprocess.run(
        [php, str(SNAPSHOT_SCRIPT), action],
        env={**os.environ, "WP_BENCH_SQLITE_PATH": str(database)},
        input=stdin, capture_output=True, text=True, timeout=10, check=False,
    )


def test_snapshot_includes_wal_and_removes_candidate_tables(php: str, tmp_path: Path) -> None:
    database = tmp_path / "wordpress.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE options (name TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO options VALUES ('siteurl', 'baseline')")
        connection.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        exported = snapshot(php, database, "export")
    connection.close()
    assert exported.returncode == 0, exported.stderr
    assert base64.b64decode(exported.stdout).startswith(b"SQLite format 3\0")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE options SET value='candidate'")
        connection.execute("CREATE TABLE candidate_table (id INTEGER)")
    connection.close()
    restored = snapshot(php, database, "import", exported.stdout)
    assert restored.returncode == 0, restored.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM options").fetchone() == ("baseline",)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='candidate_table'").fetchone() is None
    connection.close()
    assert list(tmp_path.glob(".wp-bench-snapshot-*")) == []


@pytest.mark.parametrize("contents", ["", "invalid", base64.b64encode(b"SQLite format 3\0corrupt").decode()])
def test_corrupt_restore_preserves_database_and_cleans_temp_file(php: str, tmp_path: Path, contents: str) -> None:
    database = tmp_path / "wordpress.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE retained (id INTEGER)")
        connection.execute("INSERT INTO retained VALUES (42)")
    connection.close()
    before = database.read_bytes()
    restored = snapshot(php, database, "import", contents)
    assert restored.returncode != 0
    assert database.read_bytes() == before
    assert list(tmp_path.glob(".wp-bench-snapshot-*")) == []


def test_clear_removes_database_and_sidecars(php: str, tmp_path: Path) -> None:
    database = tmp_path / "wordpress.sqlite"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(database) + suffix).write_bytes(b"old state")
    assert snapshot(php, database, "clear").returncode == 0
    assert list(tmp_path.iterdir()) == []
