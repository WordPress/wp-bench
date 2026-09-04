---
name: wp-bench-execution-tests
description: Add, revise, or review WP-Bench WordPress execution tests. Use when working on datasets/suites/*/execution JSON, runtime_checks, static_checks, reference_solution, reference_files, exploit_solutions, expected_behavior, test ID filtering, WordPress API benchmark coverage, or PR review comments about execution test quality.
---

# WP-Bench Execution Tests

Use this skill when adding or reviewing execution tests for WP-Bench.

## Workflow

1. Inspect nearby execution tests before editing. Match the suite's organization, naming style, and category balance.
2. Treat the WordPress source/runtime as the authority. The grader runs **WordPress 7.1**. Verify every API on the `7.1` branch of wordpress-develop (`git show origin/7.1:<path>`); anything whose `@since` is `7.2.0` or later, or that only exists on trunk, is not available. Prefer reading the source over recalling from memory.
3. Define the observable WordPress behavior first. Prompts should be specific enough to identify the intended API area and outcome, but should not give away exact implementation details that the test is meant to measure, such as a particular argument key, metadata field, or helper call. Ask for a behavior or artifact, not an arbitrary wrapper function, unless the function itself is the contract.
4. Keep `requirements` concise and model-facing. They are appended to the prompt.
5. Keep `expected_behavior` reviewer-facing. It documents the contract and review focus; it is not used for scoring.
6. Use `reference_solution` (and `reference_files` for plugin tests) as the canonical passing implementation. It is for verification and maintenance, not model input.
7. Make static checks robust for the contract: require expected functions, methods, classes, hooks, slugs, schema keys, and other identifiers when their use is essential to the task. Do not require incidental helpers or checker calls that the runtime assertion can perform itself. Required patterns are diagnostics only — runtime assertions decide the pass — while forbidden patterns with severity `error` fail the test outright, so reserve them for genuine policy violations.
8. Make runtime checks test the behavior inside WordPress. Use built-in assertion types when they directly express the check, such as output containment or REST response checks. Use `custom_assertion` when the verifier needs PHP to inspect the result, such as checking a registered category, returned value, database state, capability result, dispatched hook, or computed WordPress output.
9. Design every assertion so a zero-effort cheat fails: use two or more fixtures, or a before/after contrast, so no constant return (`true`, `false`, `1`, `0`, `null`, `''`, `array()`) or empty stub satisfies it. Where the generic cheat battery cannot express a plausible shortcut, author `exploit_solutions`.
10. Verify `reference_solution` with `--check-reference-solution` and the assertions with `--check-exploits` for every new or modified execution test.

## Field Semantics

- `id`: `e-<category>-NNN`, numbered per category file.
- `category`: matches the suite file name.
- `prompt`: The task sent to the model.
- `requirements`: Additional model-facing constraints.
- `test_function`: PHP signature of the entry point the verifier calls, e.g. `wpbp_queries_004( string $category_slug, array $tag_slugs ): WP_Query`. Set it whenever assertions invoke the function. Shown to the model and prepended to the runtime assertions as a weight-0 `function_exists` check (unscored). Use parameter names that convey meaning; pin the return type only when assertions check it.
- `expected_behavior`: Reviewer documentation. Must differ from `prompt`.
- `reference_solution`: Canonical passing code used for author verification. Required for every test, including plugin-artifact tests.
- `artifact_kind`: What the model must produce. `php_snippet` (default) or `wp_plugin_files` (a JSON `files` map installed as a plugin before assertions run).
- `reference_files`: For `wp_plugin_files` tests, the reference plugin files (relative path → contents) used by `--check-reference-solution`. Required in addition to `reference_solution`.
- `exploit_solutions`: Optional list of PHP snippets that must **fail** the assertions. Each defines the gateway function (when there is one) and mimics a plausible shortcut — a hard-coded fixture answer, an incomplete implementation, a wrong API. `--check-exploits` runs them after the generic battery. They are always PHP snippets, even for plugin-artifact tests (never a `{"files": ...}` map). Maintainer-side QA; not exported to the dataset.
- `static_checks`: Coarse guardrails for required or forbidden code patterns.
- `runtime_checks.setup`: Optional PHP fixture setup evaluated before the submitted code.
- `runtime_checks.assertions`: WordPress-executed behavioral assertions evaluated after the submitted code.
- `runtime_checks.teardown`: Optional PHP cleanup evaluated after assertions, even when setup, submitted code, or assertions fail. Use it for cleanup, not correctness. Teardown exceptions are swallowed.
- `metadata.source_refs`: Required source pointers (wordpress-develop paths).
- `metadata.release_focus`: `classic` for long-standing APIs, otherwise the WordPress minor the behavior depends on (`6.9`, `7.0`, `7.1`). The dataset test requires a minimum number of modern-release tests.

There is no difficulty field.

## Prompt And Assertion Shape

Write tests around the contract, not the harness mechanics.

Good:

```json
{
  "prompt": "Register an Abilities API category with the slug 'wpbp-tools' so it is discoverable by WordPress.",
  "static_checks": {
    "required_patterns": [
      { "pattern": "wp_register_ability_category", "description": "Uses the Abilities category API", "weight": 1 },
      { "pattern": "wpbp-tools", "description": "Registers the requested category slug", "weight": 1 },
      { "pattern": "wp_abilities_api_categories_init", "description": "Uses the category init hook", "weight": 1 }
    ]
  },
  "runtime_checks": {
    "assertions": [
      {
        "type": "custom_assertion",
        "code": "return wp_has_ability_category( 'wpbp-tools' );",
        "description": "The wpbp-tools category is discoverable",
        "weight": 1
      }
    ]
  }
}
```

Avoid:

- Requiring a wrapper function name unless implementing that function is the real task.
- Naming the gateway function in the prompt — `test_function` owns the naming; write the prompt as a natural task.
- Repeating the `test_function` name in `static_checks` or giving it scoring weight — it is harness scaffolding, not a WordPress skill.
- Needing two entry points in one test — split it into two tests.
- Requiring the model to call the same checker API that the runtime assertion can call.
- Assertions a constant return can satisfy (one fixture, one expected value).
- Putting fixture cleanup inside assertions instead of `runtime_checks.teardown`.
- Adding cleanup by habit when the state is process-local.
- Making `prompt` and `expected_behavior` duplicates.

Static check patterns are regular expressions. Delimiterless patterns are wrapped by the runtime, so simple slugs like `wpbp/count-words` can be written without escaping. Use explicit regex delimiters only when flags are needed, such as `/pattern/i`.

Use the pattern list to enforce important API surface, not just one token from the prompt. If a task requires retrieving an ability and executing it, check for the function, method, and ability name, such as `wp_get_ability`, `execute`, and `wpbp/add-one`.

## Scoring (v3.0)

Runtime behavior is the only signal. A test passes strictly (`execution_pass`) when the code runs without crash or timeout, every runtime assertion passes (runtime score ≥ 0.999), and no authored forbidden static pattern with severity `error` matches. The suite score is the execution pass rate. Static required-pattern scores are recorded as diagnostics and do not grant or deny credit; the runtime's bundled security pattern set is not applied. Author accordingly: the runtime assertions must fully express the contract on their own.

Each runtime execution is capped by `grader.timeout_seconds` (default 90s), covering WordPress bootstrap, setup, submitted code, assertions, and teardown; a timed-out test scores 0.0.

## Runtime Facts

The grader is a WP-CLI PHP process (`wp eval-file`) against a single-site WordPress 7.1 with the default block theme, no other plugins, no HTTP request, no network, no JavaScript. Runtime order is `setup`, submitted code, assertions, then `teardown`.

- **Any PHP notice, warning, or deprecation aborts the run.** The sandbox error handler throws on every error level, including `@`-suppressed calls, and `WP_DEBUG` is on so `_doing_it_wrong()` also throws. Setup and assertions must be notice-clean; when a `_doing_it_wrong` is expected, add `add_filter( 'doing_it_wrong_trigger_error', '__return_false' )` in setup. Avoid core paths that rely on `@` for their failure branches.
- `rest_do_request()` dispatches only: it skips `check_authentication()` and `response_to_data()`. Call `rest_get_server()->response_to_data( $response, true )` to observe `_links`/`_embedded`, and `rest_get_server()->check_authentication()` to test `rest_authentication_errors`.
- `pre_http_request` fires before `wp_http_validate_url()` and before every `http_api_debug` action. Mock only the URLs that should succeed; keep counters inside the mock. `wp_http_validate_url()` resolves named hosts with DNS, so use dotted-quad IPs in fixtures.
- `pre_wp_mail` fires before `wp_mail_from`, `wp_mail_from_name`, and `wp_mail_content_type` are applied; capture sender identity, content type, and attachments via `phpmailer_init` instead.
- `is_admin()` is false and admin hooks never fire on their own: `define( 'WP_ADMIN', true )` in setup flips `is_admin()`; `require_once ABSPATH . 'wp-admin/includes/<file>.php'` for admin functions (menus, settings errors, meta boxes, list tables, media sideload, `wp_delete_user`); fire `admin_menu`, `admin_init`, `wp_dashboard_setup` from assertions and reset the globals they populate between contrasts.
- AJAX and `wp_die()` paths need a throwing `wp_die_handler`/`wp_die_ajax_handler` filter in setup (pattern: wordpress-develop `tests/phpunit/includes/testcase-ajax.php`).
- Pretty permalinks are off. Set `permalink_structure` and call `$wp_rewrite->init()` in setup, regenerate with `$wp_rewrite->rewrite_rules()`, and never call `flush_rewrite_rules()` (it persists across the run).
- theme.json and global styles are cached: call `wp_clean_theme_json_cache()` in setup and again after registering a filter.
- No image generation: fabricate attachment metadata with `wp_update_attachment_metadata()` rather than `wp_generate_attachment_metadata()`.
- `wp_new_comment()` reads `$_SERVER['REMOTE_ADDR']` unguarded; set it in setup.
- KSES state follows `wp_set_current_user()`; reset to user 0 in teardown. Role and capability changes persist in the `wp_user_roles` option; remove them in teardown.
- `assert_returns_value` compares with `===` against JSON-decoded values (scalars and arrays only); use `custom_assertion` for objects.

## Setup, Teardown, And Isolation

Use `runtime_checks.setup` to create fixtures the submitted code or assertions need. Use `runtime_checks.teardown` to remove persistent fixtures and restore global state. Keep assertions focused on measuring behavior.

Clean up state in `teardown` when it persists beyond the PHP process or can affect later assertions:

- posts, users, terms, comments, options, metadata
- scheduled cron events and transients
- object cache values with reusable keys/groups
- files or uploads created during the test
- site-wide filters added by the test (`remove_all_filters( 'hook' )`)
- options such as `timezone_string`, `gmt_offset`, `permalink_structure`, `wp_user_roles`

Avoid cleanup for in-process-only registries when each verifier run starts a fresh WP-CLI process. Extra cleanup can make failing cases noisy and less diagnostic.

The harness resets the WordPress environment between execution tests by default (`run.execution_isolation: reset_per_test` — database reset plus fresh install), so cross-test leakage is prevented even when a teardown is missed. Teardown still matters within a single test: assertions run in the same process and site state as the submitted code, and authors iterating with `execution_isolation: none` rely on it.

## Plugin Artifact Tests

For `artifact_kind: wp_plugin_files`, the model must return a JSON object with a `files` map (relative paths → complete file contents) including one top-level PHP file with a `Plugin Name:` header. Provide `reference_files` and a non-empty `reference_solution`, and author `exploit_solutions` (the generic cheat battery skips plugin tests).

How the runtime installs the artifact:

- Files land in `WP_PLUGIN_DIR/wp-bench-candidate-<random>/`. The directory name changes every run, so never assert on a fixed basename, slug, or path. Discover dynamic hook names by prefix (for example a `$wp_filter` key starting with `activate_wp-bench-candidate-`).
- Only the top-level file with the `Plugin Name:` header is included (`include_once`). The plugin is not activated: activation and deactivation hooks do not fire, it is not in `active_plugins`, and `uninstall.php` is never executed. Assertions must `do_action()` those hooks themselves.
- The plugin is loaded **before** `runtime_checks.setup` runs and after WordPress has fired `plugins_loaded` and `init`. Register on those hooks anyway (that is the real-world contract) and have the assertion fire them or call the registered callbacks.
- Validation limits: 20 files, 256KB per file, 1MB total, path segments matching `[A-Za-z0-9._-]+`, no absolute paths or `..`. Binary files (`.mo`) are impossible; use `.l10n.php` translation files. A completion that fails validation scores as a failed test.

## Validation

For each changed test, run:

```bash
.venv/bin/python -m pytest python/tests/test_execution_dataset.py
.venv/bin/wp-bench run --config wp-bench.yaml --dry-run --test-id <test-id>
.venv/bin/wp-bench run --config wp-bench.yaml --check-reference-solution --test-id <test-id>
.venv/bin/wp-bench run --config wp-bench.yaml --check-exploits --test-id <test-id>
```

`--test-id` may be repeated or comma-separated. With `--test-id` set, the dry run prints only the selected count (`Execution tests: 1`), not the IDs. Never combine `--dry-run` with an audit flag, and never combine `--check-reference-solution` with `--check-exploits`: the CLI applies flags after config validation, so such runs silently do only one of the things. The reference-solution run must execute the selected tests through the real WordPress verifier, without model calls, and pass every selected test; the exploit audit must report the tests as not exploitable.

For broad suite changes, also run:

```bash
.venv/bin/wp-bench run --config wp-bench.yaml --dry-run
.venv/bin/wp-bench run --config wp-bench.yaml --check-reference-solution
.venv/bin/wp-bench run --config wp-bench.yaml --check-exploits
.venv/bin/python datasets/export_dataset.py
git diff --check
```

The exploit audit resets WordPress before every cheat candidate, so it costs several times a reference-solution pass; scope it with `--test-id` while iterating.

When a model fails a test, treat the failure as a suspected test bug first: compare the model's output against the WordPress source cited in `source_refs`, re-run the reference solution, and rule out over-tight assertions, 7.2-only APIs, hidden fixture knowledge, and environment artifacts before counting it as a model mistake.

## Determinism

- AI Client tests must not make live provider calls or require credentials.
- Avoid network, uncontrolled time, random IDs without cleanup, and dependency on unrelated global state.
- Never assert fixed values for generated secrets (passwords, reset keys, UUIDs); assert their properties.
- Prefer deterministic WordPress fixtures created by setup code and removed by teardown when persistent.
