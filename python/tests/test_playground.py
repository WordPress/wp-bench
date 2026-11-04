"""Playground transport/lifecycle tests plus opt-in real WASM coverage."""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from wp_bench.config import GraderConfig, HarnessConfig
from wp_bench.environment import EnvironmentSetupTimeout
from wp_bench.playground import PlaygroundEnvironment


@pytest.fixture
def fake_environment(tmp_path):
    (tmp_path / 'verify.mjs').write_text('''
import json, sys, time
print('WP_BENCH_READY', flush=True)
payload = json.loads(sys.stdin.readline())
if payload.get('code') == 'hang':
    time.sleep(30)
print(json.dumps({'stdout': json.dumps({'success': True, 'runtime': {'score': 1}}),
                  'stderr': '', 'returncode': 0}))
''')
    env = PlaygroundEnvironment(GraderConfig(
        kind='playground', playground_directory=tmp_path, playground_node=sys.executable,
        timeout_seconds=1,
    ))
    try:
        env.setup()
        yield env
    finally:
        env.close()
    assert not env._processes
    assert not env._workdirs


def test_transport_concurrency_and_cleanup(fake_environment):
    def execute(_):
        fake_environment.reset()
        return fake_environment.execute_code('echo "hello";', {}).success
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(execute, range(4)))
    assert not fake_environment._processes


def test_execution_timeout_is_a_result_and_next_candidate_runs(fake_environment):
    env = fake_environment
    env.reset()
    result = env.execute_code('hang', {})
    assert result.timed_out
    assert result.raw['runtime']['score'] == 0
    assert not env._processes
    assert not env._workdirs
    env.reset()
    assert env.execute_code('', {}).success


def test_boot_timeout_aborts_and_cleans_up(fake_environment):
    env = fake_environment
    (env.config.playground_directory / 'verify.mjs').write_text('import time; time.sleep(30)')
    env.config.setup_timeout_seconds = 1
    with pytest.raises(EnvironmentSetupTimeout):
        env.reset()
    assert not env._processes
    assert not env._workdirs


def test_shell_artifacts_are_rejected_and_cleaned_up(fake_environment):
    env = fake_environment
    env.reset()
    with pytest.raises(ValueError, match='wp_cli_shell'):
        env._run_verifier({'artifact_kind': 'wp_cli_shell'})
    assert not env._processes
    assert not env._workdirs


def test_playground_requires_fresh_instances():
    with pytest.raises(ValidationError, match='reset_per_test'):
        HarnessConfig(grader={'kind': 'playground'}, run={'execution_isolation': 'none'})


def test_relative_bridge_directory(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('grader:\n  kind: playground\n  playground_directory: bridge\n')
    assert HarnessConfig.from_file(config).grader.playground_directory == tmp_path / 'bridge'


@pytest.mark.skipif(not os.environ.get('WP_BENCH_TEST_PLAYGROUND_NODE'),
                    reason='Set WP_BENCH_TEST_PLAYGROUND_NODE to enable real WASM tests')
def test_real_playground_isolation_plugins_and_timeout():
    env = PlaygroundEnvironment(GraderConfig(
        kind='playground', playground_node=os.environ['WP_BENCH_TEST_PLAYGROUND_NODE'],
        timeout_seconds=15,
    ))
    def verify(code, assertion):
        env.reset()
        result = env.execute_code(code, {'runtime_checks': {'assertions': [
            {'type': 'custom_assertion', 'code': assertion},
        ]}})
        assert result.success, (result.raw, result.stderr)
        assert result.raw['runtime']['score'] == 1
    try:
        env.setup()
        verify("update_option('probe', 'dirty'); file_put_contents('/tmp/probe', 'dirty');",
               "return get_option('probe') === 'dirty';")
        verify('', "return get_option('probe') === false && !file_exists('/tmp/probe');")
        from wp_bench.artifacts import Artifact
        env.reset()
        result = env.execute_artifact(Artifact(kind='wp_plugin_files', files={
            'probe.php': '<?php /* Plugin Name: Probe */ update_option("plugin_probe", "ok");',
        }), {'runtime_checks': {'assertions': [
            {'type': 'option_value', 'target': 'plugin_probe', 'expected': 'ok'},
        ]}})
        assert result.success, (result.raw, result.stderr)
        env.config.timeout_seconds = 1
        env.reset()
        assert env.execute_code('while (true) {}', {}).timed_out
        env.config.timeout_seconds = 15
        verify('', "return get_option('plugin_probe') === false;")
    finally:
        env.close()
    assert not env._processes
    assert not env._workdirs
