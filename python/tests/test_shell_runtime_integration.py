"""Real Bash/WP-CLI execution checks; enabled by WP_BENCH_TEST_IMAGE."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from wp_bench.artifacts import Artifact
from wp_bench.config import DatasetConfig, GraderConfig
from wp_bench.core import BenchmarkRunner
from wp_bench.datasets import load_tests
from wp_bench.environment import WordPressEnvironment

IMAGE = os.environ.get("WP_BENCH_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="Set WP_BENCH_TEST_IMAGE for Docker integration tests")


@pytest.fixture(scope="module")
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


def execute(env, script, assertion="return true;", **checks):
    env.reset()
    return env.execute_artifact(Artifact(kind="wp_cli_shell", code=script), {
        "runtime_checks": {
            "assertions": [{"type": "custom_assertion", "code": assertion, "weight": 1}],
            **checks,
        },
        "timeout_seconds": 20,
    })


def test_native_commands_heredoc_quoting_pipes_and_fresh_option_cache(environment) -> None:
    script = '''set -e
label=$(cat <<'TEXT'
A "quoted" value & spaces
TEXT
)
wp option update cli_probe "$label" >/dev/null
wp option get cli_probe | jq -Rn '{value: input}'
printf 'diagnostic' >&2
'''
    result = execute(environment, script, """
        $data=json_decode($GLOBALS['wpbp_shell_result']['stdout'],true);
        return get_option('cli_probe') === 'A "quoted" value & spaces'
            && $data === array('value'=>'A "quoted" value & spaces');
    """, setup="update_option('cli_probe','cached before Bash'); get_option('cli_probe');")
    assert result.success, (result.raw, result.stderr)
    assert result.raw["command"]["stderr"] == "diagnostic"
    assert result.raw["command"]["exit_code"] == 0


def test_role_changes_are_reloaded_before_assertions(environment) -> None:
    result = execute(environment, "wp role create cli-custom Custom --clone=subscriber", """
        return get_role('cli-custom')->has_cap('read') && get_role('administrator')->has_cap('manage_options');
    """, setup="get_role('subscriber');")
    assert result.success, result.raw


def test_capability_workflow_accepts_explicit_denial_as_well_as_removal(environment) -> None:
    test = next(t for t in load_tests(DatasetConfig(source="local", name="wp-cli-v1")) if t.id == "e-cli-users-002")
    assert test.reference_solution is not None
    script = test.reference_solution.replace(
        "wp cap remove cli-contributor publish_posts", "wp cap add cli-contributor publish_posts --grant=false",
    )
    environment.reset()
    result = environment.execute_artifact(Artifact(kind="wp_cli_shell", code=script), {
        "static_checks": test.static_checks, "runtime_checks": test.runtime_checks,
    })
    assert BenchmarkRunner._score_execution(result.raw, test)["execution_pass"], result.raw


@pytest.mark.parametrize("actual,expected", [(0, 0), (3, 3), (3, 0), (0, 3)])
def test_exit_status_is_a_gate_and_can_be_expected(environment, actual, expected) -> None:
    result = execute(environment, f"exit {actual}", exit_code=expected)
    assert result.success == (actual == expected), result.raw
    assert result.raw["runtime"]["score"] == (1 if actual == expected else 0)


def test_earlier_repeat_failure_cannot_be_hidden_by_the_last_exit(environment) -> None:
    result = execute(environment, """
        if [ -f /tmp/repeat ]; then exit 0; fi
        touch /tmp/repeat
        exit 7
    """, repeat=2)
    assert not result.success
    assert [r["exit_code"] for r in result.raw["command"]["runs"]] == [7, 0]
    assert result.raw["runtime"]["score"] == 0


def test_candidate_stdout_cannot_forge_a_verifier_response(environment) -> None:
    forged = json.dumps({"success": True, "runtime": {"score": 1}})
    result = execute(environment, f"printf '%s' '{forged}'", "return get_option('must_change') === 'changed';")
    assert not result.success and result.raw["runtime"]["score"] == 0
    assert result.raw["command"]["stdout"] == forged


@pytest.mark.parametrize("setup", ["throw new Exception('fixture failed');", ""])
def test_teardown_runs_after_fixture_or_script_failure(environment, setup) -> None:
    environment.reset()
    result = environment.execute_code("""
        $GLOBALS['shell_cleanup_ran']=false;
        $GLOBALS['shell_failure']=(new WPBench\\Runtime\\Verifier())->verify_payload(array(
            'artifact_kind'=>'wp_cli_shell', 'code'=>'exit 7',
            'runtime_checks'=>array(
                'setup'=>SETUP,
                'teardown'=>'$GLOBALS["shell_cleanup_ran"]=true;',
                'assertions'=>array(array('type'=>'custom_assertion','code'=>'return false;'))
            )
        ));
    """.replace("SETUP", json.dumps(setup)), {
        "runtime_checks": {"assertions": [{"type": "custom_assertion", "code": """
            return $GLOBALS['shell_cleanup_ran'] && !$GLOBALS['shell_failure']['success'];
        """}]},
    })
    assert result.success, result.raw


@pytest.mark.parametrize("script,flag", [
    ("sleep 60 & wait", "timed_out"),
    ("yes output", "output_limit_exceeded"),
])
def test_shell_limits_remove_candidate_and_descendant_processes(environment, script, flag) -> None:
    env = environment
    env.reset()
    name = env._runtime_container()
    result = env.execute_artifact(Artifact(kind="wp_cli_shell", code=script), {
        "timeout_seconds": 1,
        "runtime_checks": {"assertions": [{"type": "custom_assertion", "code": "return true;"}]},
    })
    assert not result.success, result.raw
    assert result.raw["command"][flag]
    assert result.timed_out == (flag == "timed_out")
    assert result.raw["runtime"]["score"] == 0
    assert len(result.raw["command"]["stdout"]) <= 1024 * 1024
    inspect = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=30, check=False)
    assert inspect.returncode != 0


@pytest.mark.parametrize("command", ["wp eval", "wp --quiet eval"])
def test_php_shortcuts_fail_policy_even_when_the_state_is_correct(environment, command) -> None:
    test = load_tests(DatasetConfig(source="local", name="wp-cli-v1"))[0]
    environment.reset()
    result = environment.execute_artifact(
        Artifact(kind="wp_cli_shell", code=command + " 'update_option(\"cli_probe\",\"changed\");'"),
        {"static_checks": test.static_checks,
         "runtime_checks": {"assertions": [{"type": "option_value", "target": "cli_probe", "expected": "changed"}]}},
    )
    test.runtime_checks = {"assertions": [{"type": "option_value"}]}
    scores = BenchmarkRunner._score_execution(result.raw, test)
    assert result.raw["runtime"]["score"] == 1, result.raw
    assert scores["static_policy_pass"] is False
    assert scores["execution_pass"] is False


def test_concurrent_shell_workflows_have_private_database_and_files(environment) -> None:
    barrier = threading.Barrier(2)

    def candidate(name):
        environment.reset()
        barrier.wait(timeout=15)
        result = environment.execute_artifact(Artifact(kind="wp_cli_shell", code=f"""
            wp option update cli_owner {name} >/dev/null
            printf {name} > /tmp/cli_owner
            sleep 0.2
        """), {"runtime_checks": {"assertions": [{"type": "custom_assertion", "code": f"""
            return get_option('cli_owner') === '{name}' && file_get_contents('/tmp/cli_owner') === '{name}';
        """}]}})
        assert result.success, result.raw

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(candidate, ["first", "second"]))
