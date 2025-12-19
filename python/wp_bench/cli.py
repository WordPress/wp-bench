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
) -> None:
    """Run the benchmark end-to-end."""
    harness_config = HarnessConfig.from_file(config) if config else HarnessConfig()
    if suite:
        harness_config.run.suite = suite
        harness_config.dataset.name = suite if "/" not in suite else suite
    if limit is not None:
        harness_config.run.limit = limit

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
def dry_run(config: Optional[Path] = typer.Option(None, help="Config file")) -> None:
    """Load dataset and render prompt statistics without hitting models."""
    harness_config = HarnessConfig.from_file(config) if config else HarnessConfig()
    tests = load_tests(harness_config.dataset)
    console.print(
        f"Execution tests: {len(tests['execution'])}, Knowledge tests: {len(tests['knowledge'])}"
    )


if __name__ == "__main__":  # pragma: no cover
    app()
