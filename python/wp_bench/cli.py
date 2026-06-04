"""Typer-based CLI for wp-bench."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console

from .config import HarnessConfig, ModelConfig
from .core import BenchmarkRunner, MultiModelRunner
from .datasets import ensure_test_ids_match_type, filter_tests_by_ids, load_tests

# Load .env file for API keys
load_dotenv()

app = typer.Typer(add_completion=False)
console = Console()


@app.callback()
def main() -> None:
    """WP-Bench command line interface."""


def _normalize_test_ids(values: Optional[List[str]]) -> List[str]:
    """Normalize repeated and comma-separated --test-id values."""
    if not values:
        return []

    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        for test_id in value.split(","):
            test_id = test_id.strip()
            if test_id and test_id not in seen:
                normalized.append(test_id)
                seen.add(test_id)
    return normalized


def _load_filtered_tests(harness_config: HarnessConfig) -> dict[str, list[object]]:
    """Load tests and apply explicit test ID filtering."""
    tests = filter_tests_by_ids(load_tests(harness_config.dataset), harness_config.run.test_ids)
    ensure_test_ids_match_type(
        tests,
        harness_config.run.test_type,
        harness_config.run.test_ids,
    )
    return tests


def _count_selected_tests(tests: list[object], harness_config: HarnessConfig) -> int:
    """Count tests selected by current run settings."""
    if harness_config.run.test_ids:
        return len(tests)
    if harness_config.run.limit is None:
        return len(tests)
    return min(harness_config.run.limit, len(tests))


def _print_dry_run_counts(
    tests: dict[str, list[object]],
    harness_config: HarnessConfig,
) -> None:
    """Print test counts for a dry run."""
    test_type = harness_config.run.test_type
    if test_type == "knowledge":
        console.print(f"Knowledge tests: {_count_selected_tests(tests['knowledge'], harness_config)}")
    elif test_type == "execution":
        console.print(f"Execution tests: {_count_selected_tests(tests['execution'], harness_config)}")
    else:
        console.print(
            "Execution tests: "
            f"{_count_selected_tests(tests['execution'], harness_config)}, "
            f"Knowledge tests: {_count_selected_tests(tests['knowledge'], harness_config)}"
        )


@app.command()
def run(
    config: Optional[Path] = typer.Option(None, help="Path to wp-bench YAML config"),
    suite: Optional[str] = typer.Option(None, help="Override suite name"),
    model_name: Optional[str] = typer.Option(None, help="Override model name (single model mode)"),
    limit: Optional[int] = typer.Option(None, help="Limit number of tests"),
    test_type: Optional[str] = typer.Option(None, help="Run only 'knowledge' or 'execution' tests"),
    dry_run: bool = typer.Option(False, help="Load and filter tests without calling models"),
    test_id: Optional[List[str]] = typer.Option(
        None,
        "--test-id",
        help="Run only the given dataset test ID. May be repeated or comma-separated.",
    ),
) -> None:
    """Run the benchmark end-to-end."""
    harness_config = HarnessConfig.from_file(config) if config else HarnessConfig()
    if suite:
        harness_config.run.suite = suite
        harness_config.dataset.name = suite if "/" not in suite else suite
    if limit is not None:
        harness_config.run.limit = limit
    if dry_run:
        harness_config.run.dry_run = True
    normalized_test_ids = _normalize_test_ids(test_id)
    if normalized_test_ids:
        harness_config.run.test_ids = normalized_test_ids
    if test_type is not None:
        if test_type not in ("knowledge", "execution"):
            console.print(f"[red]Invalid --test-type: {test_type}. Must be 'knowledge' or 'execution'.[/red]")
            raise typer.Exit(1)
        harness_config.run.test_type = test_type  # type: ignore[assignment]

    if harness_config.run.dry_run:
        try:
            tests = _load_filtered_tests(harness_config)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_dry_run_counts(tests, harness_config)
        return

    # Check if multi-model mode
    models = harness_config.get_models()
    if model_name:
        # Override to single model
        harness_config.model = ModelConfig(name=model_name)
        harness_config.models = None
        runner = BenchmarkRunner(harness_config)
        try:
            result = runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[bold green]WP-Bench completed[/bold green]", result["metadata"]["scores"])
    elif len(models) > 1:
        # Multi-model mode
        runner = MultiModelRunner(harness_config)
        try:
            runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("\n[bold green]WP-Bench completed[/bold green]")
    else:
        # Single model mode (legacy)
        if not harness_config.model:
            harness_config.model = models[0]
        runner = BenchmarkRunner(harness_config)
        try:
            result = runner.run()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[bold green]WP-Bench completed[/bold green]", result["metadata"]["scores"])


if __name__ == "__main__":  # pragma: no cover
    app()
