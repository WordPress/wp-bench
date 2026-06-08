from __future__ import annotations

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_static_analysis(pattern: str, subject: str) -> dict:
    php_code = f"""
require_once {str(PROJECT_ROOT / "runtime/src/class-static-analysis.php")!r};
$analysis = new WPBench\\Runtime\\Static_Analysis();
$result = $analysis->check(
    {subject!r},
    array(
        'required_patterns' => array(
            array(
                'pattern' => {pattern!r},
                'description' => 'test pattern',
                'weight' => 1,
            ),
        ),
    )
);
echo json_encode( $result );
"""
    proc = subprocess.run(
        ["php", "-r", php_code],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_static_analysis_matches_delimiterless_pattern_containing_slash() -> None:
    result = _run_static_analysis(
        "wpbp/count-words",
        "wp_register_ability( 'wpbp/count-words', array() );",
    )

    assert result["score"] == 1
    assert result["details"]["required"][0]["found"] is True


def test_static_analysis_preserves_explicit_regex_delimiters_and_flags() -> None:
    result = _run_static_analysis(
        "/WPBP\\/COUNT-WORDS/i",
        "wp_register_ability( 'wpbp/count-words', array() );",
    )

    assert result["score"] == 1
    assert result["details"]["required"][0]["found"] is True


def test_static_analysis_matches_delimiterless_pattern_containing_tilde() -> None:
    result = _run_static_analysis(
        "wpbp~tool",
        "wp_register_ability_category( 'wpbp~tool', array() );",
    )

    assert result["score"] == 1
    assert result["details"]["required"][0]["found"] is True
