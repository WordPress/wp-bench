"""Typer-based CLI for wp-bench."""
from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console

from .config import HarnessConfig, ModelConfig
from .core import BenchmarkRunner, MultiModelRunner
from .datasets import ExecutionTest, filter_tests_by_ids, load_tests
from .selection import select_tests

# Load .env file for API keys
load_dotenv()

app = typer.Typer(add_completion=False)
console = Console()


@app.callback()
def main() -> None:
    """WP-Bench command line interface."""


def _normalize_test_ids(values: list[str] | None) -> list[str]:
    """Normalize repeated and comma-separated --test-id values."""
    if not values:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        for test_id in value.split(","):
            test_id = test_id.strip()
            if test_id and test_id not in seen:
                normalized.append(test_id)
                seen.add(test_id)
    return normalized


def _load_filtered_tests(harness_config: HarnessConfig) -> list[ExecutionTest]:
    """Load tests and apply explicit test ID filtering."""
    return filter_tests_by_ids(load_tests(harness_config.dataset), harness_config.run.test_ids)


def _count_selected_tests(tests: list[ExecutionTest], harness_config: HarnessConfig) -> int:
    """Count tests selected by current run settings (uses the real selector)."""
    return len(_select_for_config(tests, harness_config))


def _select_for_config(
    tests: list[ExecutionTest],
    harness_config: HarnessConfig,
) -> list[ExecutionTest]:
    """Run the seeded stratified selector with this config's settings."""
    return select_tests(
        tests,
        limit=harness_config.run.limit,
        test_ids=harness_config.run.test_ids,
        seed=harness_config.run.seed,
    )


def _print_dry_run_counts(
    tests: list[ExecutionTest],
    harness_config: HarnessConfig,
) -> None:
    """Print test counts (and selected IDs when limited) for a dry run."""
    console.print(f"Execution tests: {_count_selected_tests(tests, harness_config)}")
    if harness_config.run.limit is not None and not harness_config.run.test_ids:
        console.print(f"Selection seed: {harness_config.run.seed}")
        selected = _select_for_config(tests, harness_config)
        if selected:
            ids = ", ".join(test.id for test in selected)
            console.print(f"Selected test ids: {ids}")


@app.command()
def run(
    config: Path | None = typer.Option(None, help="Path to wp-bench YAML config"),
    suite: str | None = typer.Option(None, help="Override suite name"),
    model_name: str | None = typer.Option(None, help="Override model name (single model mode)"),
    limit: int | None = typer.Option(None, help="Limit number of tests (seeded stratified selection)"),
    seed: int | None = typer.Option(None, help="Seed for deterministic limited-test selection"),
    dry_run: bool = typer.Option(False, help="Load and filter tests without calling models"),
    check_reference_solution: bool = typer.Option(
        False,
        help="Run execution tests with their reference_solution instead of calling models",
    ),
    check_exploits: bool = typer.Option(
        False,
        help="Adversarial assertion audit: flag execution tests a zero-effort cheat can pass",
    ),
    test_id: list[str] | None = typer.Option(
        None,
        "--test-id",
        help="Run only the given dataset test ID. May be repeated or comma-separated.",
    ),
) -> None:
    """Run the benchmark end-to-end."""
    harness_config = HarnessConfig.from_file(config) if config else HarnessConfig()
    if suite:
        harness_config.run.suite = suite
        harness_config.dataset.name = suite
    if limit is not None:
        harness_config.run.limit = limit
    if seed is not None:
        harness_config.run.seed = seed
    if dry_run:
        harness_config.run.dry_run = True
    if check_reference_solution:
        harness_config.run.check_reference_solution = True
    if check_exploits:
        harness_config.run.check_exploits = True
    normalized_test_ids = _normalize_test_ids(test_id)
    if normalized_test_ids:
        harness_config.run.test_ids = normalized_test_ids

    if harness_config.run.dry_run and harness_config.run.check_reference_solution:
        console.print("[red]--dry-run and --check-reference-solution cannot be used together.[/red]")
        raise typer.Exit(1)

    if harness_config.run.dry_run:
        try:
            tests = _load_filtered_tests(harness_config)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_dry_run_counts(tests, harness_config)
        return

    if harness_config.run.check_reference_solution:
        reference_runner = BenchmarkRunner(harness_config)
        try:
            result = reference_runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[bold green]Reference solution check completed[/bold green]", result["metadata"]["scores"])
        return

    if harness_config.run.check_exploits:
        exploit_runner = BenchmarkRunner(harness_config)
        try:
            result = exploit_runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[bold green]Exploit audit completed[/bold green]", result["metadata"]["audit"])
        return

    # Check if multi-model mode
    models = harness_config.get_models()
    if model_name:
        # Override to single model
        harness_config.model = ModelConfig(name=model_name)
        harness_config.models = None
        single_runner = BenchmarkRunner(harness_config)
        try:
            result = single_runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[bold green]WP-Bench completed[/bold green]", result["metadata"]["scores"])
    elif len(models) > 1:
        # Multi-model mode
        multi_runner = MultiModelRunner(harness_config)
        try:
            multi_runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("\n[bold green]WP-Bench completed[/bold green]")
    else:
        # Single model mode (legacy)
        if not harness_config.model:
            harness_config.model = models[0]
        single_runner = BenchmarkRunner(harness_config)
        try:
            result = single_runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[bold green]WP-Bench completed[/bold green]", result["metadata"]["scores"])


if __name__ == "__main__":  # pragma: no cover
    app()
