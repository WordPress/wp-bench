"""Typed configuration models for the WP-Bench harness."""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, validator

ArtifactKind = Literal[
    "php_snippet",
    "wp_plugin_files",
    "block_plugin",
    "wp_theme_files",
    "js_module",
    "patch",
]


class DatasetConfig(BaseModel):
    source: Literal["huggingface", "local"] = "huggingface"
    name: str = "WordPress/wp-bench-v1"
    revision: Optional[str] = None
    split: str = "test"
    cache_dir: Optional[Path] = None


class ModelConfig(BaseModel):
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


class GraderConfig(BaseModel):
    kind: Literal["docker", "http", "cli"] = "docker"
    image: str = "ghcr.io/wordpress/wp-bench-grader:latest"
    container_name: str = "wp-bench-grader"
    url: Optional[HttpUrl] = None
    concurrency: int = 4
    timeout_seconds: int = 90
    wp_env_dir: Optional[Path] = None


class RunConfig(BaseModel):
    suite: str = "wp-core-v1"
    test_type: Optional[Literal["knowledge", "execution"]] = None
    limit: Optional[int] = None
    seed: int = 1337
    concurrency: int = 5
    dry_run: bool = False
    skip_judge: bool = False
    skip_runtime: bool = False
    skip_static: bool = False


class OutputConfig(BaseModel):
    path: Path = Path("results.json")
    jsonl_path: Optional[Path] = Field(default=Path("results.jsonl"))
    save_prompts: bool = True
    save_artifacts_dir: Optional[Path] = Field(default=Path("artifacts"))
    enable_test_logging: bool = False
    test_log_path: Optional[Path] = Field(default=Path("test_execution.log"))


class HarnessConfig(BaseModel):
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
            for key in ("path", "jsonl_path", "save_artifacts_dir", "test_log_path"):
                if key in data["output"] and data["output"][key]:
                    data["output"][key] = resolve_path(data["output"][key])

        return cls(**data)
