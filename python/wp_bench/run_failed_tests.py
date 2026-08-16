"""WP-Bench Retry Utility: Re-run failed benchmark tests from existing results artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson
import typer
from rich.console import Console

from .config import HarnessConfig, ModelConfig
from .core import BenchmarkRunner
from .records import execution_record_passed

app = typer.Typer(add_completion=False)
console = Console()


def extract_failed_test_ids(results_path: Path | str) -> list[str]:
    """Parse a results JSON file and collect all unique test IDs that did not pass.

    A test is marked for rerun if:
    - It encountered an error before/during grading (record['error'] is not None).
    - It ran through evaluation but failed assertions (execution_record_passed is False).
    Args:
        results_path: Path to the JSON results artifact from a previous benchmark run.
    Returns:
        A list of unique string test IDs that failed or errored.
    Raises:
        ValueError: If the specified results file does not exist.
    """
    path = Path(results_path)
    if not path.exists():
        raise ValueError(f"Results file not found: {path}")

    data = orjson.loads(path.read_bytes())

    # Support both wrapped artifact dictionaries and raw record lists
    records: list[dict[str, Any]]
    if isinstance(data, dict):
        records = data.get("records") or data.get("results") or []
    elif isinstance(data, list):
        records = data
    else:
        records = []

    failed_ids: list[str] = []
    seen: set[str] = set()

    for record in records:
        test_id = record.get("test_id") or record.get("id")
        if not test_id:
            continue

        # A test qualifies for rerun if it failed assertions OR had an execution error
        has_error = record.get("error") is not None
        passed = execution_record_passed(record)

        if (not passed or has_error) and test_id not in seen:
            failed_ids.append(test_id)
            seen.add(test_id)

    return failed_ids


@app.command()
def main(
    results_file: Path = typer.Argument(
        ...,
        help="Path to previous results JSON artifact (e.g. output/results.json)",
    ),
    config: Path | None = typer.Option(
        None,
        help="Path to custom wp-bench YAML configuration file",
    ),
    model_name: str | None = typer.Option(
        None,
        "--model-name",
        "-m",
        help="Override the model name for the retry run",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List failed test IDs without running benchmark execution",
    ),
) -> None:
    """Extract failed/errored tests from a results artifact and execute a targeted rerun."""
    try:
        failed_ids = extract_failed_test_ids(results_file)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if not failed_ids:
        console.print(
            f"[bold green]No failed or errored tests found in {results_file}![/bold green]"
        )
        return

    console.print(
        f"Found [bold yellow]{len(failed_ids)}[/bold yellow] test(s) to rerun:"
    )
    for tid in failed_ids:
        console.print(f"  • {tid}")

    # Build HarnessConfig targeting exclusively the failed test IDs
    harness_config = (
        HarnessConfig.from_file(config) if config else HarnessConfig()
    )
    harness_config.run.test_ids = failed_ids

    if model_name:
        harness_config.model = ModelConfig(name=model_name)
        harness_config.models = None

    if dry_run:
        console.print(
            f"\n[cyan]Dry-run complete. Would rerun {len(failed_ids)} test(s).[/cyan]"
        )
        return

    # Execute directly via wp-bench's native BenchmarkRunner
    console.print(
        f"\n[bold blue]Running {len(failed_ids)} failed test(s)...[/bold blue]"
    )
    try:
        runner = BenchmarkRunner(harness_config)
        result = runner.run()
        console.print(
            "\n[bold green]Rerun completed successfully![/bold green]",
            result.get("metadata", {}).get("scores", {}),
        )
    except Exception as exc:
        console.print(f"[red]Error during rerun execution: {exc}[/red]")
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()