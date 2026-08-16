"""Tests for pooled ``reset_per_test`` isolation.

``reset_per_test`` used to force serial execution: one WordPress runtime was
reset before every test, so two concurrent tests would have been grading
against each other's state. The constraint was never fundamental — it was a
consequence of there being one database. Give every worker its own and the
guarantee gets *stronger* under concurrency: a serial run time-slices one
mutable runtime, a pooled run never lets two tests touch the same one.

What these tests protect is the seam that makes that true — that a worker's
reset, its verifier run, and its provisioning all name the same database, and
that worker 0 keeps sending exactly the commands a pre-pooling run sent.
"""
from __future__ import annotations

from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import (
    EnvironmentSetupTimeout,
    WordPressEnvironment,
)

#: Stand-in for the captured clean baseline. The dump lives on the host and
#: reaches the runtime over stdin, so reset() refuses without one.
BASELINE_SQL = "-- baseline\nCREATE TABLE wp_options (id INT);\n"


def _env(config: GraderConfig, result: tuple[str, str, int, bool] = ("ok", "", 0, False)):
    """An environment whose runtime commands return a canned result."""
    environment = WordPressEnvironment(config)
    calls: list[list[str]] = []
    stdins: list[str | None] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append(command)
        stdins.append(kwargs.get("stdin"))
        return result

    environment._exec = fake_exec  # type: ignore[method-assign]
    environment._baseline = BASELINE_SQL
    environment.stdins = stdins  # type: ignore[attr-defined]
    return environment, calls


def _script(call: list[str]) -> str:
    """The shell script body from an ``sh -c`` invocation."""
    assert call[:2] == ["sh", "-c"], f"expected an sh -c invocation, got {call!r}"
    return call[2]


def _docker() -> GraderConfig:
    return GraderConfig(kind="docker")


# Slot naming -----------------------------------------------------------


def test_worker_zero_uses_the_runtimes_own_database() -> None:
    """Worker 0 must not be a pool member. It is what makes a serial run a
    no-op: nothing to provision, no command to rewrite."""
    environment = WordPressEnvironment(_docker())

    assert environment.database_name(0) is None


def test_workers_above_zero_get_their_own_database() -> None:
    environment = WordPressEnvironment(_docker())

    assert environment.database_name(1) == "wp_bench_w1"
    assert environment.database_name(3) == "wp_bench_w3"


def test_worker_databases_are_distinct_per_slot() -> None:
    """Two slots resolving to one name would silently share a runtime while
    the results file still claimed per-test isolation."""
    environment = WordPressEnvironment(_docker())

    names = [environment.database_name(worker) for worker in range(8)]

    assert len(set(names)) == len(names)


def test_negative_worker_slot_is_rejected() -> None:
    environment = WordPressEnvironment(_docker())

    with pytest.raises(ValueError, match="worker slot must be >= 0"):
        environment.database_name(-1)


# Reset -----------------------------------------------------------------


def test_worker_zero_reset_is_unchanged_by_pooling() -> None:
    """The serial command string is the compatibility contract: a run that
    does not opt into concurrency must be indistinguishable from before."""
    environment, calls = _env(_docker())

    environment.reset(0)

    assert _script(calls[0]) == (
        "wp db reset --yes && wp db import - && wp core is-installed"
    )


def test_default_reset_targets_worker_zero() -> None:
    with_default, default_calls = _env(_docker())
    explicit, explicit_calls = _env(_docker())

    with_default.reset()
    explicit.reset(0)

    assert default_calls == explicit_calls


def test_pooled_reset_targets_that_workers_database() -> None:
    environment, calls = _env(_docker())

    environment.reset(3)

    assert "export WORDPRESS_DB_NAME=wp_bench_w3;" in _script(calls[0])


def test_pooled_reset_applies_the_database_to_both_steps() -> None:
    """``export`` rather than a one-command assignment: the drop and the
    import must land in the same database, or the reset restores the
    baseline over the runtime's default one."""
    script = _script(_pooled_reset_call(2))

    assert script.startswith("export WORDPRESS_DB_NAME=wp_bench_w2;")
    assert script.index("export") < script.index("wp db reset")
    assert script.index("export") < script.index("wp db import")


def test_pooled_reset_is_still_a_single_round_trip() -> None:
    """Pooling must not cost what template restore just bought back."""
    environment, calls = _env(_docker())

    environment.reset(2)

    assert len(calls) == 1


def test_pooled_reset_still_drops_before_it_restores() -> None:
    script = _script(_pooled_reset_call(2))

    assert script.index("wp db reset") < script.index("wp db import")
    assert "&&" in script


def test_pooled_reset_restores_the_shared_baseline() -> None:
    """Every worker starts from the same dump, or the pool would grade
    identical tests against different WordPress states."""
    assert "wp db import -" in _script(_pooled_reset_call(2))


def _pooled_reset_call(worker: int) -> list[str]:
    environment, calls = _env(_docker())
    environment.reset(worker)
    return calls[0]


def test_failed_pooled_reset_still_raises() -> None:
    """A worker whose reset failed would grade the next test on its slot
    against a dirty database."""
    environment, _ = _env(_docker(), ("", "MySQL server has gone away", 1, False))

    with pytest.raises(RuntimeError, match="MySQL server has gone away"):
        environment.reset(2)


def test_pooled_reset_timeout_still_raises() -> None:
    environment, _ = _env(_docker(), ("", "", 0, True))

    with pytest.raises(EnvironmentSetupTimeout, match="Timed out resetting"):
        environment.reset(2)


def test_cli_grader_refuses_a_pooled_reset_too() -> None:
    """Pooling is not a way around a grader that cannot reset at all."""
    environment = WordPressEnvironment(GraderConfig(kind="cli"))

    with pytest.raises(RuntimeError, match="no reset implementation"):
        environment.reset(2)


# Verification ----------------------------------------------------------


def test_worker_zero_verifier_command_is_unchanged_by_pooling() -> None:
    environment, calls = _env(_docker())

    environment.execute_code("code", {}, worker=0)

    assert calls[0] == [
        "wp",
        "eval-file",
        "/var/www/html/wp-content/plugins/wp-bench-runtime/verify-runtime.php",
    ]


def test_pooled_verifier_runs_against_that_workers_database() -> None:
    """The candidate has to be graded where its reset happened. Grading on
    the default database would score it against a runtime other tests are
    concurrently mutating."""
    environment, calls = _env(_docker())

    environment.execute_code("code", {}, worker=3)

    assert "export WORDPRESS_DB_NAME=wp_bench_w3;" in _script(calls[0])
    assert "wp eval-file" in _script(calls[0])


def test_pooled_verifier_keeps_the_payload_on_stdin() -> None:
    """Payloads must never travel as argv (argv size limits, and they are
    visible in process listings). The database override must not become an
    excuse to inline one."""
    environment = WordPressEnvironment(_docker())
    seen: dict[str, Any] = {}

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        seen["command"] = command
        seen["stdin"] = kwargs.get("stdin")
        return ("{}", "", 0, False)

    environment._exec = fake_exec  # type: ignore[method-assign]

    environment.execute_code("function demo() {}", {"static_checks": {}}, worker=2)

    assert "function demo() {}" in seen["stdin"]
    assert not any("function demo" in part for part in seen["command"])


def test_pooled_verifier_execs_so_stdin_reaches_wp_cli() -> None:
    """The wrapper shell must replace itself with WP-CLI rather than fork
    it, or the payload is piped into a shell that has already exited."""
    environment, calls = _env(_docker())

    environment.execute_code("code", {}, worker=2)

    assert "exec wp eval-file" in _script(calls[0])


def test_pooled_verifier_uses_the_wp_env_verifier_path(tmp_path) -> None:
    """wp-env mounts the runtime plugin at a different path than the Docker
    grader image; the wrapper must not hardcode one."""
    environment, calls = _env(GraderConfig(kind="docker", wp_env_dir=tmp_path))

    environment.execute_code("code", {}, worker=2)

    assert "/wp-content/plugins/runtime/verify-runtime.php" in _script(calls[0])


# Provisioning ----------------------------------------------------------


def _setup_calls(
    monkeypatch: pytest.MonkeyPatch,
    worker_count: int,
    config: GraderConfig | None = None,
    provision_result: tuple[str, str, int, bool] = ("ok", "", 0, False),
) -> list[list[str]]:
    """Run setup(), letting the baseline capture succeed.

    ``provision_result`` is returned for every call *after* the capture, so a
    provisioning failure cannot be mistaken for a capture failure.
    """
    environment = WordPressEnvironment(config or _docker())
    calls: list[list[str]] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append(command)
        return ("ok", "", 0, False) if len(calls) == 1 else provision_result

    environment._exec = fake_exec  # type: ignore[method-assign]
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    monkeypatch.setattr(environment, "_run_wp_env", lambda command: None)
    environment.setup(worker_count=worker_count)
    return calls


def test_serial_setup_provisions_no_extra_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that did not ask for concurrency must not gain a code path."""
    calls = _setup_calls(monkeypatch, 1)

    assert len(calls) == 1
    assert "WORDPRESS_DB_NAME" not in _script(calls[0])


def test_pooled_setup_provisions_one_database_per_worker_above_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _setup_calls(monkeypatch, 4)

    provisioned = [_script(call) for call in calls[1:]]
    assert len(provisioned) == 3
    for worker, script in enumerate(provisioned, start=1):
        assert f"export WORDPRESS_DB_NAME=wp_bench_w{worker};" in script


def test_pooled_setup_captures_the_baseline_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One install, one dump, replayed into every worker. Installing per
    worker would give each a different set of install-time timestamps."""
    calls = _setup_calls(monkeypatch, 4)

    installs = [call for call in calls if "wp core install" in _script(call)]
    assert len(installs) == 1
    assert "WORDPRESS_DB_NAME" not in _script(installs[0])


def test_pooled_setup_seeds_every_worker_from_that_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _setup_calls(monkeypatch, 4)

    for call in calls[1:]:
        assert "wp db import -" in _script(call)


def test_provisioning_creates_the_database_before_restoring_into_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script(_setup_calls(monkeypatch, 2)[1])

    assert script.index("wp db create") < script.index("wp db import")


def test_provisioning_tolerates_a_database_left_by_an_earlier_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted run leaves its worker databases behind. Re-creating
    one fails, which is fine and expected — the restore that follows is what
    makes a leftover indistinguishable from a fresh database, so it must not
    be short-circuited by the create."""
    script = _script(_setup_calls(monkeypatch, 2)[1])

    _, _, restore = script.partition("wp db create")
    assert restore.lstrip().startswith(";"), (
        "wp db create must not be &&-chained to the restore: an existing "
        f"database would abort provisioning. Got: {script!r}"
    )
    assert "wp db reset --yes" in restore


def test_failed_provisioning_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker whose database never got the baseline would grade every test
    on its slot against an empty WordPress and blame the model."""
    with pytest.raises(RuntimeError, match="no space left on device"):
        _setup_calls(monkeypatch, 4, provision_result=("", "no space left on device", 1, False))


def test_failed_provisioning_names_the_worker_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="wp_bench_w1"):
        _setup_calls(monkeypatch, 4, provision_result=("", "boom", 1, False))


def test_provisioning_timeout_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(EnvironmentSetupTimeout, match="provisioning worker database"):
        _setup_calls(monkeypatch, 4, provision_result=("", "", 0, True))


def test_cli_grader_provisions_nothing() -> None:
    """A cli grader owns no runtime, so there is nothing to pool. It must
    stay a no-op rather than create databases it cannot reset."""
    environment, calls = _env(GraderConfig(kind="cli"))

    environment.setup(worker_count=4)

    assert calls == []
