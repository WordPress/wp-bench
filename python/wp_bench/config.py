"""Typed configuration models for the WP-Bench harness.

Every field on these models is either implemented or rejected loudly.
``extra="forbid"`` on all models means unknown (or removed) fields fail
at load time instead of becoming silent no-ops: config means behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactKind = Literal[
    "php_snippet",
    "wp_plugin_files",
    "block_plugin",
    "wp_theme_files",
    "js_module",
    "patch",
]


class StrictModel(BaseModel):
    """Base for config models: unknown fields are errors, not no-ops."""

    model_config = ConfigDict(extra="forbid")


class DatasetConfig(StrictModel):
    source: Literal["huggingface", "local"] = "huggingface"
    name: str = "WordPress/wp-bench-v1"
    revision: str | None = None
    split: str = "test"
    cache_dir: Path | None = None


class ModelConfig(StrictModel):
    #: Informational provider family, recorded in result records for audit.
    #: Routing itself is driven by ``name`` (a LiteLLM model string).
    kind: Literal["openai", "anthropic", "ollama", "openai-compatible"] = "openai"
    name: str = "gpt-4o-mini"
    temperature: float = 0.0
    max_tokens: int | None = None
    top_p: float | None = None
    request_timeout: float = Field(default=300.0, gt=0)
    #: Additional attempts after the first call fails with a transient
    #: provider error (rate limit, timeout, connection, 5xx).
    max_retries: int = 3
    retry_min_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    retry_on_rate_limit: bool = True
    retry_on_timeout: bool = True

    @field_validator("temperature")
    @classmethod
    def _clamp_temperature(cls, value: float) -> float:
        if value < 0 or value > 2:
            raise ValueError("temperature must be between 0 and 2")
        return value

    @field_validator("top_p")
    @classmethod
    def _validate_top_p(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("top_p must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def _validate_retry_policy(self) -> ModelConfig:
        if self.max_retries < 0:
            raise ValueError("model.max_retries must be >= 0")
        if self.retry_min_seconds <= 0 or self.retry_max_seconds <= 0:
            raise ValueError("model retry backoff bounds must be positive")
        if self.retry_min_seconds > self.retry_max_seconds:
            raise ValueError(
                "model.retry_min_seconds cannot exceed model.retry_max_seconds"
            )
        return self


class GraderConfig(StrictModel):
    kind: Literal["docker", "cli"] = "docker"
    image: str = "ghcr.io/wordpress/wp-bench-grader:latest"
    container_name: str = "wp-bench-grader"
    base_url: str = "http://localhost:8888"
    timeout_seconds: int = Field(default=90, gt=0)
    setup_timeout_seconds: int = Field(default=600, gt=0)
    wp_env_dir: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_unsupported_kinds(cls, data: object) -> object:
        """Fail early with a clear message for the unimplemented HTTP grader.

        A Literal error alone ('input should be docker or cli') would be
        confusing for users following older docs that mentioned 'http'.
        """
        if isinstance(data, dict) and data.get("kind") == "http":
            raise ValueError(
                "grader.kind='http' is not supported yet. Use 'docker' (default) "
                "or 'cli'. Remote HTTP grading is planned but not implemented."
            )
        return data


ExecutionIsolation = Literal["reset_per_test", "none"]

#: Ceiling on ``run.execution_concurrency``. Every worker above the first
#: gets its own WordPress database provisioned before the run starts, so a
#: mistyped 500 would spend the run building databases. Sixteen is far below
#: anything MySQL's connection limits care about and well past the point
#: where more workers stop buying wall clock.
MAX_EXECUTION_CONCURRENCY = 16


class RunConfig(StrictModel):
    suite: str = "wp-core-v1"
    limit: int | None = Field(default=None, gt=0)
    test_ids: list[str] = Field(default_factory=list)
    #: Reserved for deterministic subset selection; wired by seeded
    #: stratified test limiting. Not yet consumed elsewhere.
    seed: int = 1337
    execution_isolation: ExecutionIsolation = "reset_per_test"
    execution_concurrency: int = 1
    dry_run: bool = False
    check_reference_solution: bool = False
    #: Record per-test errors (provider failures, harness exceptions) and
    #: continue instead of aborting the run. Errored tests are excluded from
    #: score aggregates and listed in result metadata. Diagnostic runs only:
    #: official leaderboard runs must grade every selected test.
    continue_on_error: bool = False
    #: Adversarial assertion audit: run zero-effort cheats against each test
    #: and flag any whose assertions a cheat can satisfy (they are
    #: under-specified). The mirror of check_reference_solution — that proves
    #: correct code passes; this proves wrong code fails. Diagnostic tooling,
    #: not a scored run.
    check_exploits: bool = False
    #: Skip runtime assertions (diagnostic runs only). Official leaderboard
    #: runs must not skip grading dimensions.
    skip_runtime: bool = False
    #: Skip static checks (diagnostic runs only). Official leaderboard
    #: runs must not skip grading dimensions.
    skip_static: bool = False

    @property
    def pools_databases(self) -> bool:
        """Whether the run gives each concurrent worker its own database.

        Only ``reset_per_test`` pools: ``none`` is an explicit opt-out of
        isolation, so its concurrent tests keep sharing the one runtime they
        always shared.
        """
        return self.execution_isolation == "reset_per_test" and self.execution_concurrency > 1

    @property
    def database_pool_size(self) -> int:
        """How many independent WordPress databases the run needs."""
        return self.execution_concurrency if self.pools_databases else 1

    @model_validator(mode="after")
    def _validate_execution_concurrency(self) -> RunConfig:
        """Bound concurrency to what the harness can actually provision.

        ``reset_per_test`` used to force serial execution, because every
        test reset the one shared WordPress runtime. Concurrent runs now
        provision a database per worker, so the harness never resets or
        grades two concurrent tests against the same database, and the old
        constraint is gone. What is left is a ceiling, because each worker
        costs a real database.

        This buys back what serialization was protecting and no more: see
        WordPressEnvironment._provision_worker_databases for what the pool
        does not separate.
        """
        if self.execution_concurrency < 1:
            raise ValueError("run.execution_concurrency must be >= 1")
        if self.execution_concurrency > MAX_EXECUTION_CONCURRENCY:
            raise ValueError(
                "run.execution_concurrency must be <= "
                f"{MAX_EXECUTION_CONCURRENCY}, got {self.execution_concurrency}: "
                "every worker above the first is provisioned its own WordPress "
                "database before the run starts."
            )
        return self

    @model_validator(mode="after")
    def _validate_skips(self) -> RunConfig:
        if self.skip_runtime and self.skip_static:
            raise ValueError(
                "run.skip_runtime and run.skip_static cannot both be true: "
                "execution tests would have no grading dimension left."
            )
        return self

    @model_validator(mode="after")
    def _validate_exclusive_modes(self) -> RunConfig:
        if self.check_exploits and self.check_reference_solution:
            raise ValueError(
                "run.check_exploits and run.check_reference_solution are "
                "separate audit modes and cannot both be enabled."
            )
        if self.check_exploits and self.dry_run:
            raise ValueError("run.check_exploits cannot be combined with run.dry_run.")
        return self


class OutputConfig(StrictModel):
    path: Path = Path("results.json")
    jsonl_path: Path | None = Field(default=Path("results.jsonl"))


class SkillsConfig(StrictModel):
    """Skill-injection A/B runs. When ``paths`` is set, every model runs a
    baseline pass and a with-skills pass over the identical seeded test
    subset, and the comparison table shows deltas.
    """

    #: Skill sources: a directory containing SKILL.md, or a bare .md file.
    #: Path existence and content are validated at run start (load_skills),
    #: not here — config construction stays IO-free.
    paths: list[Path] = Field(default_factory=list)
    #: Inline each skill's references/*.md into the injected content. The
    #: harness is single-shot, so the model cannot follow SKILL.md's file
    #: pointers on its own; disabling this is a diagnostic mode.
    include_references: bool = True
    #: Skip the baseline (no-skills) variant. Diagnostic escape hatch: the
    #: default in-run A/B is what makes results comparable.
    only: bool = False

    @model_validator(mode="after")
    def _validate_only_requires_paths(self) -> SkillsConfig:
        if self.only and not self.paths:
            raise ValueError("skills.only requires skills.paths to be set")
        return self


class HarnessConfig(StrictModel):
    dataset: DatasetConfig = DatasetConfig()
    model: ModelConfig | None = None  # Single model (legacy)
    models: list[ModelConfig] | None = None  # Multiple models
    grader: GraderConfig = GraderConfig()
    run: RunConfig = RunConfig()
    output: OutputConfig = OutputConfig()
    skills: SkillsConfig = SkillsConfig()

    @model_validator(mode="after")
    def _validate_unique_model_names(self) -> HarnessConfig:
        """Reject duplicate model names.

        Results are keyed by model name end to end (the results dict, the
        payload's ``models`` map, the comparison table), so two entries
        sharing a name silently collapse into one and a graded pass is
        dropped without warning.
        """
        names = [model.name for model in self.models or []]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"Duplicate model names {duplicates}: results are keyed by name, "
                "so entries sharing one would overwrite each other."
            )
        return self

    def get_models(self) -> list[ModelConfig]:
        """Return list of models to evaluate."""
        if self.models:
            return self.models
        if self.model:
            return [self.model]
        return [ModelConfig()]  # Default

    @classmethod
    def from_file(cls, path: Path) -> HarnessConfig:
        import yaml

        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        base_dir = path.parent

        def resolve_path(value: str | Path | None) -> str | None:
            if value is None:
                return None
            candidate = Path(value)
            if candidate.is_absolute():
                return str(candidate)
            return str((base_dir / candidate).resolve())

        if "dataset" in data and isinstance(data["dataset"], dict):
            cache_dir = data["dataset"].get("cache_dir")
            if cache_dir:
                data["dataset"]["cache_dir"] = resolve_path(cache_dir)

        if "grader" in data and isinstance(data["grader"], dict):
            wp_env_dir = data["grader"].get("wp_env_dir")
            if wp_env_dir:
                data["grader"]["wp_env_dir"] = resolve_path(wp_env_dir)

        if "output" in data and isinstance(data["output"], dict):
            for key in ("path", "jsonl_path"):
                if key in data["output"] and data["output"][key]:
                    data["output"][key] = resolve_path(data["output"][key])

        if "skills" in data and isinstance(data["skills"], dict):
            paths = data["skills"].get("paths")
            if isinstance(paths, list):
                data["skills"]["paths"] = [resolve_path(item) for item in paths if item]

        return cls(**data)
