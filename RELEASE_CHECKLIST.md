# Release Checklist

A WP-Bench release is a scoring contract: published numbers must be
reproducible against a specific harness, dataset, runtime, and scoring
version. Complete every item before tagging a release or publishing
results.

## Versioning

- [ ] Bump the harness version in `python/pyproject.toml`.
- [ ] If any task changed (added, removed, edited prompts/checks/weights),
      bump the suite version and note affected test IDs.
- [ ] If scoring semantics changed, bump `SCORING_VERSION`
      (`python/wp_bench/scoring.py`) and document the formula change.
- [ ] If the per-test record shape changed, bump `RESULT_SCHEMA_VERSION`
      (`python/wp_bench/records.py`).

## Quality gates

- [ ] `ruff check python` — clean.
- [ ] `mypy python` — clean.
- [ ] `pytest python` — all tests pass.
- [ ] `find runtime -name '*.php' -print0 | xargs -0 -n1 php -l` — clean.
- [ ] `cd runtime && docker build -t wp-bench-grader:release .` — builds.

## Runtime verification

- [ ] Start the runtime (`cd runtime && npx wp-env start`).
- [ ] Run the full reference-solution suite:
      `wp-bench run --config wp-bench.example.yaml --check-reference-solution`
      — every reference solution passes strictly.

## Dataset artifacts

- [ ] Regenerate the export: `python datasets/export_dataset.py`.
- [ ] Verify row counts match the local suites (execution + knowledge).
- [ ] Publish the Parquet/dataset update alongside the release when tasks
      changed.

## Publication

- [ ] Publish the runtime Docker image and record its digest in the
      release notes (official runs must pin the digest, not a tag).
- [ ] Write a changelog entry stating explicitly whether scores from this
      release are comparable to the previous release, and why.
