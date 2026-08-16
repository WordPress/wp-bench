"""Bridge between Python harness and WordPress runtime."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

from .config import GraderConfig
from .output import print_orphaned_databases

#: Short timeout for cheap local Docker queries (e.g. ``docker ps``).
CONTAINER_QUERY_TIMEOUT_SECONDS = 30

#: Name prefix for the databases a pooled run provisions, one per worker
#: slot above 0. Worker 0 keeps the runtime's own default database, so a
#: serial run creates none of these and issues the same commands it always
#: did. Each run's id is appended, so concurrent harness processes on one
#: runtime never claim the same database.
WORKER_DATABASE_PREFIX = "wp_bench_"

#: Teardown runs after grading, from a finally, so it must not add
#: minutes to a run that is otherwise finished. The per-test bound
#: multiplied by a pool of 16 would.
WORKER_TEARDOWN_TIMEOUT_SECONDS = 15



#: Prefixes PHP uses for diagnostics it writes to stdout. A runtime with
#: display_errors on emits these alongside command output, and they say
#: nothing about which database answered a query.
PHP_DIAGNOSTIC_PREFIXES = (
    "warning:",
    "notice:",
    "deprecated:",
    "strict standards:",
    "fatal error:",
    "parse error:",
    "php warning:",
    "php notice:",
    "php deprecated:",
)


#: MySQL identifiers are alphanumerics, underscore, dollar; 64 chars max.
#: Used to sanity-check a name read back from the runtime before it is
#: written into wp-config.php.
_VALID_DATABASE_NAME = re.compile(r"[A-Za-z0-9_$]{1,64}")


def _is_php_diagnostic(line: str) -> bool:
    """Whether a line of runtime output is a PHP diagnostic rather than data."""
    return line.strip().lower().startswith(PHP_DIAGNOSTIC_PREFIXES)


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

    def __init__(self, config: GraderConfig, run_id: str | None = None):
        self.config = config
        #: Clean-baseline SQL dump, held on the host between setup and reset.
        #: Deliberately not written into the runtime: candidate code is
        #: eval'd there with root, so an on-disk dump would be a file the
        #: graded code could truncate or seed to defeat isolation.
        self._baseline: str | None = None
        #: Namespaces this run's worker databases. setup() reuses whatever
        #: container is already up, so two harness processes against one
        #: runtime would otherwise both claim ``wp_bench_w1`` and reset it
        #: under each other mid-test -- producing exactly the "restore did
        #: not land" failure that looks like a harness bug and is not one.
        self._run_id = run_id or uuid.uuid4().hex[:8]
        #: How many workers were provisioned *and verified*, so reset() and
        #: the verifier cannot be handed a slot whose database was never
        #: proven to be its own.
        self._provisioned_workers = 1
        #: Highest worker slot whose database may exist on the server, which
        #: is not the same thing: ``wp db reset`` creates the database before
        #: anything about it has been verified, so a worker that fails its
        #: restore or its check still left one behind. Teardown must drop by
        #: this, or the failure that most needs cleaning up leaks.
        self._created_workers = 0
        #: Whether teardown has already run since the last provision. Cleared
        #: when provisioning re-arms it, so an environment reused for a second
        #: run cannot inherit a spent flag and skip dropping what it just
        #: built.
        self._dropped = False

    def setup(self, *, capture_baseline: bool = True, worker_count: int = 1) -> None:
        """Bring the runtime up, and record the baseline reset() restores.

        Args:
            capture_baseline: Whether to capture the clean baseline. Capturing
                runs ``wp db reset``, which destroys whatever is in the
                database, so a run that never calls :meth:`reset` (isolation
                ``none``) must pass False: it would pay for an install it
                cannot use and wipe state the caller deliberately kept.
                Defaults to True so any caller that does reset is safe by
                omission.
            worker_count: How many isolated databases the run needs. The
                default (1) is the serial case: nothing beyond the runtime's
                own database is provisioned. Anything higher gives each
                concurrent worker its own copy of the baseline. Only
                meaningful when a baseline is captured, since provisioning
                replays it.

        """
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
        if not capture_baseline and worker_count > 1:
            # Provisioning replays the baseline, so without one there is
            # nothing to seed a worker database from. Silently running
            # unpooled would surface as a per-test failure much later.
            raise RuntimeError(
                f"setup() asked for {worker_count} worker databases without "
                "capturing a baseline, but provisioning replays that baseline. "
                "This is a harness bug: the caller's pooling and isolation "
                "settings disagree."
            )
        if capture_baseline:
            self._capture_baseline()
            self._provision_worker_databases(worker_count)


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

        The dump comes back on stdout and stays on the host. Writing it into
        the runtime would put the harness's isolation source inside the blast
        radius of the code it grades: candidates are eval'd in that container
        with root, so any test could truncate the dump (silently emptying
        every later reset) or append rows to it (silently pre-seeding every
        later test). The reset feeds it back over stdin instead.

        The install and drop are silenced because their WP-CLI success lines
        would otherwise land on stdout ahead of the SQL and corrupt the dump.
        """
        script = " && ".join(
            [
                "wp db reset --yes >/dev/null",
                f"{shlex.join(self._install_command())} >/dev/null",
                "wp db export -",
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
        if not stdout.strip():
            # An empty dump imports cleanly and exits 0, so catching it here is
            # the difference between failing setup and grading every test in
            # the run against an empty database.
            raise RuntimeError(
                "Captured an empty WordPress baseline: 'wp db export -' returned "
                "no SQL, so no reset could restore a usable database."
            )
        self._baseline = stdout

    def database_name(self, worker: int) -> str | None:
        """The database that worker slot ``worker`` grades against.

        ``None`` means the runtime's own default database — whatever
        ``wp-config.php`` resolves ``DB_NAME`` to. Worker 0 always gets it,
        which is what keeps a serial run byte-identical to a pre-pooling one:
        no database is created and no command grows an override.

        Names carry this run's id so two harness processes sharing a runtime
        cannot collide. A fixed ``wp_bench_w1`` would have each process
        dropping and re-importing the other's database mid-test.
        """
        if worker < 0:
            raise ValueError(f"worker slot must be >= 0, got {worker}")
        if worker == 0:
            return None
        return f"{WORKER_DATABASE_PREFIX}{self._run_id}_w{worker}"

    def _database_env(self, worker: int) -> str:
        """Shell prefix pinning WP-CLI to this worker's database.

        ``wp-config.php`` resolves ``DB_NAME`` from the ``WORDPRESS_DB_NAME``
        environment variable at runtime in both the wp-env image and the
        Docker grader image, so an inline assignment is enough to retarget a
        whole command. It has to travel inside the script rather than as a
        process environment because ``npx wp-env run cli`` has no ``-e``
        flag; the inline form is the one that works on both grader paths.

        Empty for worker 0 by design — its commands must stay exactly what a
        serial run sends.
        """
        name = self.database_name(worker)
        if name is None:
            return ""
        return f"export WORDPRESS_DB_NAME={shlex.quote(name)}; "

    def _restore_script(self) -> str:
        """Drop everything, then replay the baseline from stdin.

        ``&&`` so a failed drop can never import the baseline over surviving
        state. ``wp db import`` exits 0 on an empty or truncated dump, which
        would drop every table, import nothing, and report success -- every
        later test would then fail against an empty database and be blamed on
        the model, so ``wp core is-installed`` confirms the restore landed.
        """
        return "wp db reset --yes && wp db import - && wp core is-installed"

    def _provision_worker_databases(self, worker_count: int) -> None:
        """Give every worker slot above 0 its own copy of the baseline.

        Pooling is what lets ``reset_per_test`` run concurrently. Serial
        isolation time-slices a single mutable runtime; pooled isolation
        means no two concurrent tests touch the same runtime at all, so the
        guarantee gets stronger under concurrency rather than weaker.

        ``wp db reset`` is ``DROP DATABASE IF EXISTS`` plus ``CREATE
        DATABASE``, so it builds the database as well as clearing it; no
        separate create is needed, and a database an interrupted run left
        behind is restored to the baseline rather than tripping anything.

        Every worker is then verified with ``SELECT DATABASE()``. That check
        is the point: the override is a shell variable that only takes effect
        if the runtime's wp-config.php resolves ``DB_NAME`` from the
        environment. Where it does not, every command silently addresses the
        default database, provisioning "succeeds" having built nothing, and
        the run grades W concurrent tests against one database while stamping
        per-worker isolation. Asserting the database by name is what turns
        that from silent corruption into a failed setup.

        Worker databases are dropped by :meth:`drop_worker_databases` when
        the run ends. They are namespaced per run, so leaving them would
        accumulate a fresh set on every invocation rather than reusing one.

        Filesystem state outside the candidate plugin directory stays shared
        across pooled workers: ``wp-content/uploads`` is one directory and
        ``debug.log`` is one file, however many databases the pool has. The
        isolation boundary is the database, not the container. Candidate
        plugins are already safe from collision — class-artifact-installer.php
        installs each one under a random directory suffix — though a candidate
        that enumerates the plugins directory can still see a concurrent one.

        The pool separates accidental interference, not deliberate access.
        Candidates are eval'd with a full WordPress bootstrap and root MySQL
        credentials, and nothing restricts which database ``$wpdb`` talks to,
        so code that reaches for another worker's database reaches it. Serial
        execution made that harmless because nothing else was running;
        concurrency does not. Treat the guarantee as "no test observes another
        test's leftovers", which is what ``reset_per_test`` has always meant,
        rather than as a sandbox between concurrent tests.
        """
        if worker_count <= 1:
            return
        # Provisioning re-arms teardown and restarts both counters, so a
        # second run on this environment neither inherits a spent flag nor
        # trusts the previous run's slot count.
        self._dropped = False
        self._created_workers = 0
        self._provisioned_workers = 1
        self._make_runtime_resolve_database()
        for worker in range(1, worker_count):
            name = self.database_name(worker)
            # Recorded before the command that creates it, not after. Every
            # failure branch below fires with the database already on the
            # server, because the restore starts with `wp db reset`, which
            # is DROP IF EXISTS plus CREATE. Counting it only on success is
            # what would leak precisely the worker that failed.
            self._created_workers = worker
            _, stderr, returncode, timed_out = self._exec(
                ["sh", "-c", self._database_env(worker) + self._restore_script()],
                stdin=self._baseline,
                timeout=self.config.setup_timeout_seconds,
            )
            if timed_out:
                raise EnvironmentSetupTimeout(
                    f"Timed out provisioning worker database {name!r} after "
                    f"{self.config.setup_timeout_seconds}s (grader.setup_timeout_seconds)."
                )
            if returncode != 0:
                raise RuntimeError(
                    f"Failed to provision worker database {name!r} (exit code "
                    f"{returncode}){': ' + stderr.strip() if stderr.strip() else ''}"
                )
            self._verify_worker_database(worker)
            # Recorded per worker, not once at the end: a failure partway
            # through must still leave behind a truthful count, or teardown
            # drops nothing and every database built so far leaks.
            self._provisioned_workers = worker + 1

    @staticmethod
    def _php_quote(value: str) -> str:
        """Render a string as a PHP single-quoted literal.

        The value is written into wp-config.php verbatim by ``--raw``, so an
        unescaped quote would either break the file or close the literal and
        continue as code.
        """
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"

    def _current_database(self) -> str:
        """The database this runtime resolves to with no override applied.

        Used as the fallback when rewriting DB_NAME, so the rewrite preserves
        whatever the container was configured with instead of assuming the
        WordPress default.
        """
        stdout, stderr, returncode, timed_out = self._exec(
            ["sh", "-c", "wp config get DB_NAME"],
            timeout=self.config.setup_timeout_seconds,
        )
        if timed_out:
            raise EnvironmentSetupTimeout(
                "Timed out reading the runtime's current DB_NAME after "
                f"{self.config.setup_timeout_seconds}s (grader.setup_timeout_seconds)."
            )
        lines = [
            line.strip()
            for line in stdout.splitlines()
            if line.strip() and not _is_php_diagnostic(line)
        ]
        # The value is written into wp-config.php permanently, so a garbled
        # read would repoint the runtime at a database that does not exist.
        # A database name is a plain identifier; anything else means the read
        # returned something other than the name.
        name = lines[0] if len(lines) == 1 else ""
        if returncode != 0 or not _VALID_DATABASE_NAME.fullmatch(name):
            raise RuntimeError(
                "Could not read the runtime's current DB_NAME, so rewriting it "
                "would have to guess a fallback and could repoint the runtime "
                f"at a database that does not exist. Got: {stdout.strip()!r}"
                f"{'. ' + stderr.strip() if stderr.strip() else ''}"
            )
        return name

    def _make_runtime_resolve_database(self) -> None:
        """Make wp-config.php read ``DB_NAME`` from the environment.

        Both images the harness ships now do this on their own: wp-env's
        always has, and runtime/docker-entrypoint.sh rewrites the constant
        after ``wp config create`` bakes a literal. This stays because a
        container is not always one the harness built -- an image pulled
        before that entrypoint fix, a wp-config.php left in a volume by an
        older image, or a runtime an operator manages themselves. Rewriting
        the constant in the running container covers all three without a
        rebuild.

        The fallback is read from the runtime rather than hardcoded. This
        edits the operator's container permanently, so writing a guessed
        ``'wordpress'`` would silently repoint every non-pooled context at a
        database that may not exist for anyone running with a custom name.
        Taking the value the runtime already resolves to makes the rewrite a
        no-op for anything that does not set the variable — which is worker
        0 and every serial run — and idempotent if it has run before.

        Only the paths that need it are rewritten: wp-env's config already
        resolves from the environment and is left alone.
        :meth:`_verify_worker_database` is what proves this worked; this
        method only tries.
        """
        if self.config.wp_env_dir:
            return
        expression = f"getenv('WORDPRESS_DB_NAME') ?: {self._php_quote(self._current_database())}"
        script = f"wp config set DB_NAME {shlex.quote(expression)} --raw"
        _, stderr, returncode, timed_out = self._exec(
            ["sh", "-c", script],
            timeout=self.config.setup_timeout_seconds,
        )
        if timed_out:
            raise EnvironmentSetupTimeout(
                "Timed out making the runtime resolve DB_NAME from the environment "
                f"after {self.config.setup_timeout_seconds}s (grader.setup_timeout_seconds)."
            )
        if returncode != 0:
            raise RuntimeError(
                "Failed to make the runtime resolve DB_NAME from the environment "
                f"(exit code {returncode}), so per-worker databases cannot take "
                f"effect{': ' + stderr.strip() if stderr.strip() else ''}"
            )

    def _verify_worker_database(self, worker: int) -> None:
        """Confirm this worker's commands actually land in its own database.

        Everything about pooling rests on the override being honored. Where
        it is not, each step still exits 0 against the default database, so
        without this check the run is silently unisolated while claiming
        otherwise -- the failure mode issue #39 exists to prevent.
        """
        expected = self.database_name(worker)
        stdout, stderr, returncode, timed_out = self._exec(
            [
                "sh",
                "-c",
                self._database_env(worker)
                + "wp db query 'SELECT DATABASE()' --skip-column-names",
            ],
            timeout=self.config.setup_timeout_seconds,
        )
        if timed_out:
            raise EnvironmentSetupTimeout(
                f"Timed out verifying worker database {expected!r} after "
                f"{self.config.setup_timeout_seconds}s (grader.setup_timeout_seconds)."
            )
        # PHP diagnostics are dropped first: a runtime with display_errors on,
        # or any deprecation notice from a newer PHP, puts a line on stdout
        # that says nothing about which database answered.
        #
        # What is left must then be the name and nothing else. Taking the last
        # line instead would accept a runtime that ignored the override
        # entirely but echoed WORDPRESS_DB_NAME from a leftover mu-plugin --
        # and the run id is handed to candidate code in that environment, so
        # this is forgeable rather than hypothetical.
        lines = [
            line.strip()
            for line in stdout.splitlines()
            if line.strip() and not _is_php_diagnostic(line)
        ]
        if returncode != 0 or lines != [expected]:
            actual = " / ".join(lines) if lines else "<no output>"
            raise RuntimeError(
                f"Worker {worker} resolved to database {actual!r}, "
                f"expected {expected!r}: the runtime does not honor "
                "WORDPRESS_DB_NAME, so concurrent tests would share one database "
                "while results claimed per-worker isolation. Run with "
                "run.execution_concurrency: 1"
                f"{'. ' + stderr.strip() if stderr.strip() else ''}"
            )

    def _require_provisioned(self, worker: int) -> None:
        """Refuse a slot no database was built and verified for.

        Both paths that touch a worker's database need this, for different
        reasons: ``wp db reset`` would create the database on the spot and
        skip every setup-time check, while the verifier would grade against
        a database that does not exist and score the resulting connection
        error as a model failure.
        """
        if worker >= self._provisioned_workers:
            raise RuntimeError(
                f"Worker slot {worker} was never provisioned (setup built "
                f"{self._provisioned_workers}), so its database was never "
                "verified. This is a harness bug: the pool and setup disagree."
            )

    def drop_worker_databases(self) -> None:
        """Remove this run's worker databases. Best effort.

        Names are per-run, so skipping this would leave a fresh set behind on
        every invocation instead of reusing one. It drops by
        ``_created_workers`` rather than by the verified count: the restore
        starts with ``wp db reset``, which creates the database before
        anything about it has been checked, so a worker that failed still has
        one to clean up -- and that is the case cleanup exists for.

        Each database is dropped **by name** rather than by pointing
        ``DB_NAME`` at it. A drop that resolved through the environment would
        target whatever wp-config.php says at that moment -- and that file
        lives in the container the graded code runs in, with root -- so a
        candidate that rewrote it could turn this into a drop of the
        runtime's own database. Naming the target rules that out by
        construction rather than trusting a check made earlier. The
        connection still goes through the runtime's own database, so if a
        candidate destroyed *that*, this reports orphans instead of dropping
        them, which is the safe direction.

        One round trip drops every database. It runs from a ``finally`` after
        grading, so N sequential trips into the runtime would add minutes to
        a run that is already finished.

        No ``Exception`` propagates: this is called from a ``finally``, and
        raising there would replace whatever actually ended the run. A
        ``BaseException`` -- a second Ctrl-C landing during cleanup -- is
        deliberately allowed through, because that is the operator asking to
        stop; orphans are still reported first.
        """
        if self._dropped:
            return
        self._dropped = True
        names = [
            name
            for worker in range(1, self._created_workers + 1)
            if (name := self.database_name(worker)) is not None
        ]
        # Cleared even if the drop fails, so a retry cannot re-drop names
        # this run no longer owns, and so a post-teardown reset() refuses
        # rather than silently re-creating a database that was just removed.
        self._created_workers = 0
        self._provisioned_workers = 1
        if not names:
            return
        statement = "; ".join(f"DROP DATABASE IF EXISTS `{name}`" for name in names)
        dropped = False
        try:
            _, _, returncode, timed_out = self._exec(
                ["sh", "-c", f"wp db query {shlex.quote(statement)}"],
                timeout=WORKER_TEARDOWN_TIMEOUT_SECONDS,
            )
            dropped = returncode == 0 and not timed_out
        except Exception:  # noqa: BLE001 - a cleanup failure, never the run's
            # A missing docker/npx binary, exhausted file descriptors, a
            # broken pipe. Narrowing this would let it escape the finally.
            dropped = False
        finally:
            if not dropped:
                # In a finally so the operator still learns what was left
                # behind when a BaseException is on its way through.
                self._report_orphans(names)

    @staticmethod
    def _report_orphans(names: list[str]) -> None:
        """Name what cleanup could not drop, without becoming a new failure.

        Console output can raise on its own (an encoding that cannot render
        the warning glyph), and this is reached from a ``finally``.
        """
        try:
            print_orphaned_databases(names)
        except Exception:  # noqa: BLE001,S110 - a warning must not become the error
            # Nowhere left to report to: the reporting channel is what failed.
            pass

    def reset(self, worker: int = 0) -> None:
        """Restore one worker's database to the captured clean baseline.

        ``wp db reset`` drops every table, then the baseline dump recorded by
        :meth:`setup` is imported to return to a deterministic just-installed
        state. Called before every execution test when
        ``run.execution_isolation`` is ``reset_per_test`` so no test can
        observe database state (options, posts, roles, transients, cron
        events, etc.) left behind by an earlier test or model run.

        Both steps travel in one invocation: a round trip into the runtime
        costs ~0.9s through ``npx wp-env run cli`` (~0.24s through ``docker
        exec``), so a second trip costs more than the restore itself. ``&&``
        chains them so a failed drop can never import the baseline over
        surviving state.

        Args:
            worker: Which pooled database to restore. 0 (the default) is the
                runtime's own database and produces the exact command a
                serial run has always sent.

        """
        if not self.config.wp_env_dir and self.config.kind != "docker":
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
        self._require_provisioned(worker)
        self._reset_step(
            ["sh", "-c", self._database_env(worker) + self._restore_script()],
            stdin=self._baseline,
        )

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

    def execute_code(
        self,
        code: str,
        verification_spec: dict[str, Any],
        worker: int = 0,
    ) -> ExecutionResult:
        """Run a candidate PHP snippet through the runtime verifier.

        Compatibility wrapper over execute_artifact() for snippet payloads.
        """
        payload = {
            "payload_version": "1.0",
            "code": code,
            **verification_spec,
        }
        return self._run_verifier(payload, worker)

    def execute_artifact(
        self,
        artifact: Any,
        verification_spec: dict[str, Any],
        worker: int = 0,
    ) -> ExecutionResult:
        """Run a candidate artifact (snippet or plugin files) through the verifier.

        Args:
            artifact: An Artifact with kind, code, and optional files map.
            verification_spec: static_checks/runtime_checks for the test.
            worker: Which pooled database to grade against. Must be the slot
                whose :meth:`reset` cleaned the state this candidate is meant
                to see — grading on another worker's database would score the
                candidate against a runtime some other test is mutating.
        """
        payload = {
            "payload_version": "1.0",
            **artifact.payload_fields(),
            **verification_spec,
        }
        return self._run_verifier(payload, worker)

    def _verifier_command(self, worker: int) -> list[str]:
        """The runtime command that grades a candidate on a worker's database.

        Worker 0 gets the bare WP-CLI call a serial run has always used.
        Pooled workers wrap it so the database override applies, and ``exec``
        replaces the shell rather than forking under it, keeping stdin
        attached to WP-CLI itself — the payload travels on stdin and must
        never become an argument (argv size limits, visible in process
        listings).
        """
        self._require_provisioned(worker)
        verifier_path = self._runtime_verifier_path()
        database_env = self._database_env(worker)
        if not database_env:
            return ["wp", "eval-file", verifier_path]
        return ["sh", "-c", f"{database_env}exec wp eval-file {shlex.quote(verifier_path)}"]

    def _run_verifier(self, payload: dict[str, Any], worker: int = 0) -> ExecutionResult:
        """Send a payload to the runtime verifier and parse the result.

        A runtime timeout is a per-test failure, not a harness crash: it
        returns a structured ``ExecutionResult`` with ``timed_out=True`` and
        a synthetic zero-score runtime payload, so the benchmark records the
        timeout and continues with the next test.
        """
        cmd = self._verifier_command(worker)
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
