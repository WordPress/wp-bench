---
name: wp-bench-execution-tests
description: Add, revise, or review WP-Bench WordPress execution tests. Use when working on datasets/suites/*/execution JSON, runtime_checks, static_checks, reference_solution, expected_behavior, test ID filtering, WordPress API benchmark coverage, or PR review comments about execution test quality.
---

# WP-Bench Execution Tests

Use this skill when adding or reviewing execution tests for WP-Bench.

## Workflow

1. Inspect nearby execution tests before editing. Match the suite's organization, naming style, and category balance.
2. Treat the WordPress source/runtime as the authority. For modern APIs, verify behavior against WordPress 7.0 source or official field-guide docs before writing assertions.
3. Define the observable WordPress behavior first. Prompts should be specific enough to identify the intended API area and outcome, but should not give away exact implementation details that the test is meant to measure, such as a particular argument key, metadata field, or helper call. Ask for a behavior or artifact, not an arbitrary wrapper function, unless the function itself is the contract.
4. Keep `requirements` concise and model-facing. They are appended to the prompt.
5. Keep `expected_behavior` reviewer-facing. It documents the contract and review focus; it is not used for scoring.
6. Use `reference_solution` as the canonical passing implementation. It is for verification and maintenance, not model input.
7. Make static checks robust for the contract: require expected functions, methods, classes, hooks, slugs, schema keys, and other identifiers when their use is essential to the task. Do not require incidental helpers or checker calls that the runtime assertion can perform itself. Under scoring v2.0, required patterns are diagnostics only — runtime assertions decide the pass — while forbidden patterns with severity `error` fail the test outright, so reserve them for genuine policy violations.
8. Make runtime checks test the behavior inside WordPress. Use built-in assertion types when they directly express the check, such as output containment or REST response checks. Use `custom_assertion` when the verifier needs PHP to inspect the result, such as checking a registered category, returned value, database state, capability result, dispatched hook, or computed WordPress output.
9. Verify `reference_solution` with `wp-bench run --check-reference-solution` for every new or modified execution test.

## Field Semantics

- `prompt`: The task sent to the model.
- `requirements`: Additional model-facing constraints.
- `test_function`: PHP signature of the entry point the verifier calls, e.g. `wpbp_queries_004( string $category_slug, array $tag_slugs ): WP_Query`. Set it whenever assertions invoke the function. Shown to the model and checked at runtime with an unscored `function_exists` assertion. Use parameter names that convey meaning; pin the return type only when assertions check it.
- `expected_behavior`: Reviewer documentation.
- `reference_solution`: Canonical passing code used for author verification.
- `artifact_kind`: What the model must produce. `php_snippet` (default) or `wp_plugin_files` (a JSON `files` map installed as a plugin before assertions run).
- `reference_files`: For `wp_plugin_files` tests, the reference plugin files (relative path → contents) used by `--check-reference-solution` in place of `reference_solution`.
- `static_checks`: Coarse guardrails for required or forbidden code patterns.
- `runtime_checks.setup`: Optional PHP fixture setup evaluated before the submitted code.
- `runtime_checks.assertions`: WordPress-executed behavioral assertions evaluated after the submitted code.
- `runtime_checks.teardown`: Optional PHP cleanup evaluated after assertions, even when setup, submitted code, or assertions fail. Use it for cleanup, not correctness.
- `metadata.source_refs`: Required source pointers.

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
- Putting fixture cleanup inside assertions instead of `runtime_checks.teardown`.
- Adding cleanup by habit when the state is process-local.
- Making `prompt` and `expected_behavior` duplicates.

Static check patterns are regular expressions. Delimiterless patterns are wrapped by the runtime, so simple slugs like `wpbp/count-words` can be written without escaping. Use explicit regex delimiters only when flags are needed, such as `/pattern/i`.

Use the pattern list to enforce important API surface, not just one token from the prompt. If a task requires retrieving an ability and executing it, check for the function, method, and ability name, such as `wp_get_ability`, `execute`, and `wpbp/add-one`.

## Difficulty

Treat `difficulty` as author-estimated implementation complexity, not scoring.

- `basic`: One obvious API or behavior, minimal setup.
- `intermediate`: Combines multiple WordPress concepts or requires lifecycle timing, setup, teardown, or edge handling.
- `hard`: Requires newer/obscure APIs plus nontrivial interaction, permissions, schemas, REST exposure, block/editor internals, or runtime reasoning.

Do not mark a test `hard` only because the API is new.

## Scoring (v2.0)

Runtime behavior is the primary signal. A test passes strictly (`execution_pass`) when the code runs without crash or timeout, every runtime assertion passes, and no forbidden static pattern with severity `error` matches. Static required-pattern scores are recorded as diagnostics and do not grant or deny credit. Author accordingly: the runtime assertions must fully express the contract on their own.

Each runtime execution is capped by `grader.timeout_seconds` (default 90s); a timed-out test scores 0.0. Keep setup, submitted-code expectations, and assertions comfortably inside that budget.

## Setup, Teardown, And Isolation

Runtime order is `setup`, submitted code, assertions, then `teardown`.

Use `runtime_checks.setup` to create fixtures the submitted code or assertions need. Use `runtime_checks.teardown` to remove persistent fixtures and restore global state. Keep assertions focused on measuring behavior.

Clean up state in `teardown` when it persists beyond the PHP process or can affect later assertions:

- posts, users, terms, comments, options, metadata
- scheduled cron events and transients
- object cache values with reusable keys/groups
- files or uploads created during the test

Avoid cleanup for in-process-only registries when each verifier run starts a fresh WP-CLI process. Extra cleanup can make failing cases noisy and less diagnostic.

The harness also resets the WordPress environment between execution tests by default (`run.execution_isolation: reset_per_test` — database reset plus fresh install), so cross-test leakage is prevented even when a teardown is missed. Teardown still matters within a single test: assertions run in the same process and site state as the submitted code.

## Plugin Artifact Tests

For `artifact_kind: wp_plugin_files`, the model must return a JSON object with a `files` map (relative paths → complete file contents) including one top-level PHP file with a `Plugin Name:` header. The runtime installs the files as a plugin, loads the main file, runs the assertions, and removes the plugin directory. Provide `reference_files` instead of `reference_solution`, and keep artifacts within the validation limits: 20 files, 256KB per file, 1MB total, no path traversal. A completion that fails artifact validation scores as a failed test.

## Validation

For each changed test, run:

```bash
.venv/bin/python -m pytest python/tests/test_execution_dataset.py
.venv/bin/wp-bench run --config wp-bench.yaml --dry-run --test-id <test-id>
.venv/bin/wp-bench run --config wp-bench.yaml --check-reference-solution --test-id <test-id>
```

Require the dry run to select only the requested test ID or IDs. Require the reference-solution run to execute the selected tests through the real WordPress verifier, without model calls, and pass every selected test.

For broad suite changes, also run:

```bash
.venv/bin/wp-bench run --config wp-bench.yaml --dry-run
.venv/bin/wp-bench run --config wp-bench.yaml --check-reference-solution
.venv/bin/python datasets/export_dataset.py
git diff --check
```

## Determinism

- AI Client tests must not make live provider calls or require credentials.
- Avoid network, uncontrolled time, random IDs without cleanup, and dependency on unrelated global state.
- Prefer deterministic WordPress fixtures created by setup code and removed by teardown when persistent.
