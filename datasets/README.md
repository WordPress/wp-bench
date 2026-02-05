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

1. Create `suites/<suite-name>/execution/`, `knowledge/`, and optionally `abilities/` directories
2. Add category JSON files (e.g., `hooks.json`, `rest-api.json`) to each directory
3. Follow the schema in existing suites
4. Run `python datasets/export_dataset.py` to include in Parquet export

## Schema

### Execution Tests
| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique test ID |
| `prompt` | string | Task description for the model |
| `requirements` | array | List of requirements the solution must meet |
| `static_checks` | object | Regex patterns to check in generated code |
| `runtime_checks` | object | Assertions to run in WordPress environment |
| `reference_solution` | string | Example correct solution |

### Knowledge Tests
| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique test ID |
| `prompt` | string | Question text |
| `choices` | array | Multiple choice options `[{key, text}]` |
| `correct_answer` | string | Correct choice key (e.g., "B") |

### Abilities Tests (Tool Use)
| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique test ID |
| `prompt` | string | Task description for the model |
| `allowed_abilities` | array | List of ability names allowed |
| `max_steps` | integer | Max tool calls |
| `fixtures` | object | Setup/teardown or initial state |
| `verifiers` | array | Verifier names to score this task |
| `expected_outputs` | array | Optional expected tool outputs |
| `expected_state` | object | Optional expected state |

Notes:
- Abilities tests require the Abilities API to be available in the runtime (WordPress 6.9+ or the Abilities API plugin).

#### Verifier Names (Abilities)
Common verifier keys you can use in `verifiers`:
- `tool_calls_present` — at least one tool call was produced
- `schema_validity` — tool call includes required fields with basic types
- `allowed_abilities_only` — only abilities in `allowed_abilities` are used
- `max_steps_respected` — tool call count does not exceed `max_steps`
- `ability_success` — all tool calls executed successfully
- `ability_found` — all tool calls targeted a registered ability
- `method_valid` — ability method respected read-only vs write expectations
- `permission_ok` — no permission errors were raised by the ability
- `confirmation_ok` — destructive abilities included confirmation fields
- `expected_outputs_match` — tool call outputs match `expected_outputs`
- `expected_state_match` — post-run state checks in `expected_state` pass

#### expected_outputs
List aligned to tool call order. Each entry can be:
- a subset object matched against the tool observation `result`
- or an object with one of:
  - `match`: subset matched against `result`
  - `result`: exact/partial match against `result`
  - `equals`: exact/partial match against the full observation
  - `ability`: optional guard to ensure the expected entry aligns with a specific ability name

Example:
```json
{
  "expected_outputs": [
    {
      "ability": "wpbench/get_site_info",
      "match": { "name": "My Site" }
    }
  ]
}
```

#### expected_state
Optional list of post-run checks, each with:
- `ability` (required)
- `input` (optional)
- `method` (optional)
- `match` / `result` / `equals` (optional match against the ability result)

Example:
```json
{
  "expected_state": [
    {
      "ability": "wpbench/get_site_info",
      "match": { "url": "https://example.test" }
    }
  ]
}
```
