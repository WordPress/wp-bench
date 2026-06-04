"""Rich console output formatting for WP-Bench."""
from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from pathlib import Path
    from .core import TestError

console = Console()


def print_test_error(error: TestError) -> None:
    """Display a detailed error panel for a failed test."""
    content = Text()
    content.append("Test ID\n", style="bold")
    content.append(f"  {error.test_id}\n\n", style="cyan")

    content.append("Test Type\n", style="bold")
    content.append(f"  {error.test_type}\n\n", style="magenta")

    content.append("Error Type\n", style="bold")
    content.append(f"  {type(error.original_error).__name__}\n\n", style="red")

    content.append("Message\n", style="bold")
    content.append(f"  {error.original_error}\n\n", style="yellow")

    content.append("Traceback\n", style="bold")
    for line in error.traceback_str.strip().split("\n"):
        content.append(f"  {line}\n", style="bright_black")

    panel = Panel(
        content,
        title="[red bold]Benchmark Failed[/red bold]",
        subtitle="[dim]Fix the error and re-run[/dim]",
        border_style="red",
        box=box.HEAVY,
        padding=(1, 2),
    )

    console.print()
    console.print(panel)


def print_abort_message() -> None:
    """Display a message when the benchmark is aborted by the user."""
    panel = Panel(
        Text("Benchmark interrupted by user (Ctrl+C)", style="yellow"),
        title="[yellow bold]Aborted[/yellow bold]",
        border_style="yellow",
        box=box.HEAVY,
        padding=(1, 2),
    )
    console.print()
    console.print(panel)


def print_model_header(model_name: str) -> None:
    """Print a header announcing which model is being run."""
    console.print(f"\n[bold blue]Running: {model_name}[/bold blue]")


def print_results_path(path: Path) -> None:
    """Print the path where results were written."""
    console.print(f"Results written to: {path}")


def print_comparison_table(results: Dict[str, Dict[str, Any]]) -> None:
    """Print a formatted table comparing scores across all models.

    Args:
        results: Dict mapping model names to their result dicts containing scores.
    """
    table = Table(title="WP-Bench Results")
    table.add_column("Model", style="cyan")
    table.add_column("Knowledge", justify="right")
    table.add_column("Correctness", justify="right")
    table.add_column("Overall", justify="right", style="bold")

    def _fmt_score(value: float | None) -> str:
        return f"{value*100:.1f}%" if value is not None else "N/A"

    for model_name, result in results.items():
        scores = result["scores"]
        table.add_row(
            model_name,
            _fmt_score(scores["knowledge"]),
            _fmt_score(scores["correctness"]),
            f"{scores['overall']*100:.1f}%",
        )

    console.print(table)


def create_progress() -> Progress:
    """Create a Progress instance for tracking test execution."""
    return Progress()
