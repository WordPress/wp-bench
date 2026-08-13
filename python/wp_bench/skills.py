"""Skill loading and run-variant modeling for skill-injection A/B runs.

A "skill" follows the SKILL.md convention (a directory containing a
``SKILL.md`` with YAML frontmatter plus optional ``references/*.md``, or a
bare markdown file). The harness is single-shot, so a skill is "used" by
injecting its rendered content as a system message; ``Variant`` models the
baseline-vs-skills axis so every model can run both passes in one run.
``Variant.kind`` is the extension seam for a future agentic mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import orjson
import yaml

from .utils import sha256

SYSTEM_PROMPT_PREAMBLE = (
    "You are assisting with WordPress development tasks. The following "
    "skill documents provide domain knowledge; apply them when relevant."
)


class SkillError(ValueError):
    """A skill path could not be loaded into an injectable skill."""


@dataclass(frozen=True)
class LoadedSkill:
    """One skill loaded from disk, rendered for prompt injection."""

    name: str
    description: str | None
    source_path: str
    body: str
    #: (relative path, file content) pairs, sorted by path.
    references: tuple[tuple[str, str], ...]
    #: The exact injection block for this skill; hashed for provenance.
    rendered: str
    content_sha256: str
    token_estimate: int

    def provenance(self) -> dict[str, Any]:
        """Identity block recorded in run metadata."""
        return {
            "name": self.name,
            "description": self.description,
            "source_path": self.source_path,
            "content_sha256": self.content_sha256,
            "token_estimate": self.token_estimate,
            "reference_count": len(self.references),
        }


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split optional ``---`` YAML frontmatter from a markdown body."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillError(f"Invalid YAML frontmatter: {exc}") from exc
    if parsed is None:
        return {}, body
    if not isinstance(parsed, dict):
        raise SkillError("SKILL.md frontmatter must be a YAML mapping.")
    return parsed, body


def _load_references(skill_dir: Path) -> tuple[tuple[str, str], ...]:
    references_dir = skill_dir / "references"
    if not references_dir.is_dir():
        return ()
    pairs = []
    for ref_path in sorted(references_dir.glob("*.md")):
        pairs.append((f"references/{ref_path.name}", ref_path.read_text(encoding="utf-8")))
    return tuple(pairs)


def _render_skill(name: str, description: str | None, body: str, references: tuple[tuple[str, str], ...]) -> str:
    """Render one skill's injection block.

    References are inlined under headings matching their relative paths so
    the body's pointers ("Read: references/foo.md") resolve in-context
    without rewriting the body itself.
    """
    parts = [f"# Skill: {name}"]
    if description:
        parts.append(description)
    parts.append(body.strip())
    for rel_path, content in references:
        parts.append(f"## Reference: {rel_path}")
        parts.append(content.strip())
    return "\n\n".join(part for part in parts if part)


def load_skill(path: Path, *, include_references: bool = True) -> LoadedSkill:
    """Load a skill from a directory containing SKILL.md or a bare .md file.

    Raises:
        SkillError: if the path is missing, SKILL.md is absent, frontmatter
            is invalid, or the body is empty.
    """
    path = path.expanduser()
    if path.is_dir():
        skill_file = path / "SKILL.md"
        if not skill_file.is_file():
            raise SkillError(f"Skill directory has no SKILL.md: {path}")
        fallback_name = path.name
        references_dir: Path | None = path
    elif path.is_file():
        if path.suffix.lower() != ".md":
            raise SkillError(f"Skill file must be markdown (.md): {path}")
        skill_file = path
        fallback_name = path.stem
        references_dir = None
    else:
        raise SkillError(f"Skill path does not exist: {path}")

    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"Cannot read skill file {skill_file}: {exc}") from exc

    try:
        frontmatter, body = _split_frontmatter(text)
    except SkillError as exc:
        raise SkillError(f"{skill_file}: {exc}") from exc
    if not body.strip():
        raise SkillError(f"Skill body is empty: {skill_file}")

    name = str(frontmatter.get("name") or fallback_name)
    raw_description = frontmatter.get("description")
    description = str(raw_description) if raw_description else None

    references: tuple[tuple[str, str], ...] = ()
    if include_references and references_dir is not None:
        references = _load_references(references_dir)

    rendered = _render_skill(name, description, body, references)
    return LoadedSkill(
        name=name,
        description=description,
        source_path=str(path.resolve()),
        body=body,
        references=references,
        rendered=rendered,
        content_sha256=sha256(rendered),
        token_estimate=len(rendered) // 4,
    )


def load_skills(paths: list[Path], *, include_references: bool = True) -> list[LoadedSkill]:
    """Load skills from paths, deduplicating repeats (order-preserving)."""
    skills: list[LoadedSkill] = []
    seen: set[str] = set()
    for path in paths:
        resolved = str(path.expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        skills.append(load_skill(path, include_references=include_references))
    return skills


def render_skills_system_prompt(skills: list[LoadedSkill]) -> str:
    """Render the full system prompt injected in the skills variant."""
    blocks = [SYSTEM_PROMPT_PREAMBLE]
    blocks.extend(skill.rendered for skill in skills)
    return "\n\n".join(blocks)


#: How a variant applies skills. "none" = baseline; "inject" = skill content
#: injected as a system message. A future agentic mode adds "agentic" here
#: and selects a different runner strategy on it.
VariantKind = Literal["none", "inject"]


@dataclass(frozen=True)
class Variant:
    """One pass of the (model x variant) run matrix."""

    key: str
    kind: VariantKind
    #: Appended to the model name for display/payload keys (e.g. "+skills").
    label_suffix: str
    skills: tuple[LoadedSkill, ...]
    system_prompt: str | None

    def record_info(self) -> dict[str, Any]:
        """Compact per-record provenance (full skill list lives in metadata)."""
        return {
            "key": self.key,
            "kind": self.kind,
            "system_prompt_hash": sha256(self.system_prompt) if self.system_prompt else None,
        }

    def payload_info(self) -> dict[str, Any]:
        """Per-model payload entry provenance."""
        return {
            "key": self.key,
            "kind": self.kind,
            "skills": [skill.name for skill in self.skills],
            "system_prompt_sha256": sha256(self.system_prompt) if self.system_prompt else None,
        }


BASELINE_VARIANT = Variant(
    key="baseline",
    kind="none",
    label_suffix="",
    skills=(),
    system_prompt=None,
)


def build_variants(skills: list[LoadedSkill], *, skills_only: bool = False) -> list[Variant]:
    """Build the variant axis of the run matrix.

    No skills -> just the baseline. Skills -> baseline plus the injected
    variant (both passes grade the identical seeded test subset), unless
    ``skills_only`` skips the baseline.
    """
    if not skills:
        return [BASELINE_VARIANT]
    skills_variant = Variant(
        key="skills",
        kind="inject",
        label_suffix="+skills",
        skills=tuple(skills),
        system_prompt=render_skills_system_prompt(skills),
    )
    if skills_only:
        return [skills_variant]
    return [BASELINE_VARIANT, skills_variant]


class BaselineReuseError(ValueError):
    """A stored baseline cannot stand in for the one this run would grade."""


@dataclass(frozen=True)
class ReusedBaseline:
    """Baseline passes taken from a previous run instead of graded again.

    The baseline arm of a skills A/B cannot change while a skill author
    iterates, so re-grading it every round costs money and wall clock for
    a result already known. Reuse is only sound when the stored pass
    measured the same thing, which ``load_baseline`` enforces before any
    model call.
    """

    source_path: str
    #: base model name -> the stored payload entry for its baseline pass.
    entries: dict[str, dict[str, Any]]

    def provenance(self) -> dict[str, Any]:
        """Recorded in run metadata so results never hide the recycling."""
        return {
            "source_path": self.source_path,
            "models": sorted(self.entries),
        }


def _baseline_entries(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a previous payload's baseline passes by base model name."""
    entries: dict[str, dict[str, Any]] = {}
    for name, entry in (payload.get("models") or {}).items():
        variant = entry.get("variant") or {}
        if variant.get("key") == "baseline":
            entries[str(entry.get("base_model") or name)] = entry
    return entries


def load_baseline(
    path: Path,
    *,
    model_names: list[str],
    test_ids: set[str],
    scoring_version: str,
    schema_version: str,
) -> ReusedBaseline:
    """Load baseline passes from a previous results file for reuse.

    Every check here exists because a mismatched baseline produces a
    plausible-looking delta that measures something other than the skill.

    Raises:
        BaselineReuseError: when the file is unreadable, was produced by a
            different scoring or schema version, lacks a baseline pass for
            a model in this run, or graded a different set of tests.
    """
    path = path.expanduser()
    try:
        payload = orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        raise BaselineReuseError(f"Cannot read baseline results {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BaselineReuseError(f"Baseline results are not a results payload: {path}")

    metadata = payload.get("metadata") or {}
    for label, current, key in (
        ("scoring", scoring_version, "scoring_version"),
        ("result schema", schema_version, "result_schema_version"),
    ):
        stored = metadata.get(key)
        if stored != current:
            raise BaselineReuseError(
                f"Baseline results use {label} version {stored!r}, this run uses "
                f"{current!r}; the scores are not comparable. Re-run the baseline."
            )

    entries = _baseline_entries(payload)
    missing = [name for name in model_names if name not in entries]
    if missing:
        raise BaselineReuseError(
            f"Baseline results have no baseline pass for {missing}. "
            f"Available: {sorted(entries) or 'none'}."
        )

    for name in model_names:
        stored_ids = {record.get("test_id") for record in entries[name].get("results") or []}
        if stored_ids != test_ids:
            only_stored = sorted(stored_ids - test_ids)[:3]
            only_now = sorted(test_ids - stored_ids)[:3]
            raise BaselineReuseError(
                f"Baseline for {name!r} graded a different test set "
                f"({len(stored_ids)} tests vs {len(test_ids)} now). "
                f"Only in baseline: {only_stored or 'none'}; only now: {only_now or 'none'}."
            )

    return ReusedBaseline(
        source_path=str(path.resolve()),
        entries={name: entries[name] for name in model_names},
    )
