"""Export local JSON suites to HF-compatible Parquet format.

Usage:
    python datasets/export_dataset.py

Output:
    datasets/data/test.parquet  - ready for HF upload
"""
from __future__ import annotations

from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq

DATASETS_DIR = Path(__file__).resolve().parent
SUITES_DIR = DATASETS_DIR / "suites"
OUTPUT_DIR = DATASETS_DIR / "data"


def load_suite(suite_name: str) -> list[dict]:
    """Load and flatten a suite's JSON files into HF-compatible rows."""
    suite_dir = SUITES_DIR / suite_name
    rows = []

    # Load all execution tests from execution/ directory
    execution_dir = suite_dir / "execution"
    if execution_dir.is_dir():
        for path in sorted(execution_dir.glob("*.json")):
            data = orjson.loads(path.read_bytes())
            for t in data.get("tests", []):
                rows.append({
                    "id": t["id"],
                    "suite": suite_name,
                    "test_kind": "execution",
                    "type": "execution",
                    "prompt": t["prompt"],
                    "expected_behavior": t.get("expected_behavior", ""),
                    "category": t.get("category", "general"),
                    "requirements": orjson.dumps(t.get("requirements", [])).decode(),
                    "test_function": t.get("test_function", ""),
                    "static_checks": orjson.dumps(t.get("static_checks", {})).decode(),
                    "runtime_checks": orjson.dumps(t.get("runtime_checks", {})).decode(),
                    "reference_solution": t.get("reference_solution", ""),
                    "metadata": orjson.dumps(t.get("metadata", {})).decode(),
                    "artifact_kind": t.get("artifact_kind", "php_snippet"),
                    "reference_files": orjson.dumps(t.get("reference_files") or {}).decode(),
                    # exploit_solutions is deliberately not exported: it is
                    # maintainer-side assertion QA (--check-exploits), not
                    # benchmark content for dataset consumers.
                })

    return rows


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Find all suites
    suites = [d.name for d in SUITES_DIR.iterdir() if d.is_dir()]
    print(f"Found suites: {suites}")

    all_rows = []
    for suite in suites:
        rows = load_suite(suite)
        all_rows.extend(rows)
        print(f"  {suite}: {len(rows)} tests")

    # Convert to PyArrow table and write Parquet
    table = pa.Table.from_pylist(all_rows)
    output_path = OUTPUT_DIR / "test.parquet"
    pq.write_table(table, output_path)

    print(f"\nExported {len(all_rows)} total tests to {output_path}")
    print(f"Columns: {table.column_names}")


if __name__ == "__main__":
    main()
