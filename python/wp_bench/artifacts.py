"""Artifact parsing and validation for execution tests.

Execution tests can require artifact kinds beyond a single PHP snippet.
This module turns raw model completions into validated artifacts:

- ``php_snippet``: the existing behavior; fenced PHP code becomes one
  code string.
- ``wp_plugin_files``: the model must return a JSON object with a
  ``files`` map of relative paths to file contents, which the runtime
  installs as a plugin before running assertions.

Parse and validation failures raise ArtifactError, which runners record
as structured per-test failures — a model that cannot produce a valid
artifact has failed the task, not crashed the harness.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .utils import strip_code_fences

#: Limits guarding the runtime filesystem against hostile artifacts.
MAX_ARTIFACT_FILES = 20
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

_VALID_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactError(ValueError):
    """A model completion could not be parsed into a valid artifact."""


@dataclass
class Artifact:
    """A validated candidate artifact ready for runtime verification."""

    kind: str
    code: str = ""
    files: Dict[str, str] = field(default_factory=dict)

    def payload_fields(self) -> Dict[str, Any]:
        """Fields merged into the runtime verification payload."""
        fields: Dict[str, Any] = {"artifact_kind": self.kind, "code": self.code}
        if self.files:
            fields["files"] = self.files
        return fields


def parse_artifact(completion: str, artifact_kind: str) -> Artifact:
    """Parse a model completion into a validated artifact.

    Raises:
        ArtifactError: When the completion cannot be parsed or the parsed
            artifact violates path/size constraints.
    """
    if artifact_kind == "php_snippet":
        return Artifact(kind="php_snippet", code=strip_code_fences(completion))
    if artifact_kind == "wp_plugin_files":
        return _parse_plugin_files(completion)
    raise ArtifactError(f"Unsupported artifact kind: {artifact_kind}")


def _parse_plugin_files(completion: str) -> Artifact:
    """Parse a JSON files-object completion into a plugin artifact."""
    text = strip_code_fences(completion)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ArtifactError(
            f"Plugin artifact must be a JSON object with a 'files' map: {error}"
        ) from error

    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ArtifactError("Plugin artifact JSON must contain a 'files' object.")

    files: Dict[str, str] = {}
    total_bytes = 0
    for raw_path, content in data["files"].items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise ArtifactError("Plugin artifact file paths and contents must be strings.")
        path = _validate_relative_path(raw_path)
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ArtifactError(f"Artifact file too large: {path} ({size} bytes)")
        total_bytes += size
        files[path] = content

    if not files:
        raise ArtifactError("Plugin artifact contains no files.")
    if len(files) > MAX_ARTIFACT_FILES:
        raise ArtifactError(f"Plugin artifact has too many files ({len(files)}).")
    if total_bytes > MAX_TOTAL_BYTES:
        raise ArtifactError(f"Plugin artifact too large ({total_bytes} bytes).")

    main_file = _find_main_plugin_file(files)
    if main_file is None:
        raise ArtifactError(
            "Plugin artifact needs a top-level .php file with a 'Plugin Name:' header."
        )

    return Artifact(kind="wp_plugin_files", files=files)


def _validate_relative_path(raw_path: str) -> str:
    """Reject absolute paths, traversal, and hostile path segments."""
    path = raw_path.strip().replace("\\", "/")
    if not path:
        raise ArtifactError("Artifact file path is empty.")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ArtifactError(f"Artifact file path must be relative: {raw_path}")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ArtifactError(f"Artifact file path contains traversal: {raw_path}")
        if not _VALID_PATH_SEGMENT.match(segment):
            raise ArtifactError(f"Artifact file path contains invalid characters: {raw_path}")
    return path


def _find_main_plugin_file(files: Dict[str, str]) -> Optional[str]:
    """Locate the top-level PHP file carrying the plugin header."""
    for path, content in files.items():
        if "/" in path or not path.endswith(".php"):
            continue
        if re.search(r"Plugin\s+Name\s*:", content, re.IGNORECASE):
            return path
    return None


def render_artifact_instructions(artifact_kind: str) -> str:
    """Prompt suffix telling the model what artifact format to return."""
    if artifact_kind == "wp_plugin_files":
        return (
            "Return a JSON object with a `files` object. Keys are relative file "
            "paths inside the plugin directory and values are complete file "
            "contents. Include a top-level PHP file with a valid `Plugin Name:` "
            "header. Do not include explanations or markdown fences."
        )
    return "Return only valid PHP code without explanations. Wrap the response in ```php fences."
