# WP-Bench Datasets

This directory contains the benchmark test suites and tooling for publishing to Hugging Face Hub.

## Structure

```
datasets/
├── suites/                    # Source of truth (human-editable JSON)
│   └── wp-core-v0.0.1/
│       ├── execution.json     # Code generation tests
│       └── knowledge.json     # Multiple choice / short answer tests
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
  name: wp-core-v0.0.1
```

## Publishing to Hugging Face

1. **Export to Parquet:**
   ```bash
   python datasets/export_dataset.py
   ```

2. **Upload to HF Hub:**
   ```bash
   huggingface-cli upload WordPress/wp-bench-v0.0.1 datasets/data/
   ```

3. **Users can then load:**
   ```python
   from datasets import load_dataset
   ds = load_dataset("WordPress/wp-bench-v0.0.1", split="test")
   ```

## Adding New Suites

1. Create `suites/<suite-name>/execution.json` and `knowledge.json`
2. Follow the schema in existing suites
3. Run `python datasets/export_dataset.py` to include in Parquet export

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
