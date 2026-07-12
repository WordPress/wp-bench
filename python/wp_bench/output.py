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


def print_test_warning(error: TestError) -> None:
    """Display a one-line warning for a test recorded as errored, not aborting."""
    console.print(
        f"[yellow]⚠ Test errored[/yellow] [cyan]{error.test_id}[/cyan] "
        f"([magenta]{error.test_type}[/magenta]): "
        f"[red]{type(error.original_error).__name__}[/red] {error.original_error}"
    )


def print_systemic_abort(error_count: int) -> None:
    """Explain why continue_on_error still aborted: every test errored."""
    console.print(
        f"[red]✖ Aborting despite run.continue_on_error: {error_count} "
        f"test(s) errored with zero successes — this looks systemic "
        f"(bad credentials, unreachable runtime), not per-test.[/red]"
    )


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
    table.add_column("Execution Pass", justify="right")
    table.add_column("Runtime Partial", justify="right")
    table.add_column("Overall", justify="right", style="bold")
    table.add_column("Est. Cost", justify="right")
    table.add_column("Median Latency", justify="right")

    def _fmt_score(value: float | None) -> str:
        return f"{value*100:.1f}%" if value is not None else "N/A"

    def _fmt_cost(value: float | None) -> str:
        return f"${value:.4f}" if value is not None else "N/A"

    def _fmt_latency(value: float | None) -> str:
        return f"{value:.0f}ms" if value is not None else "N/A"

    for model_name, result in results.items():
        scores = result["scores"]
        usage = result.get("usage") or {}
        table.add_row(
            model_name,
            _fmt_score(scores.get("knowledge")),
            _fmt_score(scores.get("execution_pass_rate")),
            _fmt_score(scores.get("runtime")),
            f"{scores['overall']*100:.1f}%",
            _fmt_cost(usage.get("estimated_cost_usd")),
            _fmt_latency(usage.get("median_latency_ms")),
        )

    console.print(table)


def print_reference_solution_failures(records: list[Dict[str, Any]]) -> None:
    """Print reference solution failures in a compact table."""
    table = Table(title="Reference Solution Failures")
    table.add_column("Test ID", style="cyan")
    table.add_column("Correctness", justify="right")
    table.add_column("Static", justify="right")
    table.add_column("Runtime", justify="right")

    def _fmt_score(value: Any) -> str:
        return f"{value*100:.1f}%" if isinstance(value, (int, float)) else "N/A"

    for record in records:
        grader = record.get("grader") or {}
        raw = grader.get("raw") or {}
        scores = record.get("scores") or {}
        table.add_row(
            str(record.get("test_id", "")),
            _fmt_score(scores.get("correctness")),
            _fmt_score((raw.get("static") or {}).get("score")),
            _fmt_score((raw.get("runtime") or {}).get("score")),
        )

    console.print(table)


def print_exploit_findings(records: list[Dict[str, Any]]) -> None:
    """Summarize the adversarial assertion audit and list exploitable tests."""
    exploitable = [record for record in records if record.get("exploitable")]
    auditable = [record for record in records if record.get("auditable")]
    not_auditable = len(records) - len(auditable)

    if exploitable:
        table = Table(title="Exploitable Tests (a zero-effort cheat passed)")
        table.add_column("Test ID", style="cyan")
        table.add_column("Category", style="magenta")
        table.add_column("Passing cheat", style="red")
        for record in exploitable:
            table.add_row(
                str(record.get("test_id", "")),
                str(record.get("category", "")),
                str(record.get("passing_exploit", "")),
            )
        console.print(table)

    style = "red" if exploitable else "green"
    console.print(
        f"[{style}]Exploit audit: {len(exploitable)}/{len(auditable)} auditable "
        f"tests exploitable[/{style}] "
        f"[dim]({not_auditable} not covered by the generic battery — "
        f"no test_function or plugin artifact)[/dim]"
    )


def create_progress() -> Progress:
    """Create a Progress instance for tracking test execution."""
    return Progress()
