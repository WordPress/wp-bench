# Leaderboard results

`results/` holds raw `wp-bench run` outputs, copied from `output/` unchanged:
`results_<timestamp>.json` (the run payload) and, when available, the matching
`results_<timestamp>.jsonl` (streamed per-test records). Nothing else belongs in
this folder. The timestamp is when the run started, so a run's JSON and JSONL share
one name.

A payload is either a single-model run (`metadata` with `model`, `scores`, `usage`
plus per-test `results`) or a multi-model run (`metadata` plus `models`, one entry
per model with its `config`, `scores` and `results`). The leaderboard page reads
both: it lists the folder, takes every model from every run, and shows the latest
run per model (older runs stay visible with "Show all runs"). Who published a run,
and when, is the file's git history, which the page shows as well.

## Adding a run

1. Check the model does not already have a run (see `AGENTS.md`, "Leaderboard
   results").
2. Copy the run output in:

   ```sh
   cp output/results_<timestamp>.json output/results_<timestamp>.jsonl leaderboard/results/
   ```

3. Commit the run files (the commit becomes the run's attribution on the page),
   then rebuild the page data and commit it too:

   ```sh
   python3 leaderboard/site/build_data.py
   ```

4. Run `pytest python -k leaderboard` and open a PR linking the discussion.

## Leaderboard page

`site/` is the static leaderboard page, hosted on Spacefast at
https://wpbench-leaderboard.space.fast/ and deployed from this folder through the
Space's Git connection.

- `site/build_data.py` reads every run in `results/` plus `git log` and writes
  `site/data/leaderboard.json`: per-run scores, usage, latency/cost/token
  percentiles, per-category pass counts, passed test ids, failure details
  (failing assertion descriptions), the commit that published each file, and the
  folder's commit history with a score snapshot per commit. It is committed so
  the page needs no API calls and no multi-megabyte downloads.
- `site/index.html` renders that file: leaderboard (latest run per model, with
  confidence intervals, cost per run and per passed test, tokens, latency),
  category heatmap, score-vs-cost chart, head-to-head comparison of two models,
  hardest tests across models, and the folder history with per-commit diffs.
  Rows of a different suite or scoring version than the selected one are greyed
  out, never ranked together.
- `pytest python -k leaderboard` fails when the data file is stale.
