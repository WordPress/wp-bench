# WP-Bench

The official WordPress AI benchmark. Evaluate how well language models understand WordPress development—from core APIs and coding standards to plugin architecture and security best practices.

## Overview

WP-Bench measures AI model capabilities on **execution**: code generation tasks graded by static checks and runtime assertions in a real WordPress environment.

The benchmark uses WordPress itself as the grader, running generated code in a sandboxed environment with static analysis and runtime assertions.

## Requirements

Requires Python version 3.10 or later and Docker with Compose support.

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

### 3. Build the WordPress Runtime

```bash
cd runtime
docker build -t wp-bench-grader:dev .
```

Runtime 2.1 runs WordPress 7.1 with SQLite. It uses the official
[SQLite Database Integration](https://github.com/WordPress/sqlite-database-integration/releases/tag/v3.0.2)
3.0.2 drop-in, pinned by version and archive checksum. No database server is
started. For a persistent diagnostic runtime, run
`docker compose up -d --build --renew-anon-volumes --wait`. `docker compose stop`
stops it; `docker compose down --volumes` removes it and
its disposable data.
Renewing anonymous volumes when Compose recreates a container ensures rebuilt
WordPress, verifier, and SQLite adapter files are used.
The grader runs through WP-CLI; `grader.base_url` sets the installed site's URL
and no HTTP port is exposed.

For an existing local config, remove `grader.wp_env_dir` and use
`grader.image: wp-bench-grader:dev` as below. SQLite is the sole supported
database backend; there is no backend selection setting.
Existing MySQL databases are not migrated. With the default
`run.execution_isolation: reset_per_test`, the harness creates a fresh trusted
site from `grader.image` and captures its SQLite database and configuration on
the host. Every candidate then receives a new container, pristine WordPress
files, and a copy of that database. Existing Compose containers and their data
are never used or reset by isolated runs. Building the image is sufficient;
starting Compose is optional for diagnostic runs with isolation `none`.

Each candidate has private plugins, uploads, logs, temporary files, and process
and network namespaces. Networking is disabled, the image filesystem is read-only,
and execution uses an unprivileged user with no Linux capabilities. WordPress
files live in a private bounded tmpfs. The container and all its child processes
are removed after verification, crashes, timeouts, and Ctrl-C. Baseline snapshots
include committed WAL data and are validated before restore;
`wp core is-installed` checks each restore.
The verifier disables WordPress's automatic update-check hooks so admin and
maintenance actions do not depend on WordPress.org availability in this offline site.

`run.execution_concurrency` bounds simultaneous tests (default `1`). For example:

```bash
wp-bench run --config wp-bench.example.yaml --execution-concurrency 4
```

Serial and concurrent runs use the same container-per-candidate isolation.
Reference checks, exploit audits, and model/skill variants use this path too.
Each runtime defaults to 1 CPU, 512 MiB memory with swap disabled, 256 MiB for
WordPress files, 64 MiB for `/tmp`, and 64 processes; the configurable limits are
shown in the example YAML. Filesystem usage counts toward the memory limit.

Official runs must use `reset_per_test`, grade every selected test and dimension,
and publish a fixed execution profile: concurrency, image ID, limits, and host
capacity. Results record `metadata.runtime_isolation_boundary: container_per_test`,
`metadata.execution_concurrency`, `metadata.runtime_image_id`, and grader limits.
Containers share the host kernel and physical resources; CPU limits are caps,
not reserved cores. Select concurrency that fits available CPU and memory,
allowing headroom for the host. Wall-clock timeouts can still depend on host
load, so verify score parity before comparing different execution profiles.
Isolation `none` and the external CLI grader are diagnostic modes with shared
state, unsuitable for published scores.

After a forced termination such as SIGKILL or a host crash, automatic cleanup
cannot run. Disposable containers are labeled `org.wordpress.wp-bench.run` with
a unique run ID. Inspect remaining labeled containers with
`docker ps -a --filter label=org.wordpress.wp-bench.run` and remove that run's
containers with `docker rm -f -v <container-name>` before another measured run.

The MySQL fixtures and assertions remain in place to exercise the adapter's
WordPress SQL compatibility, including `dbDelta()`, `SHOW COLUMNS`, `SHOW INDEX`,
identifier placeholders, and escaped LIKE queries. SQLite results should be
compared separately from MySQL results; `metadata.grader.database` records the
backend, after setup verifies that WordPress loaded the SQLite drop-in.

### 4. Run the Benchmark

```bash
cd ..
wp-bench run --config wp-bench.example.yaml
```

Results are written to `output/results.json` with per-test logs in
`output/results.jsonl`. Multi-model runs write one combined JSONL covering every
model, each record carrying the model it came from.

While a run is in flight its records stream to `output/results_<timestamp>.jsonl.partial`,
so you can `tail -f` progress and, if the run dies (dead runtime, exhausted
provider quota, Ctrl-C), keep everything already graded. **A `.partial` file is by
definition an incomplete run: read it for the individual records, never compute a
suite score from it.** The canonical `.jsonl` appears only when a run finishes, and
the timestamp in every filename is when the run *started*, so a run's JSON and JSONL
share one name.

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
  image: wp-bench-grader:dev # built in step 3
  timeout_seconds: 90        # hard cap per runtime execution (timeout = 0.0 score)
  setup_timeout_seconds: 600 # hard cap for environment setup

run:
  suite: wp-core-v1
  limit: 10                  # limit tests (null = all); seeded stratified selection
  seed: 1337                 # selection seed (same seed = same subset)
  test_ids: []               # optional explicit test IDs to run
  dry_run: false             # load/filter tests without calling models
  execution_isolation: reset_per_test  # reset WordPress before each execution test
  execution_concurrency: 1   # increase for concurrent private runtimes
  continue_on_error: false   # record per-test errors and keep going (diagnostic
                             # only; errored tests are excluded from aggregates)

output:
  path: output/results.json
  jsonl_path: output/results.jsonl
```

### CLI Options

```bash
# Run from project root
wp-bench run --config wp-bench.yaml          # run with config file
wp-bench run --model-name gpt-4o --limit 5   # quick single-model test (stratified subset)
wp-bench run --limit 5 --seed 42             # different deterministic subset
wp-bench run --test-id e-abilities-api-001
wp-bench run --test-id e-abilities-api-001 --test-id e-rest-api-001
wp-bench run --config wp-bench.yaml --dry-run # validate config without calling models
wp-bench run --check-reference-solution      # verify reference solutions pass
wp-bench run --check-exploits                # adversarial assertion audit (see below)
wp-bench run --skill /path/to/skill          # skills A/B run (see below)
```

To check SQLite compatibility without model calls:

```bash
wp-bench run --config wp-bench.example.yaml --check-reference-solution
wp-bench run --config wp-bench.example.yaml --check-exploits --test-id e-database-001
```

### Skills A/B comparison

To measure how much an agent skill improves model scores, pass one or more
skills with `--skill` (a directory containing `SKILL.md`, or a bare `.md`
file — e.g. from [WordPress/agent-skills](https://github.com/WordPress/agent-skills)).
Every model then runs **both** a baseline pass and a with-skills pass over the
identical seeded test subset; the skill content is injected as a system
message, the user prompt stays byte-identical, and the comparison table gains
a `Δ skills` row per model showing the score, cost, and latency deltas.
A per-test "Skill Impact" table follows, listing every test the skill fixed,
broke, or moved (runtime-score shifts on still-failing tests), plus the tests
still failing in both variants — the skill's next targets.

```bash
wp-bench run --config wp-bench.yaml --limit 10 \
  --skill ../agent-skills/skills/wp-plugin-development
```

By default each skill's `references/*.md` files are inlined into the injected
content (the run is single-shot, so the model cannot follow SKILL.md's file
pointers on its own); disable with `--no-skills-include-references`.
`--skills-only` skips the baseline pass. The equivalent config block is
`skills:` (see `wp-bench.example.yaml`). Run metadata records each skill's
name, source path, and content hash so results stay attributable to the exact
skill version measured.

### Adversarial assertion audit

`--check-reference-solution` proves a correct solution *passes*; `--check-exploits`
proves that trivial cheats *fail*. For every execution test it runs a battery of
zero-effort stubs (an empty function, `return 1`, `return true`, `return array()`, …)
through the real WordPress verifier and flags any test whose assertions a cheat can
satisfy. Such a test is under-specified — its assertions check a predictable output
(one fixture's answer) rather than the WordPress behavior the task describes, so a
model could score on it without doing the work. Exits non-zero if any test is
exploitable; results (with the passing cheat per test) are written to the output file.

```bash
wp-bench run --check-exploits
```

## Repository Structure

```
.
├── python/          # Benchmark harness (pip installable)
├── runtime/         # WordPress grader plugin + SQLite Docker runtime
├── datasets/        # Test suites (local JSON + Hugging Face builder)
├── notebooks/       # Results visualization and reporting
└── output/          # Benchmark results (gitignored)
```

## Test Suites

Test suites live in `datasets/suites/<suite-name>/`:

- `execution/` — Code generation tasks with assertions (one JSON file per category)

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
- Radar chart for top models
- Exportable HTML report

## How Grading Works

1. The harness sends a prompt to the model requesting WordPress code
2. Generated code is sent to the WordPress runtime
3. The runtime performs static analysis (syntax, coding standards, security)
4. Code executes in a sandbox with test assertions
5. Results return as JSON with scores and detailed feedback

## Development

```bash
pip install -e ./python[dev]    # install with dev dependencies
ruff check python/              # lint
mypy python/                    # type check
pytest python/                  # test
```

## License

[GPL-2.0-or-later](https://github.com/WordPress/wp-bench/blob/trunk/LICENSE.md)
