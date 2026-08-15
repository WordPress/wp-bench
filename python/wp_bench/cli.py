"""Typer-based CLI for wp-bench."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import typer
from dotenv import load_dotenv
from rich.console import Console

from .config import HarnessConfig, ModelConfig
from .core import BenchmarkRunner, MultiModelRunner, select_run_tests
from .datasets import ExecutionTest, filter_tests_by_ids, load_tests
from .skills import LoadedSkill, load_skills

# Load .env file for API keys
load_dotenv()

T = TypeVar("T")

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


def _run_or_fail(action: Callable[[], T]) -> T:
    """Run a CLI action, surfacing ValueError as the red message + exit 1."""
    try:
        return action()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _print_dry_run_counts(
    tests: list[ExecutionTest],
    harness_config: HarnessConfig,
) -> None:
    """Print selected test counts (and IDs when limited) for a dry run.

    Selection goes through the same guarded chokepoint as every run mode,
    so zero selected tests (missing suite, execution-less dataset) fails
    loudly here too instead of printing a successful zero count.
    """
    selected = select_run_tests(tests, harness_config)
    console.print(f"Execution tests: {len(selected)}")
    if harness_config.run.limit is not None and not harness_config.run.test_ids:
        console.print(f"Selection seed: {harness_config.run.seed}")
        ids = ", ".join(test.id for test in selected)
        console.print(f"Selected test ids: {ids}")


def _print_skill_summary(skills: list[LoadedSkill]) -> None:
    """Print which skills would be injected (dry run)."""
    if not skills:
        return
    console.print("Skills to inject:")
    for loaded in skills:
        console.print(
            f"  {loaded.name} (~{loaded.token_estimate} tokens, "
            f"{len(loaded.references)} reference file(s))"
        )


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
    skill: list[Path] | None = typer.Option(
        None,
        "--skill",
        help=(
            "Skill to inject (directory containing SKILL.md, or a bare .md file). "
            "May be repeated. Each model then runs both a baseline pass and a "
            "with-skills pass over the identical test subset."
        ),
    ),
    skills_include_references: bool | None = typer.Option(
        None,
        "--skills-include-references/--no-skills-include-references",
        help="Inline each skill's references/*.md into the injected content (default: on).",
    ),
    baseline_from: Path | None = typer.Option(
        None,
        "--baseline-from",
        help=(
            "Reuse the baseline pass from a previous results JSON instead of "
            "grading it again. Validated against this run (same models, tests, "
            "and versions) before any model call."
        ),
    ),
    skills_only: bool = typer.Option(
        False,
        help="Skip the baseline (no-skills) pass; run only the with-skills variant.",
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
    if skill:
        harness_config.skills.paths = list(skill)
    if skills_include_references is not None:
        harness_config.skills.include_references = skills_include_references
    if skills_only:
        harness_config.skills.only = True
    if baseline_from is not None:
        harness_config.skills.baseline_from = baseline_from

    if harness_config.run.dry_run and harness_config.run.check_reference_solution:
        console.print("[red]--dry-run and --check-reference-solution cannot be used together.[/red]")
        raise typer.Exit(1)

    if harness_config.skills.baseline_from is not None:
        # SkillsConfig validates these too, but CLI flags mutate the model
        # after construction, so its validators never re-run.
        if not harness_config.skills.paths:
            console.print("[red]--baseline-from requires at least one --skill (or skills.paths).[/red]")
            raise typer.Exit(1)
        if harness_config.skills.only:
            console.print(
                "[red]--baseline-from and --skills-only conflict: one supplies a "
                "baseline to compare against, the other drops the comparison.[/red]"
            )
            raise typer.Exit(1)

    if harness_config.skills.only and not harness_config.skills.paths:
        console.print("[red]--skills-only requires at least one --skill (or skills.paths).[/red]")
        raise typer.Exit(1)

    skills_active = bool(harness_config.skills.paths)
    if skills_active and (
        harness_config.run.check_reference_solution or harness_config.run.check_exploits
    ):
        console.print(
            "[red]Skills cannot be combined with --check-reference-solution or "
            "--check-exploits: audit modes never call a model.[/red]"
        )
        raise typer.Exit(1)

    # Load skills before anything expensive so bad paths fail fast.
    loaded_skills: list[LoadedSkill] = _run_or_fail(
        lambda: load_skills(
            harness_config.skills.paths,
            include_references=harness_config.skills.include_references,
        )
    )

    if harness_config.run.dry_run:
        _run_or_fail(
            lambda: _print_dry_run_counts(_load_filtered_tests(harness_config), harness_config)
        )
        _print_skill_summary(loaded_skills)
        return

    if harness_config.run.check_reference_solution:
        result = _run_or_fail(BenchmarkRunner(harness_config).run)
        console.print("[bold green]Reference solution check completed[/bold green]", result["metadata"]["scores"])
        return

    if harness_config.run.check_exploits:
        result = _run_or_fail(BenchmarkRunner(harness_config).run)
        console.print("[bold green]Exploit audit completed[/bold green]", result["metadata"]["audit"])
        return

    models = harness_config.get_models()
    if model_name:
        harness_config.model = ModelConfig(name=model_name)
        harness_config.models = None
        models = harness_config.get_models()
    if skills_active:
        # Skills always run the (model x variant) matrix, even for one
        # model: the A/B payload shape and delta table live in the multi
        # runner only.
        harness_config.models = models
        harness_config.model = None
        _run_or_fail(MultiModelRunner(harness_config, skills=loaded_skills).run)
        console.print("\n[bold green]WP-Bench completed[/bold green]")
        return
    if not model_name and len(models) > 1:
        _run_or_fail(MultiModelRunner(harness_config).run)
        console.print("\n[bold green]WP-Bench completed[/bold green]")
        return
    if not harness_config.model:
        harness_config.model = models[0]
    result = _run_or_fail(BenchmarkRunner(harness_config).run)
    console.print("[bold green]WP-Bench completed[/bold green]", result["metadata"]["scores"])


if __name__ == "__main__":  # pragma: no cover
    app()
