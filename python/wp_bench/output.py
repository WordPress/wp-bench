"""Rich console output formatting for WP-Bench."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
        f"[yellow]⚠ Test errored[/yellow] [cyan]{error.test_id}[/cyan]: "
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


def print_comparison_table(results: dict[str, dict[str, Any]]) -> None:
    """Print a formatted table comparing scores across all models.

    In a skills A/B run each base model has a baseline row and a "+skills"
    row; a delta row follows each such pair. Results without variant keys
    (plain multi-model runs) render exactly as before.

    Args:
        results: Dict mapping display names to their result dicts containing scores.
    """
    table = Table(title="WP-Bench Results")
    table.add_column("Model", style="cyan")
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
        label = f"{model_name} [dim](reused)[/dim]" if result.get("reused_from") else model_name
        table.add_row(
            label,
            _fmt_score(scores.get("execution_pass_rate")),
            _fmt_score(scores.get("runtime")),
            f"{scores['overall']*100:.1f}%",
            _fmt_cost(usage.get("estimated_cost_usd")),
            _fmt_latency(usage.get("median_latency_ms")),
        )

    delta_rows = _skill_delta_rows(results)
    for delta_row in delta_rows:
        table.add_row(*delta_row)
    if delta_rows:
        # One run per variant is a single sample; a small aggregate delta can
        # be run-to-run noise. The per-test Skill Impact table names what
        # actually moved. With a reused baseline the arms are not even the
        # same run, which the caption has to say rather than imply.
        reused = any(result.get("reused_from") for result in results.values())
        table.caption = (
            "Δ compares this run's skills pass against a baseline reused from an "
            "earlier run; see the per-test Skill Impact table."
            if reused
            else "Δ compares a single run per variant; see the per-test Skill Impact table."
        )

    console.print(table)


def _variant_pairs(
    results: dict[str, dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Map each base model to its (baseline, skills) result pair, when both ran."""

    def _variant_key(result: dict[str, Any]) -> str | None:
        variant = result.get("variant")
        return variant.get("key") if isinstance(variant, dict) else None

    by_base: dict[str, dict[str, dict[str, Any]]] = {}
    for result in results.values():
        key = _variant_key(result)
        if key:
            by_base.setdefault(result.get("base_model", ""), {})[key] = result

    return {
        base_model: (variants["baseline"], variants["skills"])
        for base_model, variants in by_base.items()
        if "baseline" in variants and "skills" in variants
    }


def _skill_delta_rows(results: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Build a "Δ skills" row per base model that ran both variants."""

    def _fmt_delta(base: float | None, with_skills: float | None) -> str:
        if base is None or with_skills is None:
            return "N/A"
        delta = with_skills - base
        color = "green" if delta > 0 else "red" if delta < 0 else "dim"
        return f"[{color}]{delta*100:+.1f}pp[/{color}]"

    def _usage_delta(
        base_usage: dict[str, Any],
        skill_usage: dict[str, Any],
        field: str,
        prefix: str = "",
        suffix: str = "",
    ) -> str:
        base_value, skill_value = base_usage.get(field), skill_usage.get(field)
        if base_value is None or skill_value is None:
            return "N/A"
        delta = skill_value - base_value
        precision = 4 if field == "estimated_cost_usd" else 0
        return f"{prefix}{delta:+.{precision}f}{suffix}"

    rows: list[list[str]] = []
    for base_model, (baseline, skilled) in _variant_pairs(results).items():
        base_scores, skill_scores = baseline["scores"], skilled["scores"]
        base_usage, skill_usage = baseline.get("usage") or {}, skilled.get("usage") or {}

        rows.append(
            [
                f"[dim]Δ skills ({base_model})[/dim]",
                _fmt_delta(
                    base_scores.get("execution_pass_rate"),
                    skill_scores.get("execution_pass_rate"),
                ),
                _fmt_delta(base_scores.get("runtime"), skill_scores.get("runtime")),
                _fmt_delta(base_scores.get("overall"), skill_scores.get("overall")),
                _usage_delta(base_usage, skill_usage, "estimated_cost_usd", prefix="$"),
                _usage_delta(base_usage, skill_usage, "median_latency_ms", suffix="ms"),
            ]
        )
    return rows


def print_baseline_reuse(source_path: str) -> None:
    """Say which run the baseline arm came from, before any grading starts."""
    console.print(f"[dim]Reusing baseline from {source_path}[/dim]")


def print_skill_impact(results: dict[str, dict[str, Any]]) -> None:
    """Per-test breakdown of what the skills variant changed, per base model.

    Skill authors iterate on one skill at a time; the aggregate delta hides
    which tests actually moved. For every base model that ran both variants
    this prints one row per test that changed (execution pass flip, or a
    runtime-score shift) or is still failing in both variants — the latter
    are the skill's next targets. Tests passing in both variants are only
    counted. No-op for runs without a baseline/skills pair.
    """
    for base_model, (baseline, skilled) in _variant_pairs(results).items():
        base_records = {r["test_id"]: r for r in baseline.get("results", [])}
        skill_records = {r["test_id"]: r for r in skilled.get("results", [])}

        rows: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None, str | None]] = []
        unchanged_pass = 0
        for test_id in sorted(base_records.keys() | skill_records.keys()):
            base, skill = base_records.get(test_id), skill_records.get(test_id)
            if base is None or skill is None or _test_outcome(base) != _test_outcome(skill):
                rows.append((test_id, base, skill, None))
            elif _test_outcome(base)[0] == "pass":
                unchanged_pass += 1
            else:
                rows.append((test_id, base, skill, "[dim]— still failing[/dim]"))

        table = Table(title=f"Skill Impact per Test ({base_model})")
        table.add_column("Test ID", style="cyan")
        table.add_column("Baseline", justify="right")
        table.add_column("+Skills", justify="right")
        table.add_column("Change", justify="left")
        for test_id, base, skill, change in rows:
            base_outcome = _test_outcome(base) if base else None
            skill_outcome = _test_outcome(skill) if skill else None
            table.add_row(
                test_id,
                _fmt_outcome(base_outcome),
                _fmt_outcome(skill_outcome),
                change if change is not None else _fmt_outcome_change(base_outcome, skill_outcome),
            )
        if not rows:
            table.add_row("[dim]—[/dim]", "", "", "[dim]all tests passing in both variants[/dim]")
        console.print(table)
        console.print(f"[dim]Passing in both variants (not shown): {unchanged_pass}[/dim]")


def _test_outcome(record: dict[str, Any]) -> tuple[str, float | None]:
    """Reduce a record to a comparable (status, runtime score) outcome."""
    if record.get("error") is not None:
        return ("error", None)
    scores = record.get("scores") or {}
    status = "pass" if scores.get("execution_pass") else "fail"
    return (status, scores.get("runtime"))


def _fmt_outcome(outcome: tuple[str, float | None] | None) -> str:
    if outcome is None:
        return "[dim]missing[/dim]"
    status, runtime = outcome
    if status == "pass":
        return "[green]pass[/green]"
    if status == "error":
        return "[yellow]error[/yellow]"
    if runtime is not None and runtime > 0:
        return f"[red]fail[/red] [dim]({runtime*100:.0f}% runtime)[/dim]"
    return "[red]fail[/red]"


def _fmt_outcome_change(
    base: tuple[str, float | None] | None,
    skill: tuple[str, float | None] | None,
) -> str:
    if base is None or skill is None:
        return "[yellow]present in one variant only[/yellow]"
    base_status, base_runtime = base
    skill_status, skill_runtime = skill
    if base_status != "pass" and skill_status == "pass":
        return "[green]▲ fixed by skill[/green]"
    if base_status == "pass" and skill_status != "pass":
        return "[red]▼ broken by skill[/red]"
    if base_runtime is not None and skill_runtime is not None:
        direction = "▲" if skill_runtime > base_runtime else "▼"
        color = "green" if skill_runtime > base_runtime else "red"
        return f"[{color}]{direction} runtime {base_runtime*100:.0f}% → {skill_runtime*100:.0f}%[/{color}]"
    return "[yellow]changed[/yellow]"


def print_reference_solution_failures(records: list[dict[str, Any]]) -> None:
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


def print_exploit_findings(audit: dict[str, Any], exploitable: list[dict[str, Any]]) -> None:
    """Render the adversarial assertion audit summary and exploitable tests.

    ``audit`` is the summary dict assembled by the runner (counts +
    exploitable ids); ``exploitable`` is the list of exploitable records.
    The runner owns the partition so it is not recomputed here.
    """
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

    style = "red" if audit["exploitable"] else "green"
    console.print(
        f"[{style}]Exploit audit: {audit['exploitable']}/{audit['auditable']} "
        f"auditable tests exploitable[/{style}] "
        f"[dim]({audit['not_auditable']} not covered by the generic battery — "
        f"no test_function or plugin artifact)[/dim]"
    )


def create_progress() -> Progress:
    """Create a Progress instance for tracking test execution."""
    return Progress()
