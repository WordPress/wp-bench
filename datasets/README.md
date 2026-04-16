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
| `requirements` | array | List of requirements the solution must meet |
| `static_checks` | object | Regex patterns to check in generated code |
| `runtime_checks` | object | Assertions to run in WordPress environment |
| `reference_solution` | string | Example correct solution |

### Knowledge Tests
| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique test ID |
| `prompt` | string | Question text |
| `type` | string | Knowledge mode such as `multiple_choice` or `short_answer` |
| `choices` | array | Optional multiple choice options `[{key, text}]` |
| `correct_answer` | string | Correct choice key or canonical short answer |
| `answer_type` | string | Optional short-answer scoring mode such as `exact` or `contains` |
