"""Tests for pooled ``reset_per_test`` isolation.

``reset_per_test`` used to force serial execution: one WordPress runtime was
reset before every test, so two concurrent tests would have been grading
against each other's state. The constraint was never fundamental — it was a
consequence of there being one database. Give every worker its own and the
guarantee holds under concurrency: a serial run time-slices one database, a
pooled run never resets or grades two concurrent tests against the same one.

What these tests protect is the seam that makes that true — that a worker's
reset, its verifier run, and its provisioning all name the same database,
that the runtime is proven to honor the override rather than assumed to, and
that worker 0 keeps sending exactly the commands a pre-pooling run sent.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import (
    WORKER_TEARDOWN_TIMEOUT_SECONDS,
    EnvironmentSetupTimeout,
    WordPressEnvironment,
)

#: Stand-in for the captured clean baseline. The dump lives on the host and
#: reaches the runtime over stdin, so reset() refuses without one.
BASELINE_SQL = "-- baseline\nCREATE TABLE wp_options (id INT);\n"

#: Fixed so database names are predictable; real runs get a random one.
RUN_ID = "testrun"


def _worker_db(worker: int) -> str:
    return f"wp_bench_{RUN_ID}_w{worker}"


def _dropped_names(script: str) -> list[str]:
    """Every database a DROP statement names, in order."""
    return re.findall(r"DROP DATABASE IF EXISTS `([^`]+)`", script)


def _database_in(script: str) -> str:
    """The database a script pins itself to, or '' for the default."""
    if "WORDPRESS_DB_NAME=" not in script:
        return ""
    return script.split("WORDPRESS_DB_NAME=", 1)[1].split(";", 1)[0].strip()


def _env(config: GraderConfig, result: tuple[str, str, int, bool] = ("ok", "", 0, False)):
    """An environment whose runtime commands return a canned result.

    The verification probe is answered honestly (it echoes back whichever
    database the script pinned itself to) so tests exercising reset and the
    verifier are not fighting the setup-time check.
    """
    environment = WordPressEnvironment(config, run_id=RUN_ID)
    calls: list[list[str]] = []
    stdins: list[str | None] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append(command)
        stdins.append(kwargs.get("stdin"))
        if "SELECT DATABASE()" in command[-1]:
            return (_database_in(command[-1]) or "wordpress", "", 0, False)
        return result

    environment._exec = fake_exec  # type: ignore[method-assign]
    environment._baseline = BASELINE_SQL
    environment._provisioned_workers = 8
    environment._created_workers = 7
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
    environment, _ = _env(_docker())

    assert environment.database_name(0) is None


def test_workers_above_zero_get_their_own_database() -> None:
    environment, _ = _env(_docker())

    assert environment.database_name(1) == _worker_db(1)
    assert environment.database_name(3) == _worker_db(3)


def test_worker_databases_are_distinct_per_slot() -> None:
    """Two slots resolving to one name would silently share a runtime while
    the results file still claimed per-test isolation."""
    environment, _ = _env(_docker())

    names = [environment.database_name(worker) for worker in range(8)]

    assert len(set(names)) == len(names)


def test_worker_databases_are_scoped_to_the_run() -> None:
    """setup() reuses whatever container is already up, so two harness
    processes would otherwise both claim wp_bench_w1 and reset it under each
    other mid-test."""
    first, _ = _env(_docker())
    second = WordPressEnvironment(_docker())

    assert first.database_name(1) != second.database_name(1)


def test_negative_worker_slot_is_rejected() -> None:
    environment, _ = _env(_docker())

    with pytest.raises(ValueError, match="worker slot must be >= 0"):
        environment.database_name(-1)


# Reset -----------------------------------------------------------------


def test_worker_zero_reset_is_unchanged_by_pooling() -> None:
    """The serial command string is the compatibility contract: a run that
    does not opt into concurrency must be indistinguishable from before."""
    environment, calls = _env(_docker())

    environment.reset(0)

    assert _script(calls[0]) == "wp db reset --yes && wp db import - && wp core is-installed"


def test_default_reset_targets_worker_zero() -> None:
    with_default, default_calls = _env(_docker())
    explicit, explicit_calls = _env(_docker())

    with_default.reset()
    explicit.reset(0)

    assert default_calls == explicit_calls


def test_pooled_reset_targets_that_workers_database() -> None:
    environment, calls = _env(_docker())

    environment.reset(3)

    assert _database_in(_script(calls[0])) == _worker_db(3)


def test_pooled_reset_applies_the_database_to_both_steps() -> None:
    """``export`` rather than a one-command assignment: the drop and the
    import must land in the same database, or the reset restores the
    baseline over the runtime's default one."""
    script = _script(_pooled_reset_call(2))

    assert script.startswith(f"export WORDPRESS_DB_NAME={_worker_db(2)};")
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


def test_pooled_reset_restores_the_shared_baseline_over_stdin() -> None:
    """Every worker starts from the same dump, or the pool would grade
    identical tests against different WordPress states. The dump stays on
    the host, so it travels on stdin rather than as a path."""
    environment, calls = _env(_docker())

    environment.reset(2)

    assert "wp db import -" in _script(calls[0])
    assert environment.stdins[0] == BASELINE_SQL  # type: ignore[attr-defined]


def test_pooled_reset_verifies_the_restore_landed() -> None:
    assert "wp core is-installed" in _script(_pooled_reset_call(2))


def _pooled_reset_call(worker: int) -> list[str]:
    environment, calls = _env(_docker())
    environment.reset(worker)
    return calls[0]


def test_reset_refuses_a_slot_setup_never_provisioned() -> None:
    """``wp db reset`` creates the database it targets, so an unprovisioned
    slot would quietly self-provision and skip every setup-time check."""
    environment, _ = _env(_docker())
    environment._provisioned_workers = 2

    with pytest.raises(RuntimeError, match="never provisioned"):
        environment.reset(5)


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
    environment._baseline = BASELINE_SQL

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

    assert _database_in(_script(calls[0])) == _worker_db(3)
    assert "wp eval-file" in _script(calls[0])


def test_pooled_verifier_keeps_the_payload_on_stdin() -> None:
    """Payloads must never travel as argv (argv size limits, and they are
    visible in process listings). The database override must not become an
    excuse to inline one."""
    environment = WordPressEnvironment(_docker(), run_id=RUN_ID)
    environment._provisioned_workers = 8
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
    honors_override: bool = True,
    current_database: str = "wordpress",
) -> list[list[str]]:
    """Run setup(), letting the baseline capture succeed.

    ``provision_result`` is returned for every non-probe call after the
    capture, so a provisioning failure cannot be mistaken for a capture
    failure. ``honors_override`` models a runtime whose wp-config.php ignores
    WORDPRESS_DB_NAME — the case the verification probe exists to catch.
    """
    environment = WordPressEnvironment(config or _docker(), run_id=RUN_ID)
    calls: list[list[str]] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append(command)
        script = command[-1]
        if "SELECT DATABASE()" in script:
            resolved = _database_in(script) if honors_override else "wordpress"
            return (resolved or "wordpress", "", 0, False)
        if "wp config get DB_NAME" in script:
            return (current_database, "", 0, False)
        if "wp config set" in script:
            return ("ok", "", 0, False)
        if len(calls) == 1:
            return ("-- baseline SQL\n", "", 0, False)
        return provision_result

    environment._exec = fake_exec  # type: ignore[method-assign]
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    monkeypatch.setattr(environment, "_run_wp_env", lambda command: None)
    environment.setup(worker_count=worker_count)
    return calls


def _provision_scripts(calls: list[list[str]]) -> list[str]:
    """Restore scripts, excluding the capture, config rewrite and probes."""
    return [
        _script(call)
        for call in calls[1:]
        if "wp db import" in _script(call) and "SELECT DATABASE()" not in _script(call)
    ]


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
    scripts = _provision_scripts(_setup_calls(monkeypatch, 4))

    assert [_database_in(script) for script in scripts] == [
        _worker_db(1),
        _worker_db(2),
        _worker_db(3),
    ]


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
    for script in _provision_scripts(_setup_calls(monkeypatch, 4)):
        assert "wp db import -" in script


def test_provisioning_builds_the_database_with_the_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wp db reset`` is DROP IF EXISTS plus CREATE, so it builds the
    database as well as clearing it. A separate ``wp db create`` would be
    dead weight."""
    script = _provision_scripts(_setup_calls(monkeypatch, 2))[0]

    assert "wp db create" not in script
    assert script.index("wp db reset") < script.index("wp db import")


def test_provisioning_tolerates_a_database_left_by_an_earlier_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted run leaves its worker databases behind. The restore is
    what makes a leftover indistinguishable from a fresh database, and it
    must not be conditional on the database being absent."""
    script = _provision_scripts(_setup_calls(monkeypatch, 2))[0]

    assert script.startswith(f"export WORDPRESS_DB_NAME={_worker_db(1)};")
    assert "wp db reset --yes" in script


def test_provisioning_verifies_each_worker_resolves_to_its_own_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override is only a shell variable; if the runtime's wp-config.php
    does not read it, every command silently addresses the default database
    and still exits 0."""
    calls = _setup_calls(monkeypatch, 4)

    probes = [_script(call) for call in calls if "SELECT DATABASE()" in _script(call)]
    assert [_database_in(probe) for probe in probes] == [
        _worker_db(1),
        _worker_db(2),
        _worker_db(3),
    ]


def test_setup_fails_when_the_runtime_ignores_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this whole check exists for: a runtime that bakes DB_NAME
    would grade every worker against one database while results claimed
    per-worker isolation."""
    with pytest.raises(RuntimeError, match="does not honor"):
        _setup_calls(monkeypatch, 4, honors_override=False)


def test_docker_setup_makes_the_runtime_resolve_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grader image bakes a literal DB_NAME and docker exec never re-runs
    its entrypoint, so the constant is rewritten in the running container."""
    calls = _setup_calls(monkeypatch, 4)

    rewrites = [call for call in calls if "wp config set DB_NAME" in _script(call)]
    assert len(rewrites) == 1
    assert "getenv" in _script(rewrites[0])
    assert "WORDPRESS_DB_NAME" in _script(rewrites[0])
    assert "--raw" in _script(rewrites[0])


def test_wp_env_setup_leaves_its_config_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """wp-env's image already resolves DB_NAME from the environment; nothing
    needs rewriting, so nothing is."""
    calls = _setup_calls(
        monkeypatch, 4, config=GraderConfig(kind="docker", wp_env_dir=tmp_path)
    )

    assert not [call for call in calls if "wp config set" in _script(call)]


def test_serial_setup_rewrites_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _setup_calls(monkeypatch, 1)

    assert not [call for call in calls if "wp config set" in _script(call)]


def test_failed_provisioning_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker whose database never got the baseline would grade every test
    on its slot against an empty WordPress and blame the model."""
    with pytest.raises(RuntimeError, match="no space left on device"):
        _setup_calls(monkeypatch, 4, provision_result=("", "no space left on device", 1, False))


def test_failed_provisioning_names_the_worker_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match=_worker_db(1)):
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


# Teardown --------------------------------------------------------------


def test_worker_databases_are_dropped_when_the_run_ends() -> None:
    """Names are per-run, so leaving them would accumulate a fresh set every
    invocation instead of reusing one."""
    environment, calls = _env(_docker())
    environment._created_workers = 3

    environment.drop_worker_databases()

    # One round trip, not one per worker: this runs from a finally after
    # grading, so N sequential trips would add minutes to a finished run.
    assert len(calls) == 1
    assert _dropped_names(_script(calls[0])) == [
        _worker_db(1),
        _worker_db(2),
        _worker_db(3),
    ]
    # Named explicitly, never resolved through DB_NAME: a candidate that
    # rewrote wp-config.php could otherwise redirect the drop at the
    # runtime's own database.
    assert not any("WORDPRESS_DB_NAME" in _script(call) for call in calls)


def test_teardown_leaves_the_runtimes_own_database_alone() -> None:
    """Worker 0 is the runtime's database, not the pool's to drop."""
    environment, calls = _env(_docker())
    environment._created_workers = 1

    environment.drop_worker_databases()

    assert len(calls) == 1
    assert _dropped_names(_script(calls[0])) == [_worker_db(1)]


def test_serial_teardown_drops_nothing() -> None:
    environment, calls = _env(_docker())
    environment._created_workers = 0

    environment.drop_worker_databases()

    assert calls == []


def test_teardown_never_raises() -> None:
    """It runs after grading, so a cleanup failure must not invalidate
    results that are already correct."""
    environment, _ = _env(_docker(), ("", "connection refused", 1, False))
    environment._created_workers = 2

    environment.drop_worker_databases()


def test_failed_teardown_names_what_it_left_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not raising is not the same as saying nothing. Names are per-run, so
    nothing later reuses an undropped database -- swallowing the failure
    silently is what turns one bad cleanup into unbounded growth."""
    environment, _ = _env(_docker(), ("", "connection refused", 1, False))
    environment._created_workers = 2
    warned: list[list[str]] = []
    monkeypatch.setattr(
        "wp_bench.environment.print_orphaned_databases", lambda names: warned.append(names)
    )

    environment.drop_worker_databases()

    assert warned == [[_worker_db(1), _worker_db(2)]]


def test_successful_teardown_warns_about_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _ = _env(_docker())
    environment._created_workers = 2
    warned: list[list[str]] = []
    monkeypatch.setattr(
        "wp_bench.environment.print_orphaned_databases", lambda names: warned.append(names)
    )

    environment.drop_worker_databases()

    assert warned == []


# Fixes from the second review ------------------------------------------


def test_partial_provisioning_records_what_it_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure partway through must leave a truthful count, or teardown
    drops nothing and every database built so far leaks."""
    environment = WordPressEnvironment(_docker(), run_id=RUN_ID)
    calls: list[list[str]] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append(command)
        script = command[-1]
        if "SELECT DATABASE()" in script:
            return (_database_in(script), "", 0, False)
        if "wp config get DB_NAME" in script:
            return ("wordpress", "", 0, False)
        if len(calls) == 1:
            return ("-- baseline SQL\n", "", 0, False)
        # Worker 3's restore fails; workers 1 and 2 already succeeded.
        if _database_in(script) == _worker_db(3):
            return ("", "disk full", 1, False)
        return ("ok", "", 0, False)

    environment._exec = fake_exec  # type: ignore[method-assign]
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    with pytest.raises(RuntimeError, match="disk full"):
        environment.setup(worker_count=4)

    # Worker 3's restore failed, but `wp db reset` had already created its
    # database, so teardown must still know about it. Counting only verified
    # workers would leak precisely the one that failed.
    assert environment._provisioned_workers == 3
    assert environment._created_workers == 3


def test_partial_provisioning_still_drops_what_it_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, calls = _env(_docker())
    environment._created_workers = 2

    environment.drop_worker_databases()

    assert _dropped_names(_script(calls[0])) == [_worker_db(1), _worker_db(2)]


def test_verification_rejects_output_with_anything_after_the_name() -> None:
    """A runtime that ignored the override but echoed the variable from a
    stray mu-plugin would otherwise pass by matching the last line."""
    environment, _ = _env(_docker())

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        return (f"wordpress\n{_worker_db(1)}\n", "", 0, False)

    environment._exec = fake_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="does not honor"):
        environment._verify_worker_database(1)


def test_verification_tolerates_a_php_notice_before_the_name() -> None:
    """A runtime with display_errors on, or any PHP 8 deprecation, puts a
    line on stdout that says nothing about which database answered. Failing
    setup on that would diagnose a healthy runtime as broken."""
    environment, _ = _env(_docker())

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        return (f"Deprecated: strlen(): Passing null is deprecated\n{_worker_db(1)}\n", "", 0, False)

    environment._exec = fake_exec  # type: ignore[method-assign]

    environment._verify_worker_database(1)


def test_verification_rejects_a_non_diagnostic_line_before_the_name() -> None:
    """Anything that is not a PHP diagnostic is data, and data the probe did
    not ask for means the output cannot be trusted to identify the
    database."""
    environment, _ = _env(_docker())

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        return (f"wordpress\n{_worker_db(1)}\n", "", 0, False)

    environment._exec = fake_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="does not honor"):
        environment._verify_worker_database(1)

def test_verifier_refuses_a_slot_setup_never_provisioned() -> None:
    """Grading against a database that does not exist scores the resulting
    connection error as a model failure."""
    environment, _ = _env(_docker())
    environment._provisioned_workers = 2

    with pytest.raises(RuntimeError, match="never provisioned"):
        environment.execute_code("code", {}, worker=5)


def test_teardown_survives_a_missing_docker_binary() -> None:
    """It runs from a finally, so an exception here would replace whatever
    actually ended the run."""
    environment, _ = _env(_docker())
    environment._created_workers = 2

    def exploding_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        raise FileNotFoundError("No such file or directory: 'docker'")

    environment._exec = exploding_exec  # type: ignore[method-assign]

    environment.drop_worker_databases()


def test_teardown_is_idempotent() -> None:
    """A second call must not re-issue drops for names this run no longer
    owns."""
    environment, calls = _env(_docker())
    environment._created_workers = 3

    environment.drop_worker_databases()
    environment.drop_worker_databases()

    assert len(calls) == 1


def test_reset_refuses_after_teardown() -> None:
    """``wp db reset`` is DROP IF EXISTS plus CREATE, so a reset after
    teardown would silently re-create the database that was just dropped --
    and the flag means it could never be dropped again. Refusing is the safe
    direction."""
    environment, _ = _env(_docker())
    environment._created_workers = 3

    environment.drop_worker_databases()

    with pytest.raises(RuntimeError, match="never provisioned"):
        environment.reset(3)

def test_teardown_uses_a_short_timeout() -> None:
    """The per-test bound times a pool of 16 would add minutes to a run that
    is already finished."""
    environment, _ = _env(_docker())
    environment._created_workers = 2
    timeouts: list[Any] = []

    def recording_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        timeouts.append(kwargs.get("timeout"))
        return ("ok", "", 0, False)

    environment._exec = recording_exec  # type: ignore[method-assign]

    environment.drop_worker_databases()

    assert timeouts == [WORKER_TEARDOWN_TIMEOUT_SECONDS]


def test_rewrite_preserves_the_runtimes_configured_database_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This edits the operator's container permanently. Guessing 'wordpress'
    would repoint every non-pooled context at a database that may not exist
    for anyone running with a custom name."""
    calls = _setup_calls(monkeypatch, 2, current_database="customdb")

    rewrites = [_script(call) for call in calls if "wp config set DB_NAME" in _script(call)]
    assert len(rewrites) == 1
    assert "customdb" in rewrites[0]
    assert "wordpress" not in rewrites[0]


def test_rewrite_escapes_the_fallback() -> None:
    """The fallback is written into wp-config.php verbatim by --raw."""
    quoted = WordPressEnvironment._php_quote("my'db")

    assert quoted == "'my\\'db'"


def test_provisioning_rearms_teardown() -> None:
    """One environment can serve a second run. The idempotence flag must mean
    "dropped since the last provision", or the second run leaks everything it
    built because the first run already spent the flag."""
    environment, calls = _env(_docker())
    environment._created_workers = 3

    environment.drop_worker_databases()
    assert environment._dropped is True

    environment._provision_worker_databases(3)
    assert environment._dropped is False

    calls.clear()
    environment.drop_worker_databases()

    assert _dropped_names(_script(calls[0])) == [_worker_db(1), _worker_db(2)]
