# Leaderboard results

`results/` holds one JSON file per benchmarked model, named after the LiteLLM
model id with `/` replaced by `__` (`gemini/gemini-3-flash-preview` →
`results/gemini__gemini-3-flash-preview.json`). The leaderboard page lists this
folder straight from GitHub, and the folder's git history is the change log:
re-running a model overwrites its file, so `git log -- leaderboard/results` shows
who changed which score, and when.

The initial files were seeded from the tables in
[issue #5](https://github.com/WordPress/wp-bench/issues/5); fields the source did
not state are `null`.

## File shape

```json
{
  "metadata": { ... },      // the "metadata" object from output/results_<timestamp>.json, minus "results"
  "submission": {
    "submitted_by": "github-login",
    "source": "https://github.com/WordPress/wp-bench/...",
    "run_date": "2026-09-23",
    "reported_at": "2026-10-02",
    "tests": { "passed": 152, "total": 185 },
    "notes": null
  }
}
```

`metadata` is the harness run metadata exactly as `wp-bench run` writes it
(suite, model config, grader, dataset, scoring version, usage, scores, selected
and errored test ids), with the per-test `results` array dropped. The leaderboard
reads these keys from it:

| Key | Meaning |
| --- | --- |
| `model.name` | Model id; must match the file name. |
| `suite` | Dataset suite (`wp-core-v1`, `wp-cli-v1`, ...), or `null` when unknown. |
| `scoring_version` | `SCORING_VERSION` at run time. Only compare files with the same value. Legacy knowledge-era rows carry `scores.knowledge` and are flagged on the page. |
| `scores` | 0–1 fractions: `execution_pass_rate`, `runtime`, `correctness`, `static_policy_pass_rate`, `overall` (and legacy `knowledge`). |
| `usage.median_latency_ms`, `usage.estimated_cost_usd` | Run-level usage; cost is an estimate, not billing truth. |

`submission` is written by the person publishing the run: GitHub login, URL of
the issue/PR/comment with the numbers, dates (ISO `YYYY-MM-DD`), pass counts and
caveats.

## Adding or updating a run

1. Check the model does not already have a file (see `AGENTS.md`, "Leaderboard
   results").
2. Build the file from the run output. Single- and multi-model outputs both work;
   one file is written per model:

   ```sh
   python leaderboard/build_entry.py output/results_<timestamp>.json \
     --submitted-by <github-login> --source <url of the PR or issue comment>
   ```

   `--run-date` defaults to the timestamp in the file name; add `--notes` for
   caveats.
3. Run `pytest python -k leaderboard` and open a PR linking to the run output.
