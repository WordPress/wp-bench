"""Real Docker isolation checks; set WP_BENCH_TEST_IMAGE to enable locally/CI."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import WordPressEnvironment

IMAGE = os.environ.get("WP_BENCH_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="Set WP_BENCH_TEST_IMAGE for Docker integration tests")


@pytest.fixture
def environment():
    env = WordPressEnvironment(GraderConfig(image=IMAGE or "", setup_timeout_seconds=60))
    try:
        env.setup()
        yield env
    finally:
        env.close()
    remaining = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=org.wordpress.wp-bench.run={env._run_id}"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert not remaining.stdout.strip()


def verify(env: WordPressEnvironment, code: str, assertion: str) -> None:
    result = env.execute_code(code, {
        "runtime_checks": {"assertions": [{"type": "custom_assertion", "code": assertion, "weight": 1}]},
    })
    assert result.success, (result.raw, result.stdout, result.stderr)
    assert result.raw["runtime"]["score"] == 1


def test_files_database_and_core_changes_do_not_survive(environment) -> None:
    env = environment
    env.reset()
    verify(env, """
        update_option('isolation_probe', 'dirty');
        mkdir(ABSPATH . 'wp-content/uploads');
        file_put_contents(ABSPATH . 'wp-content/uploads/probe', 'dirty');
        file_put_contents(ABSPATH . 'wp-content/debug.log', 'dirty');
        mkdir(ABSPATH . 'wp-content/plugins/wp-bench-candidate-leftover');
        file_put_contents('/tmp/isolation_probe', 'dirty');
        file_put_contents(ABSPATH . 'index.php', 'dirty');
        file_put_contents(ABSPATH . 'wp-config.php', '<?php throw new Exception("dirty");');
    """, "return get_option('isolation_probe') === 'dirty';")
    env.reset()
    verify(env, "", """
        return false === get_option('isolation_probe')
            && !file_exists('/tmp/isolation_probe')
            && !file_exists(ABSPATH . 'wp-content/uploads/probe')
            && !file_exists(ABSPATH . 'wp-content/debug.log')
            && !file_exists(ABSPATH . 'wp-content/plugins/wp-bench-candidate-leftover')
            && str_contains(file_get_contents(ABSPATH . 'index.php'), 'wp-blog-header.php');
    """)


def test_admin_hooks_do_not_make_core_maintenance_requests(environment) -> None:
    env = environment
    env.reset()
    verify(env, """
        define('WP_ADMIN', true);
        require_once ABSPATH . 'wp-admin/includes/template.php';
        $GLOBALS['maintenance_requests'] = 0;
        add_filter('pre_http_request', static function ($pre, $args, $url) {
            if (str_contains($url, '.wordpress.org')) {
                ++$GLOBALS['maintenance_requests'];
            }
            return new WP_Error('offline', 'Network unavailable');
        }, 10, 3);
        do_action('admin_init');
        do_action('load-plugins.php');
        do_action('load-themes.php');
        do_action('load-update.php');
        do_action('load-update-core.php');
        do_action('wp_version_check');
        do_action('wp_update_plugins');
        do_action('wp_update_themes');
        do_action('wp_maybe_auto_update');
    """, "return $GLOBALS['maintenance_requests'] === 0;")


def test_concurrent_candidates_cannot_observe_neighbor_state(environment) -> None:
    env = environment
    barrier = threading.Barrier(2)

    def candidate(name: str) -> None:
        env.reset()
        # Keep both runtimes alive with a candidate plugin, option, and same
        # temp-file path. Each subsequent verifier can observe only its own.
        code = (
            f"update_option('isolation_owner', '{name}'); "
            f"mkdir(ABSPATH . 'wp-content/plugins/wp-bench-candidate-{name}'); "
            f"file_put_contents('/tmp/isolation_owner', '{name}');"
        )
        _, stderr, rc, timed_out = env._exec(["wp", "eval", code])
        assert rc == 0 and not timed_out, stderr
        barrier.wait(timeout=15)
        peer = "second" if name == "first" else "first"
        verify(env, "", f"""
            return '{name}' === get_option('isolation_owner')
                && '{name}' === file_get_contents('/tmp/isolation_owner')
                && is_dir(ABSPATH . 'wp-content/plugins/wp-bench-candidate-{name}')
                && !file_exists(ABSPATH . 'wp-content/plugins/wp-bench-candidate-{peer}');
        """)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(candidate, ["first", "second"]))


def test_runtime_has_private_network_and_enforced_limits(environment) -> None:
    env = environment
    env.reset()
    inspect = subprocess.run(
        ["docker", "inspect", env._runtime_container()], capture_output=True,
        text=True, timeout=30, check=True,
    )
    config = json.loads(inspect.stdout)[0]
    host = config["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"]
    assert host["Memory"] == 512 * 1024 * 1024
    assert host["MemorySwap"] == host["Memory"]
    assert host["NanoCpus"] == 1_000_000_000
    assert host["PidsLimit"] == 64
    assert not config["Mounts"]  # no persistent, shared, or host mounts
    verify(env, "", """
        return trim(shell_exec('id -u')) !== '0'
            && !file_exists('/var/run/docker.sock')
            && trim((string) shell_exec('ip -4 route')) === ''
            && str_contains(file_get_contents('/proc/self/status'), 'CapEff:' . "\t" . '0000000000000000')
            && str_contains(file_get_contents('/proc/self/status'), 'NoNewPrivs:' . "\t" . '1')
            && !is_writable('/opt/wp-bench-wordpress/wp-load.php');
    """)


@pytest.mark.parametrize("code", ["while (true) {}", "exit(1);"])
def test_timeout_or_exit_removes_the_entire_process_namespace(environment, code: str) -> None:
    env = environment
    env.config.timeout_seconds = 1
    env.reset()
    name = env._runtime_container()
    result = env.execute_code(
        "shell_exec('sleep 60 >/dev/null 2>&1 &'); " + code,
        {"runtime_checks": {"assertions": []}},
    )
    assert not result.success
    assert result.timed_out == (code == "while (true) {}")
    inspect = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=30, check=False)
    assert inspect.returncode != 0
    env.reset()
    verify(env, "", "return !str_contains(shell_exec('ps'), 'sleep 60');")
