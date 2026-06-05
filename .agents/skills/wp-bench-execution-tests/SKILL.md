---
name: wp-bench-execution-tests
description: Add, revise, or review WP-Bench WordPress execution tests. Use when working on datasets/suites/*/execution JSON, runtime_checks, static_checks, reference_solution, expected_behavior, test ID filtering, WordPress API benchmark coverage, or PR review comments about execution test quality.
---

# WP-Bench Execution Tests

Use this skill when adding or reviewing execution tests for WP-Bench.

## Workflow

1. Inspect nearby execution and knowledge tests before editing. Match the suite's organization, naming style, and category balance.
2. Treat the WordPress source/runtime as the authority. For modern APIs, verify behavior against WordPress 7.0 source or official field-guide docs before writing assertions.
3. Define the observable WordPress behavior first. Prompts should ask for a behavior or artifact, not an arbitrary wrapper function, unless the function itself is the contract.
4. Keep `requirements` concise and model-facing. They are appended to the prompt.
5. Keep `expected_behavior` reviewer-facing. It documents the contract and review focus; it is not used for scoring.
6. Use `reference_solution` as the canonical passing implementation. It is for verification and maintenance, not model input.
7. Make static checks minimal: require essential APIs, slugs, hooks, schema keys, or forbidden dangerous patterns. Do not require helper/checker calls that the runtime assertion can perform itself.
8. Make runtime checks test the behavior inside WordPress. Use built-in assertion types when they directly express the check, such as output containment or REST response checks. Use `custom_assertion` when the verifier needs PHP to inspect the result, such as checking a registered category, returned value, database state, capability result, dispatched hook, or computed WordPress output.
9. Verify `reference_solution` with `wp-bench run --check-reference-solution` and verify at least one intentionally wrong implementation. The wrong case should fail for the intended reason.

## Field Semantics

- `prompt`: The task sent to the model.
- `requirements`: Additional model-facing constraints.
- `expected_behavior`: Reviewer documentation.
- `reference_solution`: Canonical passing code used for author verification.
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
- Requiring the model to call the same checker API that the runtime assertion can call.
- Putting fixture cleanup inside assertions instead of `runtime_checks.teardown`.
- Adding cleanup by habit when the state is process-local.
- Making `prompt` and `expected_behavior` duplicates.

## Difficulty

Treat `difficulty` as author-estimated implementation complexity, not scoring.

- `basic`: One obvious API or behavior, minimal setup.
- `intermediate`: Combines multiple WordPress concepts or requires lifecycle timing, setup, teardown, or edge handling.
- `hard`: Requires newer/obscure APIs plus nontrivial interaction, permissions, schemas, REST exposure, block/editor internals, or runtime reasoning.

Do not mark a test `hard` only because the API is new.

## Setup, Teardown, And Isolation

Runtime order is `setup`, submitted code, assertions, then `teardown`.

Use `runtime_checks.setup` to create fixtures the submitted code or assertions need. Use `runtime_checks.teardown` to remove persistent fixtures and restore global state. Keep assertions focused on measuring behavior.

Clean up state in `teardown` when it persists beyond the PHP process or can affect later assertions:

- posts, users, terms, comments, options, metadata
- scheduled cron events and transients
- object cache values with reusable keys/groups
- files or uploads created during the test

Avoid cleanup for in-process-only registries when each verifier run starts a fresh WP-CLI process. Extra cleanup can make failing cases noisy and less diagnostic.

## Validation

For each changed test, run:

```bash
.venv/bin/python -m pytest python/tests/test_execution_dataset.py
.venv/bin/wp-bench run --config wp-bench.yaml --dry-run --test-type execution --test-id <test-id>
.venv/bin/wp-bench run --config wp-bench.yaml --check-reference-solution --test-type execution --test-id <test-id>
```

Require the dry run to select only the requested test ID or IDs. Require the reference-solution run to execute the selected tests through the real WordPress verifier, without model calls, and pass every selected test.

For broad suite changes, also run:

```bash
.venv/bin/wp-bench run --config wp-bench.yaml --dry-run --test-type execution
.venv/bin/wp-bench run --config wp-bench.yaml --check-reference-solution --test-type execution
.venv/bin/python datasets/export_dataset.py
git diff --check
```

## Determinism

- AI Client tests must not make live provider calls or require credentials.
- Avoid network, uncontrolled time, random IDs without cleanup, and dependency on unrelated global state.
- Prefer deterministic WordPress fixtures created by setup code and removed by teardown when persistent.
