# WP-Bench

The official WordPress AI benchmark. Evaluate how well language models understand WordPress development—from core APIs and coding standards to plugin architecture and security best practices.

## Overview

WP-Bench measures AI model capabilities across two dimensions:

- **Knowledge** — Multiple-choice questions testing WordPress concepts, APIs, and best practices
- **Execution** — Code generation tasks graded by a real WordPress runtime for correctness and quality

The benchmark uses WordPress itself as the grader, running generated code in a sandboxed environment with static analysis and runtime assertions.

## Quick Start

### 1. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ./python
```

### 2. Configure API Keys

Create a `.env` file with your model provider API keys:

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```

### 3. Start the WordPress Runtime

```bash
cd runtime
npm install --global @wordpress/env
npx wp-env start
```

### 4. Run the Benchmark

```bash
wp-bench run --config wp-bench.example.yaml
```

Results are written to `output/results.json` with per-test logs in `output/results.jsonl`.

## Multi-Model Benchmarking

Compare multiple models in a single run by listing them in your config:

```yaml
models:
  - name: gpt-4o
  - name: gpt-4o-mini
  - name: claude-sonnet-4-20250514
  - name: claude-opus-4-5-20251101
  - name: gemini/gemini-2.5-pro
  - name: gemini/gemini-2.5-flash
```

The harness runs each model sequentially and outputs a comparison table. Model names follow [LiteLLM conventions](https://docs.litellm.ai/docs/providers).

## Configuration

Copy `wp-bench.example.yaml` and customize:

```yaml
dataset:
  source: local              # 'local' or 'huggingface'
  name: wp-core-v1           # suite name

models:
  - name: gpt-4o

grader:
  kind: docker
  wp_env_dir: ./runtime      # path to wp-env project

run:
  suite: wp-core-v1
  limit: 10                  # limit tests (null = all)
  concurrency: 4

output:
  path: output/results.json
  jsonl_path: output/results.jsonl
```

### CLI Options

```bash
wp-bench run --config wp-bench.yaml          # run with config file
wp-bench run --model-name gpt-4o --limit 5   # quick single-model test
wp-bench dry-run --config wp-bench.yaml      # validate config without calling models
```

## Repository Structure

```
.
├── python/          # Benchmark harness (pip installable)
├── runtime/         # WordPress grader plugin + wp-env config
├── datasets/        # Test suites (local JSON + Hugging Face builder)
├── notebooks/       # Results visualization and reporting
└── output/          # Benchmark results (gitignored)
```

## Test Suites

Test suites live in `datasets/suites/` with two categories per suite:

- `execution.json` — Code generation tasks with assertions
- `knowledge.json` — Multiple-choice knowledge questions

The default suite `wp-core-v1` covers WordPress core APIs, hooks, database operations, and security patterns.

### Loading from Hugging Face

```yaml
dataset:
  source: huggingface
  name: WordPress/wp-bench-v1
```

## Results & Reporting

After running benchmarks, visualize results with the included Jupyter notebook:

```bash
pip install jupyter pandas plotly
jupyter notebook notebooks/results_report.ipynb
```

The notebook generates:
- Overall scores bar chart
- Knowledge vs Correctness comparison
- Radar chart for top models
- Exportable HTML report

## How Grading Works

1. The harness sends a prompt to the model requesting WordPress code
2. Generated code is sent to the WordPress runtime via WP-CLI
3. The runtime performs static analysis (syntax, coding standards, security)
4. Code executes in a sandbox with test assertions
5. Results return as JSON with scores and detailed feedback

```bash
# Manual grading example
npx wp-env run cli wp bench verify --payload=$(echo '{"code":"<?php echo 1;"}' | base64)
```

## Development

```bash
pip install -e ./python[dev]    # install with dev dependencies
ruff check python/              # lint
mypy python/                    # type check
pytest python/                  # test
```

## License

GPL-2.0-or-later
