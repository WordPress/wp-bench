"""Typer-based CLI for wp-bench."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console

from .config import HarnessConfig, ModelConfig
from .core import BenchmarkRunner, MultiModelRunner
from .datasets import load_tests

# Load .env file for API keys
load_dotenv()

app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def run(
    config: Optional[Path] = typer.Option(None, help="Path to wp-bench YAML config"),
    suite: Optional[str] = typer.Option(None, help="Override suite name"),
    model_name: Optional[str] = typer.Option(None, help="Override model name (single model mode)"),
    limit: Optional[int] = typer.Option(None, help="Limit number of tests"),
    test_type: Optional[str] = typer.Option(None, help="Run only 'knowledge' or 'execution' tests"),
) -> None:
    """Run the benchmark end-to-end."""
    harness_config = HarnessConfig.from_file(config) if config else HarnessConfig()
    if suite:
        harness_config.run.suite = suite
        harness_config.dataset.name = suite if "/" not in suite else suite
    if limit is not None:
        harness_config.run.limit = limit
    if test_type is not None:
        if test_type not in ("knowledge", "execution"):
            console.print(f"[red]Invalid --test-type: {test_type}. Must be 'knowledge' or 'execution'.[/red]")
            raise typer.Exit(1)
        harness_config.run.test_type = test_type  # type: ignore[assignment]

    # Check if multi-model mode
    models = harness_config.get_models()
    if model_name:
        # Override to single model
        harness_config.model = ModelConfig(name=model_name)
        harness_config.models = None
        runner = BenchmarkRunner(harness_config)
        result = runner.run()
        console.print("[bold green]WP-Bench completed[/bold green]", result["metadata"]["scores"])
    elif len(models) > 1:
        # Multi-model mode
        runner = MultiModelRunner(harness_config)
        runner.run()
        console.print("\n[bold green]WP-Bench completed[/bold green]")
    else:
        # Single model mode (legacy)
        if not harness_config.model:
            harness_config.model = models[0]
        runner = BenchmarkRunner(harness_config)
        result = runner.run()
        console.print("[bold green]WP-Bench completed[/bold green]", result["metadata"]["scores"])


@app.command()
def dry_run(
    config: Optional[Path] = typer.Option(None, help="Config file"),
    test_type: Optional[str] = typer.Option(None, help="Filter to 'knowledge' or 'execution' tests"),
) -> None:
    """Load dataset and render prompt statistics without hitting models."""
    harness_config = HarnessConfig.from_file(config) if config else HarnessConfig()
    tests = load_tests(harness_config.dataset)
    if test_type == "knowledge":
        console.print(f"Knowledge tests: {len(tests['knowledge'])}")
    elif test_type == "execution":
        console.print(f"Execution tests: {len(tests['execution'])}")
    else:
        console.print(
            f"Execution tests: {len(tests['execution'])}, Knowledge tests: {len(tests['knowledge'])}"
        )


if __name__ == "__main__":  # pragma: no cover
    app()
