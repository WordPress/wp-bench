"""Typed configuration models for the WP-Bench harness.

Every field on these models is either implemented or rejected loudly.
``extra="forbid"`` on all models means unknown (or removed) fields fail
at load time instead of becoming silent no-ops: config means behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator, validator

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
    revision: Optional[str] = None
    split: str = "test"
    cache_dir: Optional[Path] = None


class ModelConfig(StrictModel):
    #: Informational provider family, recorded in result records for audit.
    #: Routing itself is driven by ``name`` (a LiteLLM model string).
    kind: Literal["openai", "anthropic", "ollama", "openai-compatible"] = "openai"
    name: str = "gpt-4o-mini"
    temperature: float = 0.0
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    request_timeout: float = 300.0

    @validator("temperature")
    def _clamp_temperature(cls, value: float) -> float:
        if value < 0 or value > 2:
            raise ValueError("temperature must be between 0 and 2")
        return value


class GraderConfig(StrictModel):
    kind: Literal["docker", "cli"] = "docker"
    image: str = "ghcr.io/wordpress/wp-bench-grader:latest"
    container_name: str = "wp-bench-grader"
    base_url: str = "http://localhost:8888"
    timeout_seconds: int = 90
    setup_timeout_seconds: int = 600
    wp_env_dir: Optional[Path] = None

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


class RunConfig(StrictModel):
    suite: str = "wp-core-v1"
    test_type: Optional[Literal["knowledge", "execution"]] = None
    limit: Optional[int] = None
    test_ids: List[str] = Field(default_factory=list)
    #: Reserved for deterministic subset selection; wired by seeded
    #: stratified test limiting. Not yet consumed elsewhere.
    seed: int = 1337
    concurrency: int = 5
    execution_isolation: ExecutionIsolation = "reset_per_test"
    execution_concurrency: int = 1
    dry_run: bool = False
    check_reference_solution: bool = False
    #: Skip runtime assertions (diagnostic runs only). Official leaderboard
    #: runs must not skip grading dimensions.
    skip_runtime: bool = False
    #: Skip static checks (diagnostic runs only). Official leaderboard
    #: runs must not skip grading dimensions.
    skip_static: bool = False

    @model_validator(mode="after")
    def _validate_execution_concurrency(self) -> "RunConfig":
        """Reject concurrency the isolation strategy cannot support.

        ``reset_per_test`` isolation resets one shared WordPress runtime
        before every execution test, which is only sound when execution
        tests run serially. Fail loudly instead of silently sharing mutable
        WordPress state across concurrent tests.
        """
        if self.execution_concurrency < 1:
            raise ValueError("run.execution_concurrency must be >= 1")
        if self.execution_isolation == "reset_per_test" and self.execution_concurrency > 1:
            raise ValueError(
                "run.execution_concurrency must be 1 when "
                "run.execution_isolation is 'reset_per_test': concurrent tests "
                "would share one mutable WordPress runtime. Set "
                "run.execution_isolation to 'none' to opt out of isolation "
                "(not valid for official benchmark runs)."
            )
        return self

    @model_validator(mode="after")
    def _validate_skips(self) -> "RunConfig":
        if self.skip_runtime and self.skip_static:
            raise ValueError(
                "run.skip_runtime and run.skip_static cannot both be true: "
                "execution tests would have no grading dimension left."
            )
        return self


class OutputConfig(StrictModel):
    path: Path = Path("results.json")
    jsonl_path: Optional[Path] = Field(default=Path("results.jsonl"))


class HarnessConfig(StrictModel):
    dataset: DatasetConfig = DatasetConfig()
    model: Optional[ModelConfig] = None  # Single model (legacy)
    models: Optional[List[ModelConfig]] = None  # Multiple models
    grader: GraderConfig = GraderConfig()
    run: RunConfig = RunConfig()
    output: OutputConfig = OutputConfig()

    def get_models(self) -> List[ModelConfig]:
        """Return list of models to evaluate."""
        if self.models:
            return self.models
        if self.model:
            return [self.model]
        return [ModelConfig()]  # Default

    @classmethod
    def from_file(cls, path: Path) -> "HarnessConfig":
        import yaml

        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        base_dir = path.parent

        def resolve_path(value: Optional[str | Path]) -> Optional[str]:
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

        return cls(**data)
