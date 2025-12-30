# Repository Guidelines

This guide is the fastest path to contributing to WP-Bench. Keep changes minimal, documented, and verified against the harness + runtime.

## Project Structure
- `python/`: Typer CLI (`wp-bench`), config loader, LiteLLM adapters, scoring.
- `runtime/`: WordPress 6.9 plugin and Docker/wp-env assets that execute `wp bench verify`.
- `datasets/`: Canonical suites (`suites/<suite>/execution/*.json` + `knowledge/*.json`), HF builder (`wp_bench.py`), export helper.
- `legacy/wp-cli-runner/`: Archived PHP-first runner; avoid changes unless documenting history.
- `notebooks/` and `staging/`: Scratch space for experiments; avoid committing large artifacts to git.

## Build, Test, and Development Commands
- Create a venv and install the harness: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ./python`.
- Install dev tooling: `pip install -e ./python[dev]` (adds `pytest`, `ruff`, `mypy`).
- Run the benchmark locally: `wp-bench run --config python/wp-bench.example.yaml`.
- Lint: `ruff python`.
- Type check: `mypy python`.
- Unit tests: `pytest python` (use `-k <pattern>` to scope).
- Start WordPress runtime (local): `cd runtime && npx wp-env start`.
- Build Docker grader: `cd runtime && docker build -t wp-bench-grader:dev .`.

## Coding Style & Naming
- Python: 4-space indent, type hints on public functions, prefer dataclasses/pydantic for payloads. Keep Typer commands in `wp_bench/cli.py` thin and delegate to `core.py`. Use `snake_case`; CLI flags mirror config keys.
- PHP runtime: PSR-4 under `WPBench\Runtime\*`; new classes live in `runtime/src/`. Prefer strict comparisons and early returns.
- Config YAML: see `python/wp-bench.example.yaml`; add new knobs with defaults and document when user-facing.

## Testing Guidelines
- Add `test_*.py` under `python/tests` near the feature touched. Favor deterministic fixtures; mock external services (`litellm`, HTTP) at boundaries.
- For runtime changes, run `npx wp-env run cli wp bench verify --payload=<base64>` against a minimal snippet to sanity-check static/runtime assertions. If adding Docker paths, ensure the built image boots and reaches MySQL.
- Aim to cover new code paths; call out gaps in the PR if integration setup is non-trivial.

## Commit & Pull Request Guidelines
- Follow Conventional Commits (`feat:`, `refactor:`, etc.) as seen in git history. Scope prefixes are optional.
- PRs should state intent, key commands run (`ruff`, `mypy`, `pytest`, runtime smoke test), and config changes. Link related issues. Include logs or screenshots for runtime/verifier output when relevant.
- Keep changes small and focused; update README/AGENTS.md when workflows or flags change. Avoid touching `legacy/` unless explicitly required.
