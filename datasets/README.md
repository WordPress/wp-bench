# WP-Bench Datasets

This directory contains the benchmark test suites and tooling for publishing to Hugging Face Hub.

## Structure

```
datasets/
├── suites/                    # Source of truth (human-editable JSON)
│   └── wp-core-v1/
│       ├── execution/         # Code generation tests (one file per category)
│       │   ├── hooks.json
│       │   ├── rest-api.json
│       │   └── ...
│       └── knowledge/         # Multiple choice / short answer tests
│           ├── hooks.json
│           ├── rest-api.json
│           └── ...
├── data/                      # Generated Parquet for HF (gitignored)
│   └── test.parquet
├── export_dataset.py          # Converts suites → Parquet
└── README.md
```

## Local Development

The harness loads directly from `suites/` JSON files:

```yaml
# wp-bench.yaml
dataset:
  source: local
  name: wp-core-v1
```

## Publishing to Hugging Face

1. **Export to Parquet:**
   ```bash
   python datasets/export_dataset.py
   ```

2. **Upload to HF Hub:**
   ```bash
   huggingface-cli upload WordPress/wp-bench-v1 datasets/data/
   ```

3. **Users can then load:**
   ```python
   from datasets import load_dataset
   ds = load_dataset("WordPress/wp-bench-v1", split="test")
   ```

## Adding New Suites

1. Create `suites/<suite-name>/execution/` and `knowledge/` directories
2. Add category JSON files (e.g., `hooks.json`, `rest-api.json`) to each directory
3. Follow the schema in existing suites
4. Run `python datasets/export_dataset.py` to include in Parquet export

## Schema

### Execution Tests
| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique test ID |
| `prompt` | string | Task description for the model |
| `expected_behavior` | string | Reviewer-facing contract describing the behavior assertions should cover |
| `requirements` | array | List of requirements the solution must meet |
| `test_function` | string | PHP signature of the entry point the verifier calls; shown to the model and checked at runtime via `function_exists()` (unscored) |
| `static_checks` | object | Regex patterns to check in generated code |
| `runtime_checks` | object | Assertions to run in WordPress environment |
| `reference_solution` | string | Example correct solution |
| `artifact_kind` | string | What the model must produce: `php_snippet` (default) or `wp_plugin_files` |
| `reference_files` | object | For `wp_plugin_files` tests: reference plugin files (relative path → contents) |

For `wp_plugin_files` tasks the model must return a JSON object with a
`files` map (relative paths → complete file contents), including one
top-level PHP file with a `Plugin Name:` header. The runtime installs
the files as a plugin, loads it, runs the task's assertions, and removes
it. Artifacts are validated before install: no absolute paths, no `..`
traversal, limited file count and total size.

### Knowledge Tests
| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique test ID |
| `prompt` | string | Question text |
| `type` | string | Knowledge mode such as `multiple_choice` or `short_answer` |
| `choices` | array | Optional multiple choice options `[{key, text}]` |
| `correct_answer` | string | Correct choice key or canonical short answer |
| `answer_type` | string | Optional short-answer scoring mode such as `exact` or `contains` |

### Per-Test Metadata

Every test (execution and knowledge) may carry a `metadata` object with
provenance and coverage fields. Metadata is preserved end-to-end: local
JSON → Parquet export (as a JSON-encoded `metadata` column) → Hugging Face
loading → benchmark result records. In loaded tests and result records,
task metadata fields sit at the top level of `metadata` and the containing
suite file's metadata is nested under `metadata.suite_metadata` (a task
field named `suite_metadata` would be shadowed — avoid that name).

| Field | Type | Description |
|-------|------|-------------|
| `source_refs` | array | WordPress core source files this task is based on |
| `release_focus` | string | WordPress release the task targets (e.g. `6.9`, `classic`) |
| `wordpress_min_version` | string | Optional minimum WordPress version |
| `wordpress_target_version` | string | Optional intended WordPress version |
| `requires_multisite` | bool | Optional; task needs a multisite install |
| `review_status` | string | Optional editorial state (e.g. `reviewed`) |

All fields are optional; unknown fields are preserved as-is. Prefer adding
new fields here before inventing per-suite conventions.
