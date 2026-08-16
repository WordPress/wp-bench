"""Tests for skill loading and variant construction."""
from __future__ import annotations

from pathlib import Path

import pytest

from wp_bench.skills import (
    BASELINE_VARIANT,
    SkillError,
    build_variants,
    load_skill,
    load_skills,
    render_skills_system_prompt,
)

SKILL_MD = """---
name: wp-example
description: "Example skill for tests."
---

# WP Example

## Procedure
1. Read: references/details.md
"""


def _write_skill(root: Path, *, references: bool = True) -> Path:
    skill_dir = root / "wp-example"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    if references:
        refs = skill_dir / "references"
        refs.mkdir()
        (refs / "details.md").write_text("Deep details.", encoding="utf-8")
        (refs / "advanced.md").write_text("Advanced notes.", encoding="utf-8")
    return skill_dir


def test_load_skill_parses_frontmatter_and_inlines_references(tmp_path: Path) -> None:
    skill = load_skill(_write_skill(tmp_path))
    assert skill.name == "wp-example"
    assert skill.description == "Example skill for tests."
    assert skill.body.startswith("# WP Example")
    assert "---" not in skill.body
    # References sorted by filename and inlined under path headings.
    assert [rel for rel, _ in skill.references] == [
        "references/advanced.md",
        "references/details.md",
    ]
    assert "## Reference: references/details.md" in skill.rendered
    assert "Deep details." in skill.rendered
    assert skill.token_estimate == len(skill.rendered) // 4


def test_load_skill_without_references(tmp_path: Path) -> None:
    skill = load_skill(_write_skill(tmp_path), include_references=False)
    assert skill.references == ()
    assert "## Reference:" not in skill.rendered


def test_load_skill_bare_markdown_file(tmp_path: Path) -> None:
    md = tmp_path / "notes.md"
    md.write_text("Just guidance, no frontmatter.", encoding="utf-8")
    skill = load_skill(md)
    assert skill.name == "notes"
    assert skill.description is None
    assert "Just guidance" in skill.rendered


def test_load_skill_name_falls_back_to_directory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("Body only.", encoding="utf-8")
    assert load_skill(skill_dir).name == "my-skill"


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda p: p / "missing", "does not exist"),
        (lambda p: _empty_dir(p), "no SKILL.md"),
        (lambda p: _skill_with(p, "---\nname: [broken\n---\nBody"), "frontmatter"),
        (lambda p: _skill_with(p, "---\nname: x\n---\n\n"), "empty"),
        (lambda p: _text_file(p, "notes.txt"), "markdown"),
    ],
)
def test_load_skill_errors(tmp_path: Path, setup, message) -> None:
    with pytest.raises(SkillError, match=message):
        load_skill(setup(tmp_path))


def _empty_dir(root: Path) -> Path:
    path = root / "empty"
    path.mkdir()
    return path


def _skill_with(root: Path, text: str) -> Path:
    path = root / "bad"
    path.mkdir()
    (path / "SKILL.md").write_text(text, encoding="utf-8")
    return path


def _text_file(root: Path, name: str) -> Path:
    path = root / name
    path.write_text("text", encoding="utf-8")
    return path


def test_load_skills_dedupes_paths(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path)
    skills = load_skills([skill_dir, skill_dir])
    assert len(skills) == 1


def test_content_hash_changes_with_reference_content(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path)
    before = load_skill(skill_dir).content_sha256
    (skill_dir / "references" / "details.md").write_text("Changed.", encoding="utf-8")
    after = load_skill(skill_dir).content_sha256
    assert before != after


def test_build_variants_matrix(tmp_path: Path) -> None:
    assert build_variants([]) == [BASELINE_VARIANT]

    skill = load_skill(_write_skill(tmp_path))
    both = build_variants([skill])
    assert [variant.key for variant in both] == ["baseline", "skills"]
    assert both[1].kind == "inject"
    assert both[1].label_suffix == "+skills"
    assert both[1].system_prompt is not None
    assert skill.rendered in both[1].system_prompt

    only = build_variants([skill], skills_only=True)
    assert [variant.key for variant in only] == ["skills"]


def test_variant_record_and_payload_info(tmp_path: Path) -> None:
    assert BASELINE_VARIANT.record_info() == {
        "key": "baseline",
        "kind": "none",
        "system_prompt_hash": None,
    }
    skill = load_skill(_write_skill(tmp_path))
    variant = build_variants([skill])[1]
    info = variant.record_info()
    assert info["key"] == "skills"
    assert info["system_prompt_hash"]
    payload = variant.payload_info()
    assert payload["skills"] == ["wp-example"]
    assert payload["system_prompt_sha256"] == info["system_prompt_hash"]


def test_render_skills_system_prompt_contains_preamble_and_all_skills(tmp_path: Path) -> None:
    first = load_skill(_write_skill(tmp_path))
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / "SKILL.md").write_text("Other body.", encoding="utf-8")
    second = load_skill(other_dir)

    prompt = render_skills_system_prompt([first, second])
    assert prompt.index("skill documents") < prompt.index("# Skill: wp-example")
    assert "# Skill: other" in prompt

def _write_second_skill(root: Path, name: str = "wp-second", body: str = "Second body.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Second skill.\n---\n\n{body}",
        encoding="utf-8",
    )
    return skill_dir


def test_build_variants_with_two_skills(tmp_path: Path) -> None:
    first = load_skill(_write_skill(tmp_path))
    second = load_skill(_write_second_skill(tmp_path))

    both = build_variants([first, second])
    assert [variant.key for variant in both] == ["baseline", "skills"]

    skills_variant = both[1]
    # Variant.skills is tuple[LoadedSkill, ...], stored in input order.
    assert skills_variant.skills == (first, second)

    # render_skills_system_prompt is called with the full list, so both
    # skills' rendered bodies must appear, in input order.
    prompt = skills_variant.system_prompt
    assert prompt is not None
    assert first.rendered in prompt
    assert second.rendered in prompt
    assert prompt.index(first.rendered) < prompt.index(second.rendered)


def test_variant_payload_info_with_two_skills(tmp_path: Path) -> None:
    first = load_skill(_write_skill(tmp_path))
    second = load_skill(_write_second_skill(tmp_path))
    variant = build_variants([first, second])[1]

    payload = variant.payload_info()
    assert payload["skills"] == ["wp-example", "wp-second"]
    assert payload["system_prompt_sha256"] == variant.record_info()["system_prompt_hash"]


def test_render_skills_system_prompt_two_skills_preserve_input_order(tmp_path: Path) -> None:
    """Regression guard: render_skills_system_prompt joins skill.rendered via
    blocks.extend(...) over the input list, so order must follow the caller's
    list order, not any internal sort."""
    first = load_skill(_write_skill(tmp_path))
    second = load_skill(_write_second_skill(tmp_path))

    forward = render_skills_system_prompt([first, second])
    assert forward.index("# Skill: wp-example") < forward.index("# Skill: wp-second")

    backward = render_skills_system_prompt([second, first])
    assert backward.index("# Skill: wp-second") < backward.index("# Skill: wp-example")